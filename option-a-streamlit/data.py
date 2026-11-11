# =====================================================================
# data.py  :  OPTION A's database and actions (SQLite, no web server)
# ---------------------------------------------------------------------
# app.py (the screens) calls these functions. They save changes to the
# SQLite file, and every change is an EVENT that re-runs the optimiser
# in engine.py and stores a new plan version: the same flow as Option B.
# =====================================================================

import csv  # parse NASA FIRMS CSV hotspot feeds
import io  # in-memory text streams for CSV/XML parsing
import json  # store small lists as text and parse GeoJSON/REST payloads
import math  # haversine distance and meteorological normalization
import os  # file paths and settings
import sqlite3  # built-in SQLite database library
import ssl  # TLS context for public government CAP/RSS feeds
import threading  # one open transaction per thread
import urllib.error  # HTTP/URL error handling for live APIs
import urllib.parse  # query string encoding for REST endpoints
import urllib.request  # zero-dependency HTTP client with ETag support
import xml.etree.ElementTree as ET  # OASIS CAP 1.2 & RSS 2.0 XML parser
from contextlib import contextmanager  # transaction()
from datetime import datetime, timezone  # current time and UTC timestamps

import demo_data as demo  # shared demo world
import engine  # shared decision logic

DB_PATH = os.environ.get("DISASTER_DB", "disaster.db")  # database file name (can be changed by a setting)


def get_db_path():
    """This Streamlit session's database, or DB_PATH when no session is running."""
    try:
        from streamlit.runtime.scriptrunner_utils.script_run_context import get_script_run_ctx
        if get_script_run_ctx() is None:
            return DB_PATH
        import streamlit as st
        path = st.session_state.get("session_db_path")
        if path:
            return path
    except Exception:
        return DB_PATH
    return DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS zones(name TEXT PRIMARY KEY, lat REAL, lon REAL, population INTEGER, vulnerability REAL);
CREATE TABLE IF NOT EXISTS risk_logs(id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, zone TEXT, lat REAL,
    lon REAL, disaster_type TEXT, probability REAL, source TEXT);
CREATE TABLE IF NOT EXISTS alerts(id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, zone TEXT,
    disaster_type TEXT, severity TEXT, eta_hours REAL, radius_km REAL, sms_status TEXT);
CREATE TABLE IF NOT EXISTS incidents(id INTEGER PRIMARY KEY AUTOINCREMENT, reported_at TEXT, lat REAL NOT NULL,
    lon REAL NOT NULL, zone TEXT, need_type TEXT, people INTEGER, status TEXT, claimed_by TEXT);
CREATE TABLE IF NOT EXISTS hubs(id INTEGER PRIMARY KEY, name TEXT, lat REAL, lon REAL);
CREATE TABLE IF NOT EXISTS inventory(hub_id INTEGER, item TEXT, qty INTEGER, PRIMARY KEY(hub_id, item));
CREATE TABLE IF NOT EXISTS hospitals(id INTEGER PRIMARY KEY, name TEXT, zone TEXT, lat REAL, lon REAL,
    total_beds INTEGER, available_beds INTEGER, icu_total INTEGER, icu_available INTEGER, oxygen INTEGER);
CREATE TABLE IF NOT EXISTS roads(id TEXT PRIMARY KEY, a TEXT, b TEXT, minutes REAL, status TEXT);
CREATE TABLE IF NOT EXISTS supply_movement(entry_id INTEGER PRIMARY KEY AUTOINCREMENT, shipment_id TEXT,
    timestamp TEXT, item TEXT, qty INTEGER, source_hub TEXT, destination TEXT, status TEXT, prev_hash TEXT, hash TEXT);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, type TEXT, message TEXT);
CREATE TABLE IF NOT EXISTS plans(id INTEGER PRIMARY KEY AUTOINCREMENT, version INTEGER, kind TEXT, plan_key TEXT,
    summary TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS plan_runs(version INTEGER PRIMARY KEY, created_at TEXT, trigger_type TEXT,
    trigger_text TEXT, solver TEXT, changed INTEGER, changed_keys TEXT, shortfalls TEXT, unplaced TEXT);
"""  # the contract: same tables and columns as Option B


class ActionError(Exception):  # raised when an action is not allowed (the screen shows the message)
    pass


_tx = threading.local()  # connection shared by approve() / approve_all()


def _connect():  # autocommit unless transaction() has started BEGIN
    conn = sqlite3.connect(get_db_path(), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None
    return conn


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------
def read(sql, **params):  # SELECT -> list of dictionaries
    conn = getattr(_tx, "conn", None)  # use the open transaction when there is one
    owned = conn is None
    if owned:
        conn = _connect()
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]  # ":name" filled safely from params
    finally:
        if owned:
            conn.close()  # leave a transaction connection open


def write(sql, **params):  # INSERT / UPDATE / DELETE; commits only outside a transaction
    conn = getattr(_tx, "conn", None)
    owned = conn is None
    if owned:
        conn = _connect()
    try:
        cur = conn.execute(sql, params)  # run
        if owned:
            conn.commit()  # save immediately when this call owns the connection
        return cur.lastrowid  # id of an inserted row
    finally:
        if owned:
            conn.close()


@contextmanager
def transaction():  # several writes succeed together, or none of them are kept
    if getattr(_tx, "conn", None) is not None:
        yield _tx.conn  # already inside approve_all / approve
        return
    conn = _connect()
    _tx.conn = conn
    try:
        conn.execute("BEGIN")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        _tx.conn = None
        conn.close()


def now():  # current time as text
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log_event(event_type, message):  # one line in the live feed
    write("INSERT INTO events(timestamp, type, message) VALUES (:t, :ty, :m)", t=now(), ty=event_type, m=message)


# ---------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------
def add_ledger_entry(shipment_id, item, qty, source, dest, status):  # add one step to the ledger
    last = read("SELECT hash FROM supply_movement ORDER BY entry_id DESC LIMIT 1")  # newest fingerprint
    prev = last[0]["hash"] if last else "GENESIS"  # or the starting word
    row = {"shipment_id": shipment_id, "timestamp": now(), "item": item, "qty": int(qty),
           "source_hub": source, "destination": dest, "status": status, "prev_hash": prev}  # the record
    row["hash"] = engine.make_hash(prev, engine.ledger_record(row))  # its fingerprint
    write("""INSERT INTO supply_movement(shipment_id, timestamp, item, qty, source_hub, destination, status,
             prev_hash, hash) VALUES (:shipment_id, :timestamp, :item, :qty, :source_hub, :destination, :status,
             :prev_hash, :hash)""", **row)  # save it


def verify_ledger():  # (ok, first broken id, total rows)
    rows = read("SELECT * FROM supply_movement ORDER BY entry_id")  # every record
    ok, bad = engine.verify_chain(rows)  # engine recomputes fingerprints
    return ok, bad, len(rows)


def current_shipments():  # latest step of each shipment
    return read("""SELECT * FROM supply_movement WHERE entry_id IN
                   (SELECT MAX(entry_id) FROM supply_movement GROUP BY shipment_id) ORDER BY entry_id DESC""")


# ---------------------------------------------------------------------
# Current situation, for the engine and the map
# ---------------------------------------------------------------------
def places():  # name -> (lat, lon) for zones, hubs and hospitals
    spots = {z["name"]: (z["lat"], z["lon"]) for z in read("SELECT name, lat, lon FROM zones")}
    spots.update({h["name"]: (h["lat"], h["lon"]) for h in read("SELECT name, lat, lon FROM hubs")})
    spots.update({h["name"]: (h["lat"], h["lon"]) for h in read("SELECT name, lat, lon FROM hospitals")})
    return spots


def severity():  # zone severity, decided by the engine
    zones = read("SELECT * FROM zones")
    latest = read("""SELECT zone, disaster_type, probability FROM risk_logs
                     WHERE id IN (SELECT MAX(id) FROM risk_logs GROUP BY zone)""")
    counts = read("SELECT zone, COUNT(*) AS n FROM incidents WHERE status='OPEN' GROUP BY zone")
    return engine.compute_severity(zones, {r["zone"]: r for r in latest}, {r["zone"]: r["n"] for r in counts})


def roads_with_positions():  # roads plus both ends' positions and current time (for the map)
    spots = places()
    rows = read("SELECT * FROM roads ORDER BY CAST(SUBSTR(id, 2) AS INTEGER)")  # R1, R2 ... in number order
    for r in rows:
        r["a_lat"], r["a_lon"] = spots.get(r["a"], (None, None))
        r["b_lat"], r["b_lon"] = spots.get(r["b"], (None, None))
        r["current_minutes"] = engine.road_minutes(r)
    return rows


def shipments_with_positions():  # shipments; moving ones get a position halfway along
    spots = places()
    rows = current_shipments()
    for s in rows:
        a, b = spots.get(s["source_hub"]), spots.get(s["destination"])
        if a and b and s["status"] in ("DISPATCHED", "IN_TRANSIT"):
            s["lat"], s["lon"] = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
    return rows


def situation():  # everything the engine needs
    roads = read("SELECT * FROM roads")
    hubs = read("SELECT * FROM hubs ORDER BY id")
    hospitals = read("SELECT * FROM hospitals ORDER BY id")
    inventory = read("SELECT * FROM inventory")
    zones = [z["name"] for z in read("SELECT name FROM zones")]
    travel = engine.travel_table(roads, zones + [h["name"] for h in hubs], zones + [h["name"] for h in hospitals])
    active = current_shipments()  # DISTRIBUTED shipments still cover what was sent
    groups = []
    for incident in read("SELECT * FROM incidents WHERE need_type='Medical' AND status != 'RESOLVED' ORDER BY id"):
        if incident["people"] is None:
            continue
        waiting = int(incident["people"]) - _recorded_patients(incident["id"])
        if waiting > 0:
            groups.append({"id": incident["id"], "zone": incident["zone"], "people": waiting})
    return {"roads": roads, "hubs": hubs, "hospitals": hospitals, "inventory": inventory, "travel": travel,
            "severity": severity(), "active": active, "groups": groups}


# ---------------------------------------------------------------------
# Re-optimisation and plan versions
# ---------------------------------------------------------------------
def latest_version():  # newest plan version (0 if none)
    return int(read("SELECT MAX(version) AS v FROM plan_runs")[0]["v"] or 0)


def plan_rows_of(version):  # rows of one plan version
    return [dict(r, data=json.loads(r["data"]), key=r["plan_key"])
            for r in read("SELECT * FROM plans WHERE version=:v ORDER BY id", v=version)]


def replan(trigger_type, trigger_text):  # run the optimiser, save a new version, report what changed
    s = situation()
    recs, shortfalls, solver = engine.plan_supplies(s["severity"], s["hubs"], s["inventory"], s["active"], s["travel"])
    scores = {z["name"]: z["score"] for z in s["severity"]}
    assignments, unplaced, _ = engine.plan_patients(s["groups"], s["hospitals"], s["travel"], scores)
    rows = engine.plan_rows(recs, assignments)
    old = latest_version()
    diff = engine.diff_plans(plan_rows_of(old), rows)
    version = old + 1
    for r in rows:
        write("INSERT INTO plans(version, kind, plan_key, summary, data) VALUES (:v, :k, :key, :s, :d)",
              v=version, k=r["kind"], key=r["key"], s=r["summary"], d=json.dumps(r["data"]))
    write("DELETE FROM plans WHERE version < :v", v=version - 1)  # keep this and the previous version only
    write("""INSERT INTO plan_runs VALUES (:v, :t, :tt, :tx, :s, :c, :ck, :sf, :up)""", v=version, t=now(),
          tt=trigger_type, tx=trigger_text, s=solver, c=diff["count"], ck=json.dumps(diff["changed"] + diff["removed"]),
          sf=json.dumps(shortfalls), up=json.dumps(unplaced))
    log_event("REOPTIMIZED", f"Plan v{version}: {diff['count']} allocations changed after {trigger_type}")
    return {"version": version, "changed": diff["count"], "situation": s}


def event(event_type, message, do_replan=True):  # EVERY change goes through here
    log_event(event_type, message)  # 1. record it
    return replan(event_type, message) if do_replan else None  # 2. re-optimise


def get_plan():  # the current plan with "changed" flags
    run = read("SELECT * FROM plan_runs ORDER BY version DESC LIMIT 1")
    if not run:
        return {"version": 0, "supply": [], "patients": [], "shortfalls": [], "unplaced": [], "changed": 0}
    r = run[0]
    changed = set(json.loads(r["changed_keys"]))
    rows = plan_rows_of(r["version"])
    pack = lambda kind: [dict(x["data"], key=x["key"], changed=x["key"] in changed and r["version"] > 1)
                         for x in rows if x["kind"] == kind]
    return {"version": r["version"], "created_at": r["created_at"], "trigger_type": r["trigger_type"],
            "trigger": r["trigger_text"], "solver": r["solver"], "changed": r["changed"], "supply": pack("supply"),
            "patients": pack("patient"), "shortfalls": json.loads(r["shortfalls"]), "unplaced": json.loads(r["unplaced"])}


def plan_history():  # last 10 runs
    return read("""SELECT version, created_at, trigger_type, trigger_text, changed FROM plan_runs
                   ORDER BY version DESC LIMIT 10""")


def checks(result=None, expect_change=True):  # automatic checks on the current plan
    plan = get_plan()
    s = result["situation"] if result else situation()
    changed = result["changed"] if result else plan["changed"]
    return engine.validate_plan(plan["supply"], plan["patients"], plan["shortfalls"], s["hospitals"], s["roads"],
                                s["inventory"], s["severity"], verify_ledger()[0], changed,
                                travel=s["travel"], expect_change=expect_change)


# ---------------------------------------------------------------------
# ACTIONS (each one is an event)
# ---------------------------------------------------------------------
def add_risk_reading(zone, disaster_type, probability):
    """Record one operator risk reading. This does not queue an alert or change a plan.

    The stored source is Operator-entered. It is not an IMD reading or a live sensor.
    """
    zone_name = zone.strip() if isinstance(zone, str) else ""
    hazard = disaster_type.strip() if isinstance(disaster_type, str) else ""
    if not zone_name:
        raise ActionError("A zone is required. The reading was not recorded.")
    if not hazard:
        raise ActionError("A hazard type is required. The reading was not recorded.")
    if hazard not in demo.DISASTER_TYPES:
        raise ActionError("That hazard type is not a recorded type. The reading was not recorded.")
    if isinstance(probability, bool) or not isinstance(probability, (int, float)) or probability != probability:
        raise ActionError("A risk probability between 0 and 1 is required. The reading was not recorded.")
    if probability < 0 or probability > 1:
        raise ActionError("Risk probability must be between 0 and 1. The reading was not recorded.")
    with transaction():
        zones = read("SELECT lat, lon FROM zones WHERE name=:n", n=zone_name)
        if not zones or zones[0]["lat"] is None or zones[0]["lon"] is None:
            raise ActionError("That zone is not recorded. The reading was not recorded.")
        write("""INSERT INTO risk_logs(timestamp, zone, lat, lon, disaster_type, probability, source)
                 VALUES (:t, :z, :la, :lo, :d, :p, 'Operator-entered')""",
              t=now(), z=zone_name, la=zones[0]["lat"], lo=zones[0]["lon"], d=hazard, p=float(probability))
        event("OPERATOR_RISK_RECORDED",
              f"Operator-entered {hazard} reading for {zone_name} at probability {float(probability):.2f}. "
              "No alert was queued and the allocation plan was not changed.",
              do_replan=False)


def add_alert(zone, dtype, severity_word, eta, radius):  # store a queued alert; this does not send an SMS
    write("""INSERT INTO alerts(created_at, zone, disaster_type, severity, eta_hours, radius_km, sms_status)
             VALUES (:t, :z, :d, :s, :e, :r, 'QUEUED')""", t=now(), z=zone, d=dtype, s=severity_word, e=eta, r=radius)
    event("ALERT_QUEUED", f"{severity_word} {dtype} alert queued for {zone} (impact in {eta}h). "
          "No SMS was sent.", do_replan=False)


def acknowledge_alert(alert_id):
    """Record that an operator handled one queued alert.

    This does not send an SMS and it does not mark the alert sent.
    A repeated or ineligible transition leaves the stored status unchanged.
    """
    with transaction():
        rows = read("SELECT * FROM alerts WHERE id=:i", i=alert_id)
        if not rows:
            raise ActionError("That alert is not recorded. Status was not changed.")
        status = rows[0].get("sms_status")
        if status == "ACKNOWLEDGED":
            raise ActionError("This alert is already acknowledged. Status was not changed again.")
        if status == "SENT":
            raise ActionError("This alert is stored as sent. Status was not changed.")
        if status != "QUEUED":
            raise ActionError("This alert is not queued. Status was not changed.")
        write("UPDATE alerts SET sms_status='ACKNOWLEDGED' WHERE id=:i", i=alert_id)
        event("ALERT_ACKNOWLEDGED",
              f"Alert #{int(alert_id)} acknowledged for {rows[0].get('zone')}. "
              "An operator handled this stored alert. No SMS was sent and this is not a delivery confirmation.",
              do_replan=False)


def add_incident(zone, need, people):  # new SOS near the zone centre, returned as its new id
    lat, lon = demo.zone_position(zone)
    incident_id = write("""INSERT INTO incidents(reported_at, lat, lon, zone, need_type, people, status)
             VALUES (:t, :la, :lo, :z, :n, :p, 'OPEN')""", t=now(), la=lat + 0.03, lo=lon + 0.03, z=zone, n=need, p=people)
    event("NEW_PATIENTS" if need == "Medical" else "NEW_SOS", f"New {need} SOS in {zone} ({people} people)")
    return incident_id


def claim_incident(incident_id, team):  # NGO claims a request
    rows = read("SELECT status FROM incidents WHERE id=:i", i=incident_id)
    if not rows or rows[0]["status"] != "OPEN":
        raise ActionError("That request is already claimed or does not exist.")
    write("UPDATE incidents SET status='IN_TRANSIT', claimed_by=:c WHERE id=:i", c=team, i=incident_id)
    event("SOS_CLAIMED", f"SOS #{incident_id} claimed by {team}")


def resolve_incident(incident_id):  # close a request that a team has already claimed
    rows = read("SELECT status FROM incidents WHERE id=:i", i=incident_id)
    if not rows:
        raise ActionError("That request does not exist.")
    if rows[0]["status"] != "IN_TRANSIT":
        raise ActionError("Claim the request before resolving it.")
    write("UPDATE incidents SET status='RESOLVED' WHERE id=:i", i=incident_id)
    event("SOS_RESOLVED", f"SOS #{incident_id} resolved")


def incident_ids(rows, status):  # choices for a selectbox; empty means do not draw one
    return [row["id"] for row in rows if row.get("status") == status]


def set_road(road_id, status):  # ROAD_BLOCKED / ROAD_SLOW / ROAD_REOPENED
    road = read("SELECT * FROM roads WHERE id=:i", i=road_id)[0]
    write("UPDATE roads SET status=:s WHERE id=:i", s=status, i=road_id)
    kind = {"BLOCKED": "ROAD_BLOCKED", "SLOW": "ROAD_SLOW", "OPEN": "ROAD_REOPENED"}[status]
    event(kind, f"Road {road_id} ({road['a']} to {road['b']}) is now {status}")


def set_hospital(hospital_id, beds, icu):  # HOSPITAL_CAPACITY_CHANGED
    h = read("SELECT * FROM hospitals WHERE id=:i", i=hospital_id)[0]
    if beds > h["total_beds"] or icu > h["icu_total"]:
        raise ActionError("Free beds cannot exceed total beds.")
    write("UPDATE hospitals SET available_beds=:a, icu_available=:c WHERE id=:i", a=beds, c=icu, i=hospital_id)
    event("HOSPITAL_CAPACITY_CHANGED", f"{h['name']}: {beds} beds and {icu} ICU beds free")


def receive_stock(hub_id, item, qty):  # RESOURCE_RECEIVED
    hub = read("SELECT name FROM hubs WHERE id=:i", i=hub_id)[0]["name"]
    if read("SELECT qty FROM inventory WHERE hub_id=:h AND item=:i", h=hub_id, i=item):
        write("UPDATE inventory SET qty = qty + :q WHERE hub_id=:h AND item=:i", q=qty, h=hub_id, i=item)
    else:
        write("INSERT INTO inventory VALUES (:h, :i, :q)", h=hub_id, i=item, q=qty)
    event("RESOURCE_RECEIVED", f"{qty} {item} arrived at {hub}")


def _create_shipment(item, qty, hub_id, destination):  # stock out + first ledger step
    hub = read("SELECT name FROM hubs WHERE id=:i", i=hub_id)[0]["name"]
    stock = read("SELECT qty FROM inventory WHERE hub_id=:h AND item=:i", h=hub_id, i=item)
    if not stock or stock[0]["qty"] < qty:
        raise ActionError("Not enough stock at that hub.")
    n = read("SELECT COUNT(DISTINCT shipment_id) AS n FROM supply_movement")[0]["n"]
    sid = f"SHP-{int(n) + 1:04d}"
    add_ledger_entry(sid, item, qty, hub, destination, "ALLOCATED")
    write("UPDATE inventory SET qty = qty - :q WHERE hub_id=:h AND item=:i", q=qty, h=hub_id, i=item)
    if stock[0]["qty"] - qty == 0:
        log_event("RESOURCE_DEPLETED", f"{hub} has no {item} left")
    return sid, hub


def _event_value(message, field):
    """Read one field=value token from an event message."""
    text = str(message or "")
    token = f"{field}="
    start = text.find(token)
    if start < 0:
        return None
    rest = text[start + len(token):]
    end = rest.find(";")
    value = rest if end < 0 else rest[:end]
    value = value.strip()
    return value or None


def _event_int(message, field):
    """Read a positive integer field from an event message."""
    raw = _event_value(message, field)
    if raw is None or not raw.isdigit():
        return None
    return int(raw)


def _assignment_marker(message):
    """Read the plan key stored on a patient-assignment event, if this message has one."""
    return _event_value(message, "key")


def _recorded_patients(incident_id):
    """Patients already recorded for one incident. Missing fields are not counted as zero."""
    total = 0
    for row in read("SELECT message FROM events WHERE type='PATIENT_ASSIGNED'"):
        if _event_int(row["message"], "incident") != int(incident_id):
            continue
        count = _event_int(row["message"], "patients")
        if count is None:
            continue
        total += count
    return total


def record_patient_assignment(plan_key):
    """Record one proposed patient assignment and reduce that hospital's free beds.

    This is a simulated command-centre record. It does not dispatch a patient or a responder.
    The bed update and the audit event commit together, or neither is kept.
    """
    plan = get_plan()
    move = next((row for row in plan["patients"] if row.get("key") == plan_key), None)
    if move is None:
        raise ActionError("That assignment is no longer in the plan. The plan was updated. Beds were not changed.")
    count = move.get("patients")
    hospital_id = move.get("hospital_id")
    hospital_name = move.get("hospital")
    incident_id = move.get("incident_id")
    if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
        raise ActionError("The assignment has no valid patient count. Beds were not changed.")
    if hospital_id is None or not hospital_name:
        raise ActionError("The assignment has no recorded hospital. Beds were not changed.")
    if incident_id is None:
        raise ActionError("The assignment has no recorded incident. Beds were not changed.")
    with transaction():
        hospitals = read("SELECT * FROM hospitals WHERE id=:i", i=hospital_id)
        if not hospitals or hospitals[0]["name"] != hospital_name:
            raise ActionError("That hospital is not recorded. Beds were not changed.")
        free = hospitals[0].get("available_beds")
        if free is None or isinstance(free, bool) or int(free) < count:
            shown = "not available" if free is None else free
            raise ActionError(f"{hospital_name} has {shown} free beds recorded. {count} patients were not recorded.")
        incidents = read("SELECT * FROM incidents WHERE id=:i", i=incident_id)
        if not incidents or incidents[0].get("need_type") != "Medical" or incidents[0].get("status") == "RESOLVED":
            raise ActionError("That medical request is not an open incident. Beds were not changed.")
        people = incidents[0].get("people")
        if people is not None and count > int(people):
            raise ActionError("The assignment has more patients than the incident records. Beds were not changed.")
        for row in read("SELECT message FROM events WHERE type='PATIENT_ASSIGNED'"):
            if _assignment_marker(row["message"]) == plan_key:
                raise ActionError("This assignment was already recorded. Beds were not changed again.")
        already = _recorded_patients(incident_id)
        if people is not None and already + count > int(people):
            raise ActionError(
                f"SOS #{int(incident_id)} already has {already} patients recorded. "
                f"{count} more were not recorded. Beds were not changed.")
        remaining = int(free) - count
        write("UPDATE hospitals SET available_beds=:a WHERE id=:i", a=remaining, i=hospital_id)
        event("PATIENT_ASSIGNED",
              f"Recorded {count} patients from SOS #{int(incident_id)} at {hospital_name}. "
              f"Free beds are now {remaining}. This does not dispatch a patient or a responder. "
              f"key={plan_key}; incident={int(incident_id)}; hospital_id={int(hospital_id)}; patients={count}",
              do_replan=True)


def approve(plan_key):  # approve one planned move, or keep the database unchanged
    move = next((m for m in get_plan()["supply"] if m["key"] == plan_key), None)
    if move is None:
        raise ActionError("That move is no longer in the plan. The plan was updated.")
    with transaction():
        sid, hub = _create_shipment(move["item"], move["qty"], move["hub_id"], move["zone"])
        event("SHIPMENT_ALLOCATED", f"{sid}: {move['qty']} {move['item']} from {hub} to {move['zone']}")


def approve_all():  # approve every planned move, or keep the database unchanged
    plan = get_plan()
    with transaction():
        ids = [_create_shipment(m["item"], m["qty"], m["hub_id"], m["zone"])[0] for m in plan["supply"]]
        event("SHIPMENT_ALLOCATED", f"{len(ids)} shipments allocated from plan v{plan['version']}")


def advance_shipment(shipment_id):  # next step for a shipment (adds a NEW ledger row)
    r = read("SELECT * FROM supply_movement WHERE shipment_id=:s ORDER BY entry_id DESC LIMIT 1", s=shipment_id)[0]
    flow = engine.STATUS_FLOW
    pos = flow.index(r["status"]) if r["status"] in flow else 0
    if pos == len(flow) - 1:
        raise ActionError("Shipment already distributed.")
    nxt = flow[pos + 1]
    add_ledger_entry(shipment_id, r["item"], r["qty"], r["source_hub"], r["destination"], nxt)
    event("SHIPMENT_" + nxt, f"{shipment_id} is now {nxt}", do_replan=(nxt == "DISTRIBUTED"))


# ---------------------------------------------------------------------
# SCENARIOS (from our notes) and demo buttons
# ---------------------------------------------------------------------
SCENARIOS = {
    "A": ("Normal conditions", "Reset to the starting situation"),
    "B": ("Hospital capacity decreases", "Ratnagiri Civil Hospital fills up"),
    "C": ("Major road blocked", "Roads R8 and R9 out of Kolhapur Depot are cut"),
    "D": ("New relief resources arrive", "Extra food and medical kits reach Kolhapur Depot"),
    "E": ("Several changes at once", "Cyclone worsens, road R6 blocked, Thane Civil fills, new patients in Raigad"),
}


def _risk(zone, dtype, prob):  # fake scraper reading
    lat, lon = demo.zone_position(zone)
    write("""INSERT INTO risk_logs(timestamp, zone, lat, lon, disaster_type, probability, source)
             VALUES (:t, :z, :la, :lo, :d, :p, 'IMD (demo)')""", t=now(), z=zone, la=lat, lo=lon, d=dtype, p=prob)


def run_scenario(sid):  # change the world, re-plan, check the plan
    if sid == "A":
        reset_db()
        result = event("SCENARIO_A", "Scenario A: normal conditions")
    elif sid == "B":
        write("UPDATE hospitals SET available_beds=0, icu_available=0 WHERE id=4")
        result = event("HOSPITAL_CAPACITY_CHANGED", "Scenario B: Ratnagiri Civil Hospital is full")
    elif sid == "C":
        write("UPDATE roads SET status='BLOCKED' WHERE id IN ('R8', 'R9')")
        result = event("ROAD_BLOCKED", "Scenario C: roads R8 and R9 out of Kolhapur Depot blocked")
    elif sid == "D":
        prior = get_plan()
        snap = situation()
        projected = [dict(row) for row in snap["inventory"]]

        def bump(item, qty):  # stock Scenario D is about to add, used only to judge the check
            for row in projected:
                if row["hub_id"] == 4 and row["item"] == item:
                    row["qty"] = int(row["qty"]) + qty
                    return
            projected.append({"hub_id": 4, "item": item, "qty": qty})

        bump("Food kits", 1500)
        bump("Medical kits", 600)
        useful = engine.stock_can_improve(prior["supply"], projected, snap["hubs"], snap["travel"], prior["shortfalls"])
        write("UPDATE inventory SET qty = qty + 1500 WHERE hub_id=4 AND item='Food kits'")
        write("UPDATE inventory SET qty = qty + 600 WHERE hub_id=4 AND item='Medical kits'")
        result = event("RESOURCE_RECEIVED", "Scenario D: 1500 food kits and 600 medical kits reached Kolhapur Depot")
        result["stock_was_useful"] = useful
    else:
        _risk("Ratnagiri", "Cyclone", 0.95)
        _risk("Raigad", "Cyclone", 0.85)
        write("UPDATE roads SET status='BLOCKED' WHERE id='R6'")
        write("UPDATE hospitals SET available_beds=0, icu_available=0 WHERE id=2")
        lat, lon = demo.zone_position("Raigad")
        write("""INSERT INTO incidents(reported_at, lat, lon, zone, need_type, people, status)
                 VALUES (:t, :la, :lo, 'Raigad', 'Medical', 40, 'OPEN')""", t=now(), la=lat + 0.03, lo=lon + 0.02)
        write("""INSERT INTO alerts(created_at, zone, disaster_type, severity, eta_hours, radius_km, sms_status)
                 VALUES (:t, 'Raigad', 'Cyclone', 'Red', 16, 50, 'QUEUED')""", t=now())
        result = event("MULTIPLE_EVENTS", "Scenario E: cyclone worsens, R6 blocked, Thane Civil fills, 40 new patients in Raigad")
    report = checks(result, expect_change=sid != "A")
    if sid == "D":
        current = get_plan()
        still = engine.stock_can_improve(current["supply"], result["situation"]["inventory"], result["situation"]["hubs"],
                                         result["situation"]["travel"], current["shortfalls"])
        if result.get("stock_was_useful"):
            report[0] = {"check": "New stock shortens a trip or fills a shortfall",
                         "ok": result["changed"] > 0 and not still,
                         "detail": f"{result['changed']} allocations changed"}
        else:
            report[0] = {"check": "Plan kept when the new stock is not closer", "ok": not still,
                         "detail": "No shipped route gets shorter, and no shortfall becomes reachable"}
    return {"scenario": sid, "title": SCENARIOS[sid][0], "version": result["version"], "changed": result["changed"],
            "checks": report}


def demo_cyclone():  # prediction demo
    _risk("Ratnagiri", "Cyclone", 0.95)
    _risk("Sindhudurg", "Cyclone", 0.85)
    write("""INSERT INTO alerts(created_at, zone, disaster_type, severity, eta_hours, radius_km, sms_status)
             VALUES (:t, 'Sindhudurg', 'Cyclone', 'Red', 18, 50, 'QUEUED')""", t=now())
    event("RISK_UPDATED", "Scraper: cyclone risk jumped near Ratnagiri. Auto alert queued for Sindhudurg")


def demo_tamper():  # secretly edit an old ledger record
    write("UPDATE supply_movement SET qty = qty + 500 WHERE entry_id = (SELECT MIN(entry_id) FROM supply_movement)")
    event("TAMPER", "Someone edited a past supply record directly in the database", do_replan=False)


# ---------------------------------------------------------------------
# Demo data, create, reset
# ---------------------------------------------------------------------
def seed_demo_data():  # fill an empty database from demo_data.py
    start = datetime.now()
    for name, lat, lon, pop, vul in demo.ZONES:
        write("INSERT INTO zones VALUES (:n, :la, :lo, :p, :v)", n=name, la=lat, lo=lon, p=pop, v=vul)
    for hid, name, lat, lon in demo.HUBS:
        write("INSERT INTO hubs VALUES (:i, :n, :la, :lo)", i=hid, n=name, la=lat, lo=lon)
        for item, qty in demo.INVENTORY[hid].items():
            write("INSERT INTO inventory VALUES (:h, :i, :q)", h=hid, i=item, q=qty)
    for h in demo.HOSPITALS:
        write("INSERT INTO hospitals VALUES (:i, :n, :z, :la, :lo, :tb, :ab, :it, :ia, :ox)",
              i=h[0], n=h[1], z=h[2], la=h[3], lo=h[4], tb=h[5], ab=h[6], it=h[7], ia=h[8], ox=h[9])
    for rid, a, b, mins in demo.ROADS:
        write("INSERT INTO roads VALUES (:i, :a, :b, :m, 'OPEN')", i=rid, a=a, b=b, m=mins)
    for ts, zone, lat, lon, dtype, prob, src in demo.risk_history(start):
        write("""INSERT INTO risk_logs(timestamp, zone, lat, lon, disaster_type, probability, source)
                 VALUES (:t, :z, :la, :lo, :d, :p, :s)""", t=ts, z=zone, la=lat, lo=lon, d=dtype, p=prob, s=src)
    for ts, lat, lon, zone, need, people in demo.incident_rows(start):
        write("""INSERT INTO incidents(reported_at, lat, lon, zone, need_type, people, status)
                 VALUES (:t, :la, :lo, :z, :n, :p, 'OPEN')""", t=ts, la=lat, lo=lon, z=zone, n=need, p=people)
    write("""INSERT INTO alerts(created_at, zone, disaster_type, severity, eta_hours, radius_km, sms_status)
             VALUES (:t, 'Ratnagiri', 'Cyclone', 'Red', 22, 40, 'SENT')""", t=now())
    add_ledger_entry("SHP-0001", "Medical kits", 150, "Kolhapur Depot", "Ratnagiri", "ALLOCATED")
    add_ledger_entry("SHP-0001", "Medical kits", 150, "Kolhapur Depot", "Ratnagiri", "DISPATCHED")
    log_event("SYSTEM", "System started with demo data")
    replan("SYSTEM_START", "System started")


def init_db():  # create tables; add demo data if empty
    conn = sqlite3.connect(get_db_path())
    conn.executescript(SCHEMA)
    from intake_agent import INTAKE_SCHEMA  # new table only; existing tables are not dropped
    conn.executescript(INTAKE_SCHEMA)
    conn.commit()
    conn.close()
    if read("SELECT COUNT(*) AS n FROM zones")[0]["n"] == 0:
        seed_demo_data()
    from intake_agent import ensure_schema  # add score columns on a database that already has reports
    ensure_schema()
    ensure_telemetry_schema()


def reset_db():  # delete the file and start again
    path = get_db_path()
    if os.path.exists(path):
        os.remove(path)
    init_db()



# =====================================================================
# REAL-TIME API & CAP 1.2 XML TELEMETRY INTEGRATION
# ---------------------------------------------------------------------
# Connects Saarthi directly to official Indian and global feeds using
# Python's standard library (zero extra pip packages required):
#   1. NDMA SACHET CAP 1.2 XML & RSS Feeds (sachet.ndma.gov.in)
#      - Implements mandatory HTTP ETag (If-None-Match / 304 Not Modified)
#        client-side caching per NDMA's CAP XML Integration Guide.
#   2. GDACS Multi-Hazard RSS / CAP XML Feed (gdacs.org/xml/rss.xml)
#   3. Open-Meteo Forecast API (api.open-meteo.com/v1/forecast) &
#      IMD API Gateway (api.imd.gov.in) for live zone risk probabilities
#      plus live temperature (°C), wind speed/gusts (km/h), rainfall (mm),
#      and surface pressure (hPa).
#   4. USGS Earthquake Hazards GeoJSON API (earthquake.usgs.gov) &
#      NASA FIRMS / EONET Active Fire APIs for Intake & Triage reports
#   5. OpenRouteService Directions API / OSRM Routing API for live
#      corridor travel minutes and road conditions across R1-R20.
# =====================================================================

TELEMETRY_SCHEMA = """
CREATE TABLE IF NOT EXISTS cap_feed_logs(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fetched_at TEXT,
    feed_source TEXT,
    identifier TEXT,
    sender TEXT,
    event TEXT,
    severity TEXT,
    urgency TEXT,
    area_desc TEXT,
    matched_zone TEXT,
    headline TEXT,
    pub_date TEXT,
    xml_url TEXT,
    etag_status TEXT
);
CREATE TABLE IF NOT EXISTS volunteer_teams(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    registered_at TEXT,
    team_name TEXT,
    team_lead TEXT,
    head_role TEXT,
    home_zone TEXT,
    focus_area TEXT,
    capacity INTEGER,
    status TEXT
);
CREATE TABLE IF NOT EXISTS rescue_tasks(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT,
    title TEXT,
    zone TEXT,
    lat REAL,
    lon REAL,
    priority TEXT,
    people INTEGER,
    location_note TEXT,
    status TEXT,
    assigned_team_id INTEGER,
    assigned_team_name TEXT
);
CREATE TABLE IF NOT EXISTS volunteer_assignments(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    assigned_at TEXT,
    updated_at TEXT,
    task_id INTEGER,
    task_title TEXT,
    zone TEXT,
    priority TEXT,
    people INTEGER,
    team_id INTEGER,
    team_name TEXT,
    team_lead TEXT,
    volunteers INTEGER,
    status TEXT
);
CREATE TABLE IF NOT EXISTS volunteer_status_history(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT,
    assignment_id INTEGER,
    task_id INTEGER,
    task_title TEXT,
    team_id INTEGER,
    team_name TEXT,
    status TEXT,
    note TEXT
);
"""


def ensure_telemetry_schema():
    """Add live weather telemetry columns to risk_logs, CAP metadata columns to alerts,
    and create cap_feed_logs and volunteer tables without altering existing rows or breaking tests."""
    for stmt in TELEMETRY_SCHEMA.strip().split(";"):
        if stmt.strip():
            write(stmt.strip())
    risk_cols = {row["name"] for row in read("PRAGMA table_info(risk_logs)")}
    for col_name, col_type in (
        ("temp_c", "REAL"),
        ("wind_kmh", "REAL"),
        ("gusts_kmh", "REAL"),
        ("precip_mm", "REAL"),
        ("pressure_hpa", "REAL"),
    ):
        if col_name not in risk_cols:
            write(f"ALTER TABLE risk_logs ADD COLUMN {col_name} {col_type}")

    alert_cols = {row["name"] for row in read("PRAGMA table_info(alerts)")}
    for col_name, col_type in (
        ("source", "TEXT"),
        ("cap_identifier", "TEXT"),
        ("cap_sender", "TEXT"),
        ("cap_headline", "TEXT"),
        ("cap_area_desc", "TEXT"),
    ):
        if col_name not in alert_cols:
            write(f"ALTER TABLE alerts ADD COLUMN {col_name} {col_type}")
    write("UPDATE alerts SET source='Seeded / Operator' WHERE source IS NULL")


SACHET_MH_RSS_URL = os.environ.get(
    "SAARTHI_SACHET_MH_RSS",
    "https://sachet.ndma.gov.in/cap_public_website/rss/rss_maharashtra.xml",
)
SACHET_INDIA_RSS_URL = os.environ.get(
    "SAARTHI_SACHET_INDIA_RSS",
    "https://sachet.ndma.gov.in/cap_public_website/rss/rss_india.xml",
)
GDACS_RSS_URL = os.environ.get(
    "SAARTHI_GDACS_RSS",
    "https://www.gdacs.org/xml/rss.xml",
)
OPEN_METEO_URL = os.environ.get(
    "SAARTHI_OPEN_METEO_URL",
    "https://api.open-meteo.com/v1/forecast",
)
IMD_DISTRICT_API_URL = os.environ.get(
    "SAARTHI_IMD_API_URL",
    "https://mausam.imd.gov.in/imd_latest/contents/districtwisewarnings_mc.php",
)
USGS_GEOJSON_URL = os.environ.get(
    "SAARTHI_USGS_URL",
    "https://earthquake.usgs.gov/fdsnws/event/1/query",
)
NASA_EONET_URL = os.environ.get(
    "SAARTHI_NASA_EONET_URL",
    "https://eonet.gsfc.nasa.gov/api/v3/events",
)
NASA_FIRMS_AREA_URL = os.environ.get(
    "SAARTHI_NASA_FIRMS_URL",
    "https://firms.modaps.eosdis.nasa.gov/api/area/csv",
)
OSRM_ROUTE_URL = os.environ.get(
    "SAARTHI_OSRM_URL",
    "https://router.project-osrm.org/route/v1/driving",
)
ORS_DIRECTIONS_URL = os.environ.get(
    "SAARTHI_ORS_URL",
    "https://api.openrouteservice.org/v2/directions/driving-car",
)

CAP_NS = {"cap": "urn:oasis:names:tc:emergency:cap:1.2"}
GDACS_NS = {
    "gdacs": "http://www.gdacs.org",
    "geo": "http://www.w3.org/2003/01/geo/wgs84_pos#",
}

# Client-side ETag cache: url -> (etag_str, raw_bytes)
# Required by NDMA SACHET CAP XML Feed Integration Guide for Agencies
_ETAG_CACHE = {}

# District alias lookup for matching SACHET CAP areaDesc / IMD bulletins to Saarthi zones
ZONE_ALIASES = {
    "Mumbai": ("mumbai", "mumbai city", "mumbai suburban", "greater mumbai", "bombay", "konkan"),
    "Thane": ("thane", "navi mumbai", "kalyan", "bhiwandi", "ulhasnagar", "konkan"),
    "Palghar": ("palghar", "dahanu", "vasai", "jawhar", "north konkan"),
    "Raigad": ("raigad", "raigarh", "alibag", "panvel", "mahad", "roha", "konkan"),
    "Ratnagiri": ("ratnagiri", "chiplun", "dapoli", "rajapur", "south konkan", "konkan"),
    "Sindhudurg": ("sindhudurg", "kudal", "sawantwadi", "malvan", "vengurla", "south konkan", "konkan"),
    "Pune": ("pune", "pimpri", "chinchwad", "baramati", "lonavala", "ghat areas of pune", "madhya maharashtra"),
}

CAP_SEVERITY_TO_SAARTHI = {
    "extreme": "Red",
    "severe": "Orange",
    "moderate": "Yellow",
    "minor": "Yellow",
    "unknown": "Yellow",
}


def fetch_with_etag(url, timeout=6, extra_headers=None):
    """Fetch an XML/JSON endpoint with HTTP ETag (If-None-Match) caching.

    Follows the NDMA SACHET CAP XML Integration Guide:
      - Step 1: First GET request stores the XML/JSON payload and ETag header.
      - Step 2: Subsequent GET requests send If-None-Match: <stored-etag>.
      - Step 3: On HTTP 304 Not Modified, returns the cached bytes immediately.
    """
    headers = {
        "User-Agent": "Saarthi-SEOC-CommandCentre/1.0 (Maharashtra Disaster Management)",
        "Accept": "application/xml, text/xml, application/rss+xml, application/json, */*",
    }
    if extra_headers:
        headers.update(extra_headers)
    cached_etag, cached_body = _ETAG_CACHE.get(url, (None, None))
    if cached_etag:
        headers["If-None-Match"] = cached_etag

    req = urllib.request.Request(url, headers=headers)
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            body = resp.read()
            new_etag = resp.headers.get("ETag") or resp.headers.get("etag")
            if new_etag:
                _ETAG_CACHE[url] = (new_etag, body)
            elif body:
                _ETAG_CACHE[url] = (None, body)
            return body, False, (new_etag or "200 OK (no ETag)")
    except urllib.error.HTTPError as exc:
        if exc.code == 304 and cached_body is not None:
            return cached_body, True, f"304 Not Modified ({cached_etag})"
        raise


def _haversine_km(lat1, lon1, lat2, lon2):
    """Great-circle distance in km between two WGS84 coordinates."""
    r = 6371.0
    p1, p2 = math.radians(float(lat1)), math.radians(float(lat2))
    dlat = math.radians(float(lat2) - float(lat1))
    dlon = math.radians(float(lon2) - float(lon1))
    a = math.sin(dlat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2) ** 2
    return 2 * r * math.asin(math.sqrt(max(0.0, min(1.0, a))))


def nearest_monitored_zone(lat, lon, max_km=250.0):
    """Return (zone_name, distance_km) for the closest monitored Saarthi zone, or (None, None)."""
    zones = read("SELECT name, lat, lon FROM zones")
    best_name, best_dist = None, None
    for z in zones:
        if z.get("lat") is None or z.get("lon") is None:
            continue
        dist = _haversine_km(lat, lon, z["lat"], z["lon"])
        if best_dist is None or dist < best_dist:
            best_name, best_dist = z["name"], dist
    if best_dist is not None and best_dist <= max_km:
        return best_name, round(best_dist, 1)
    return None, None


def map_cap_hazard(event_text):
    """Map a CAP 1.2 <cap:event> or RSS category string to a valid Saarthi DISASTER_TYPES value."""
    text = str(event_text or "").lower()
    if any(w in text for w in ("cyclone", "cyclonic", "squall", "gale", "storm surge", "high wind", "typhoon", "hurricane")):
        return "Cyclone"
    if any(w in text for w in ("heat", "heatwave", "heat wave", "warm night", "hot weather")):
        return "Heatwave"
    if any(w in text for w in ("landslide", "mudslide", "landslip", "rockfall", "avalanche")):
        return "Landslide"
    if any(w in text for w in ("earthquake", "seismic", "tremor", "quake", "tsunami")):
        return "Earthquake"
    return "Flood"  # Heavy rain, flash flood, thunderstorm, lightning, inundation


def _match_zones_in_text(area_text):
    """Match CAP <cap:areaDesc> or headline text to monitored Maharashtra zones."""
    lowered = str(area_text or "").lower()
    matched = []
    for zone_name, aliases in ZONE_ALIASES.items():
        if any(alias in lowered for alias in aliases):
            matched.append(zone_name)
    return matched


def _parse_cap_eta_hours(info_elem):
    """Compute expected hours to impact/expiry from CAP <cap:onset>, <cap:effective>, or <cap:expires>."""
    for tag in ("cap:onset", "cap:effective", "cap:expires"):
        raw = info_elem.findtext(tag, default="", namespaces=CAP_NS).strip()
        if not raw:
            continue
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            now_utc = datetime.now(timezone.utc)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            diff_h = (dt - now_utc).total_seconds() / 3600.0
            if 1.0 <= diff_h <= 72.0:
                return int(round(diff_h))
        except Exception:
            continue
    return 12


def _parse_cap_polygon_centroid(area_elem):
    """Extract (lat, lon, radius_km) from a CAP <cap:polygon> or <cap:circle> element if present."""
    circle_raw = area_elem.findtext("cap:circle", default="", namespaces=CAP_NS).strip()
    if circle_raw:
        try:
            coord_part, rad_part = circle_raw.split()
            lat_s, lon_s = coord_part.split(",")
            return float(lat_s), float(lon_s), max(5, min(100, int(round(float(rad_part)))))
        except Exception:
            pass
    poly_raw = area_elem.findtext("cap:polygon", default="", namespaces=CAP_NS).strip()
    if poly_raw:
        pts = []
        for pair in poly_raw.split():
            if "," in pair:
                try:
                    la, lo = pair.split(",")[:2]
                    pts.append((float(la), float(lo)))
                except Exception:
                    continue
        if pts:
            avg_lat = sum(p[0] for p in pts) / len(pts)
            avg_lon = sum(p[1] for p in pts) / len(pts)
            max_d = max((_haversine_km(avg_lat, avg_lon, p[0], p[1]) for p in pts), default=35.0)
            return avg_lat, avg_lon, max(10, min(100, int(round(max_d or 35.0))))
    return None, None, 35


def parse_sachet_cap_xml(xml_bytes, include_unmatched=False):
    """Parse an OASIS CAP 1.2 XML document from NDMA SACHET into structured alert dicts."""
    root = ET.fromstring(xml_bytes)
    identifier = (
        root.findtext("cap:identifier", default="", namespaces=CAP_NS)
        or root.findtext("{*}identifier", default="")
    ).strip()
    sender = (
        root.findtext("cap:sender", default="NDMA-SACHET", namespaces=CAP_NS)
        or root.findtext("{*}sender", default="NDMA-SACHET")
    ).strip()
    sent_time = (
        root.findtext("cap:sent", default="", namespaces=CAP_NS)
        or root.findtext("{*}sent", default="")
    ).strip()

    info_nodes = root.findall("cap:info", CAP_NS) or root.findall("{*}info")
    parsed_alerts = []
    for info in info_nodes:
        event_name = (
            info.findtext("cap:event", default="", namespaces=CAP_NS)
            or info.findtext("{*}event", default="Heavy Rain")
        ).strip()
        sev_raw = (
            info.findtext("cap:severity", default="Moderate", namespaces=CAP_NS)
            or info.findtext("{*}severity", default="Moderate")
        ).strip().lower()
        urgency_raw = (
            info.findtext("cap:urgency", default="Expected", namespaces=CAP_NS)
            or info.findtext("{*}urgency", default="Expected")
        ).strip()
        headline = (
            info.findtext("cap:headline", default="", namespaces=CAP_NS)
            or info.findtext("{*}headline", default=event_name)
        ).strip()
        instruction = (
            info.findtext("cap:instruction", default="", namespaces=CAP_NS)
            or info.findtext("{*}instruction", default="")
        ).strip()

        area_nodes = info.findall("cap:area", CAP_NS) or info.findall("{*}area")
        area_texts = []
        centroid_lat, centroid_lon, radius_km = None, None, 35
        for area in area_nodes:
            desc = (
                area.findtext("cap:areaDesc", default="", namespaces=CAP_NS)
                or area.findtext("{*}areaDesc", default="")
            ).strip()
            if desc:
                area_texts.append(desc)
            c_lat, c_lon, c_rad = _parse_cap_polygon_centroid(area)
            if c_lat is not None and centroid_lat is None:
                centroid_lat, centroid_lon, radius_km = c_lat, c_lon, c_rad

        combined_area = " ; ".join(area_texts) + " " + headline
        matched_zones = _match_zones_in_text(combined_area)
        if not matched_zones and centroid_lat is not None and centroid_lon is not None:
            near_zone, _ = nearest_monitored_zone(centroid_lat, centroid_lon, max_km=180.0)
            if near_zone:
                matched_zones = [near_zone]

        hazard = map_cap_hazard(event_name + " " + headline)
        severity_word = CAP_SEVERITY_TO_SAARTHI.get(sev_raw, "Yellow")
        eta_hours = _parse_cap_eta_hours(info)
        area_str = " ; ".join(area_texts) or "Maharashtra / Regional"

        if matched_zones:
            for z_name in matched_zones:
                parsed_alerts.append({
                    "identifier": identifier,
                    "sender": sender,
                    "sent": sent_time,
                    "zone": z_name,
                    "disaster_type": hazard,
                    "event": event_name or hazard,
                    "severity": severity_word,
                    "cap_severity": sev_raw.capitalize(),
                    "urgency": urgency_raw,
                    "eta_hours": eta_hours,
                    "radius_km": radius_km,
                    "headline": headline or event_name,
                    "instruction": instruction,
                    "area_desc": area_str,
                })
        elif include_unmatched:
            parsed_alerts.append({
                "identifier": identifier,
                "sender": sender,
                "sent": sent_time,
                "zone": None,
                "disaster_type": hazard,
                "event": event_name or hazard,
                "severity": severity_word,
                "cap_severity": sev_raw.capitalize(),
                "urgency": urgency_raw,
                "eta_hours": eta_hours,
                "radius_km": radius_km,
                "headline": headline or event_name,
                "instruction": instruction,
                "area_desc": area_str,
            })
    return parsed_alerts


def sync_sachet_cap_xml(max_items=12):
    """Poll NDMA SACHET Maharashtra & All-India RSS + CAP 1.2 XML feeds with ETag caching.

    Stores every fetched CAP 1.2 XML / RSS item in `cap_feed_logs` for table inspection,
    queues matching Maharashtra district alerts in `alerts`, and records CAP risk readings in `risk_logs`.
    """
    ensure_telemetry_schema()
    inserted_alerts = 0
    inserted_risks = 0
    logged_xml_items = 0
    etags_reused = 0
    feeds_checked = []

    existing_alerts = {
        (r["zone"], r["disaster_type"], r["severity"])
        for r in read("SELECT zone, disaster_type, severity FROM alerts WHERE sms_status='QUEUED'")
    }
    existing_feed_ids = {
        (r["feed_source"], r["identifier"], r["headline"])
        for r in read("SELECT feed_source, identifier, headline FROM cap_feed_logs")
    }

    for feed_label, rss_url in (("NDMA SACHET Maharashtra RSS", SACHET_MH_RSS_URL), ("NDMA SACHET All-India RSS", SACHET_INDIA_RSS_URL)):
        try:
            rss_bytes, was_304, rss_etag_info = fetch_with_etag(rss_url, timeout=6)
            if was_304:
                etags_reused += 1
            feeds_checked.append(feed_label)
            rss_root = ET.fromstring(rss_bytes)
        except Exception:
            continue

        items = rss_root.findall(".//item") or rss_root.findall(".//{*}item")
        for item in items[:max_items]:
            title = (item.findtext("title") or item.findtext("{*}title") or "").strip()
            link = (item.findtext("link") or item.findtext("{*}link") or item.findtext("guid") or "").strip()
            category = (item.findtext("category") or item.findtext("{*}category") or "").strip()
            pub_date = (item.findtext("pubDate") or item.findtext("{*}pubDate") or now()).strip()
            guid = (item.findtext("guid") or item.findtext("{*}guid") or link).strip()
            ident = guid.split("identifier=")[-1] if "identifier=" in guid else guid[-24:]

            # 1. If link points to FetchXMLFile?identifier=..., fetch the full CAP 1.2 XML with ETag
            cap_records = []
            etag_status = rss_etag_info
            if "FetchXMLFile" in link:
                try:
                    cap_bytes, cap_304, cap_etag_info = fetch_with_etag(link, timeout=6)
                    etag_status = cap_etag_info
                    if cap_304:
                        etags_reused += 1
                    cap_records = parse_sachet_cap_xml(cap_bytes, include_unmatched=True)
                except Exception:
                    cap_records = []

            # 2. Fallback to RSS item metadata if FetchXMLFile was not reachable or had no sub-blocks
            if not cap_records:
                matched_zones = _match_zones_in_text(title + " " + category)
                hazard = map_cap_hazard(title + " " + category)
                sev_word = "Red" if any(k in title.lower() for k in ("red", "extreme", "very heavy")) else (
                    "Orange" if any(k in title.lower() for k in ("orange", "severe", "heavy")) else "Yellow"
                )
                if matched_zones:
                    for z_name in matched_zones:
                        cap_records.append({
                            "identifier": ident,
                            "sender": "NDMA SACHET RSS",
                            "sent": pub_date,
                            "zone": z_name,
                            "disaster_type": hazard,
                            "event": category or hazard,
                            "severity": sev_word,
                            "cap_severity": sev_word,
                            "urgency": "Expected",
                            "eta_hours": 12,
                            "radius_km": 35,
                            "headline": title or f"NDMA SACHET {hazard} Advisory",
                            "area_desc": z_name,
                        })
                else:
                    cap_records.append({
                        "identifier": ident,
                        "sender": "NDMA SACHET RSS",
                        "sent": pub_date,
                        "zone": None,
                        "disaster_type": hazard,
                        "event": category or hazard,
                        "severity": sev_word,
                        "cap_severity": sev_word,
                        "urgency": "Expected",
                        "eta_hours": 12,
                        "radius_km": 35,
                        "headline": title or f"NDMA SACHET {hazard} Advisory",
                        "area_desc": "Maharashtra / National Advisory",
                    })

            for rec in cap_records:
                feed_sig = (feed_label, str(rec.get("identifier") or ident), str(rec.get("headline") or title))
                if feed_sig not in existing_feed_ids:
                    write(
                        """INSERT INTO cap_feed_logs(fetched_at, feed_source, identifier, sender, event, severity,
                           urgency, area_desc, matched_zone, headline, pub_date, xml_url, etag_status)
                           VALUES (:fa, :fs, :id, :sn, :ev, :sv, :ur, :ar, :mz, :hl, :pd, :xu, :et)""",
                        fa=now(),
                        fs=feed_label,
                        id=str(rec.get("identifier") or ident),
                        sn=str(rec.get("sender") or "NDMA-SACHET"),
                        ev=str(rec.get("event") or rec["disaster_type"]),
                        sv=str(rec.get("cap_severity") or rec["severity"]),
                        ur=str(rec.get("urgency") or "Expected"),
                        ar=str(rec.get("area_desc") or "Maharashtra"),
                        mz=rec.get("zone") or "Regional (Outside 7 Zones)",
                        hl=str(rec.get("headline") or title),
                        pd=str(rec.get("sent") or pub_date),
                        xu=link or rss_url,
                        et=str(etag_status),
                    )
                    existing_feed_ids.add(feed_sig)
                    logged_xml_items += 1

                if not rec.get("zone"):
                    continue

                sig = (rec["zone"], rec["disaster_type"], rec["severity"])
                if sig not in existing_alerts:
                    write(
                        """INSERT INTO alerts(created_at, zone, disaster_type, severity, eta_hours, radius_km,
                           sms_status, source, cap_identifier, cap_sender, cap_headline, cap_area_desc)
                           VALUES (:t, :z, :d, :s, :e, :r, 'QUEUED', 'NDMA SACHET CAP XML', :ci, :cs, :ch, :ca)""",
                        t=now(), z=rec["zone"], d=rec["disaster_type"], s=rec["severity"],
                        e=rec["eta_hours"], r=rec["radius_km"],
                        ci=str(rec.get("identifier") or ident),
                        cs=str(rec.get("sender") or "NDMA-SACHET"),
                        ch=str(rec.get("headline") or title),
                        ca=str(rec.get("area_desc") or rec["zone"]),
                    )
                    existing_alerts.add(sig)
                    inserted_alerts += 1

                cap_prob = 0.88 if rec["severity"] == "Red" else (0.68 if rec["severity"] == "Orange" else 0.45)
                lat, lon = demo.zone_position(rec["zone"])
                write(
                    """INSERT INTO risk_logs(timestamp, zone, lat, lon, disaster_type, probability, source)
                       VALUES (:t, :z, :la, :lo, :d, :p, 'NDMA SACHET CAP XML')""",
                    t=now(), z=rec["zone"], la=lat, lo=lon, d=rec["disaster_type"], p=cap_prob,
                )
                inserted_risks += 1

        if logged_xml_items > 0 and feed_label == "NDMA SACHET Maharashtra RSS":
            break

    status_msg = (
        f"NDMA SACHET CAP XML sync: {len(feeds_checked)} feed(s) polled, "
        f"{logged_xml_items} CAP XML item(s) parsed, {inserted_alerts} district alert(s) queued, "
        f"{etags_reused} ETag cache hit(s)."
    )
    if inserted_alerts or inserted_risks:
        event("ALERT_QUEUED", status_msg, do_replan=True)
    else:
        log_event("ALERT_QUEUED", status_msg)
    return {
        "source": "NDMA SACHET CAP XML",
        "feeds_polled": feeds_checked,
        "xml_items_logged": logged_xml_items,
        "alerts_queued": inserted_alerts,
        "risks_logged": inserted_risks,
        "etag_hits": etags_reused,
        "message": status_msg,
    }


def sync_gdacs_xml(max_items=20):
    """Poll the UN/EC GDACS Multi-Hazard CAP/RSS XML feed (https://www.gdacs.org/xml/rss.xml)."""
    ensure_telemetry_schema()
    inserted = 0
    logged_items = 0
    existing_feed_ids = {
        (r["feed_source"], r["identifier"], r["headline"])
        for r in read("SELECT feed_source, identifier, headline FROM cap_feed_logs")
    }
    try:
        xml_bytes, _, etag_status = fetch_with_etag(GDACS_RSS_URL, timeout=6)
        root = ET.fromstring(xml_bytes)
    except Exception as exc:
        msg = f"GDACS XML feed poll skipped/unreachable ({type(exc).__name__}). Stored alerts unchanged."
        log_event("SYSTEM", msg)
        return {"source": "GDACS XML", "alerts_queued": 0, "xml_items_logged": 0, "message": msg}

    items = root.findall(".//item") or root.findall(".//{*}item")
    for item in items[:max_items]:
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub_date = (item.findtext("pubDate") or now()).strip()
        guid = (item.findtext("guid") or title[:32]).strip()
        country = (item.findtext("gdacs:country", default="", namespaces=GDACS_NS) or "").strip()
        ev_type = (item.findtext("gdacs:eventtype", default="", namespaces=GDACS_NS) or "").strip().upper()
        alert_lvl = (item.findtext("gdacs:alertlevel", default="Green", namespaces=GDACS_NS) or "").strip().capitalize()
        lat_txt = item.findtext("geo:lat", default="", namespaces=GDACS_NS)
        lon_txt = item.findtext("geo:long", default="", namespaces=GDACS_NS)

        hazard_map = {"TC": "Cyclone", "FL": "Flood", "EQ": "Earthquake", "DR": "Heatwave", "WF": "Cyclone"}
        hazard = hazard_map.get(ev_type, map_cap_hazard(title))
        sev_word = "Red" if alert_lvl == "Red" else ("Orange" if alert_lvl == "Orange" else "Yellow")

        matched_zone = None
        if lat_txt and lon_txt:
            try:
                matched_zone, _ = nearest_monitored_zone(float(lat_txt), float(lon_txt), max_km=350.0)
            except Exception:
                matched_zone = None
        if not matched_zone and ("india" in country.lower() or "india" in title.lower()):
            zones_found = _match_zones_in_text(title)
            matched_zone = zones_found[0] if zones_found else None

        # Record top GDACS XML feed items in cap_feed_logs so the operator can inspect the live XML stream
        if logged_items < 8 or matched_zone:
            feed_sig = ("GDACS Multi-Hazard XML", guid, title)
            if feed_sig not in existing_feed_ids:
                write(
                    """INSERT INTO cap_feed_logs(fetched_at, feed_source, identifier, sender, event, severity,
                       urgency, area_desc, matched_zone, headline, pub_date, xml_url, etag_status)
                       VALUES (:fa, 'GDACS Multi-Hazard XML', :id, 'UN-OCHA / EC GDACS', :ev, :sv,
                       'Expected', :ar, :mz, :hl, :pd, :xu, :et)""",
                    fa=now(),
                    id=guid,
                    ev=f"{hazard} ({ev_type or 'CAP'})",
                    sv=f"{alert_lvl} ({sev_word})",
                    ar=country or "Global / Indian Ocean",
                    mz=matched_zone or "Global Watch (Outside 7 Zones)",
                    hl=title,
                    pd=pub_date,
                    xu=link or GDACS_RSS_URL,
                    et=str(etag_status),
                )
                existing_feed_ids.add(feed_sig)
                logged_items += 1

        if not matched_zone:
            continue

        write(
            """INSERT INTO alerts(created_at, zone, disaster_type, severity, eta_hours, radius_km,
               sms_status, source, cap_identifier, cap_sender, cap_headline, cap_area_desc)
               VALUES (:t, :z, :d, :s, 18, 50, 'QUEUED', 'GDACS XML', :ci, 'UN/EC GDACS', :ch, :ca)""",
            t=now(), z=matched_zone, d=hazard, s=sev_word,
            ci=guid, ch=title, ca=country or matched_zone,
        )
        inserted += 1

    msg = f"GDACS Multi-Hazard XML sync: {logged_items} XML item(s) logged, {inserted} Maharashtra/Konkan alert(s) matched."
    if inserted:
        event("ALERT_QUEUED", msg, do_replan=True)
    else:
        log_event("SYSTEM", msg)
    return {"source": "GDACS XML", "alerts_queued": inserted, "xml_items_logged": logged_items, "message": msg}


def _derive_weather_hazard(zone_name, temp_c, precip_mm, wind_kmh, gusts_kmh, pressure_hpa, base_hazard):
    """Convert real-time Open-Meteo / IMD meteorological parameters into (disaster_type, probability 0..1)."""
    wind_factor = max(0.0, (float(wind_kmh or 0.0) - 20.0) / 75.0)
    gust_factor = max(0.0, (float(gusts_kmh or 0.0) - 30.0) / 95.0)
    press_drop = max(0.0, (1008.0 - float(pressure_hpa or 1010.0)) / 35.0) if pressure_hpa else 0.0
    cyclone_prob = min(0.98, round(0.45 * gust_factor + 0.35 * wind_factor + 0.20 * press_drop, 2))

    rain_val = float(precip_mm or 0.0)
    flood_prob = min(0.98, round(rain_val / 35.0, 2))

    temp_val = float(temp_c or 28.0)
    heat_prob = min(0.98, round(max(0.0, (temp_val - 33.0) / 14.0), 2))

    landslide_prob = min(0.95, round(flood_prob * 0.85, 2)) if zone_name in ("Raigad", "Ratnagiri", "Sindhudurg", "Pune") else 0.0

    candidates = [
        ("Cyclone", cyclone_prob),
        ("Flood", flood_prob),
        ("Heatwave", heat_prob),
        ("Landslide", landslide_prob),
    ]
    best_type, best_prob = max(candidates, key=lambda item: item[1])
    if best_prob < 0.08:
        micro_prob = round(min(0.45, max(0.06, (float(gusts_kmh or 10.0) / 140.0) + (max(0.0, temp_val - 24.0) / 80.0))), 2)
        return base_hazard, micro_prob
    return best_type, best_prob


def sync_open_meteo_risk():
    """Fetch live meteorological telemetry from Open-Meteo API for all 7 Maharashtra zones
    and store temperature (°C), wind speed (km/h), wind gusts (km/h), precipitation (mm),
    and surface pressure (hPa) directly in `risk_logs`."""
    ensure_telemetry_schema()
    zones = read("SELECT name, lat, lon FROM zones ORDER BY name")
    if not zones:
        return {"source": "Open-Meteo API", "updated_zones": 0, "message": "No monitored zones found."}

    lats = ",".join(f"{z['lat']:.4f}" for z in zones)
    lons = ",".join(f"{z['lon']:.4f}" for z in zones)
    params = urllib.parse.urlencode({
        "latitude": lats,
        "longitude": lons,
        "current": "temperature_2m,precipitation,rain,showers,weather_code,surface_pressure,wind_speed_10m,wind_direction_10m,wind_gusts_10m",
        "timezone": "Asia/Kolkata",
    })
    url = f"{OPEN_METEO_URL}?{params}"
    updated = 0
    details = []

    try:
        raw_bytes, _, _ = fetch_with_etag(url, timeout=6)
        payload = json.loads(raw_bytes.decode("utf-8"))
        records = payload if isinstance(payload, list) else [payload]
        with transaction():
            for z, rec in zip(zones, records):
                cur = (rec or {}).get("current") or {}
                temp_c = float(cur.get("temperature_2m") if cur.get("temperature_2m") is not None else 29.0)
                precip_mm = float(cur.get("precipitation") if cur.get("precipitation") is not None else 0.0)
                wind_kmh = float(cur.get("wind_speed_10m") if cur.get("wind_speed_10m") is not None else 12.0)
                gusts_kmh = float(cur.get("wind_gusts_10m") if cur.get("wind_gusts_10m") is not None else wind_kmh)
                pressure_hpa = float(cur.get("surface_pressure") if cur.get("surface_pressure") is not None else 1009.0)
                base_dtype = demo.BASE_RISK.get(z["name"], ("Flood", 0.25))[0]
                dtype, prob = _derive_weather_hazard(
                    z["name"], temp_c, precip_mm, wind_kmh, gusts_kmh, pressure_hpa, base_dtype
                )
                write(
                    """INSERT INTO risk_logs(timestamp, zone, lat, lon, disaster_type, probability, source,
                       temp_c, wind_kmh, gusts_kmh, precip_mm, pressure_hpa)
                       VALUES (:t, :z, :la, :lo, :d, :p, 'Open-Meteo Live API', :tc, :wk, :gk, :pm, :ph)""",
                    t=now(), z=z["name"], la=z["lat"], lo=z["lon"], d=dtype, p=float(prob),
                    tc=round(temp_c, 1), wk=round(wind_kmh, 1), gk=round(gusts_kmh, 1),
                    pm=round(precip_mm, 2), ph=round(pressure_hpa, 1),
                )
                updated += 1
                details.append(f"{z['name']}: {dtype} {prob:.0%} ({temp_c:.1f}°C, gusts {gusts_kmh:.1f} km/h, rain {precip_mm:.1f} mm, {pressure_hpa:.1f} hPa)")
    except Exception as exc:
        msg = f"Open-Meteo API request could not complete ({type(exc).__name__}). Existing risk readings retained."
        log_event("SYSTEM", msg)
        return {"source": "Open-Meteo Live API", "updated_zones": 0, "message": msg}

    summary = f"Open-Meteo Live API updated {updated} Maharashtra zones ({'; '.join(details[:3])}...)."
    event("RISK_UPDATED", summary, do_replan=True)
    return {"source": "Open-Meteo Live API", "updated_zones": updated, "details": details, "message": summary}


def latest_zone_telemetry():
    """Return zone -> latest risk_logs row with live weather telemetry (temp_c, wind_kmh, gusts_kmh, precip_mm, pressure_hpa, source)."""
    ensure_telemetry_schema()
    rows = read(
        """SELECT zone, timestamp, disaster_type, probability, source, temp_c, wind_kmh, gusts_kmh, precip_mm, pressure_hpa
           FROM risk_logs WHERE id IN (SELECT MAX(id) FROM risk_logs GROUP BY zone)"""
    )
    return {r["zone"]: r for r in rows}


def sync_usgs_and_eonet_intake():
    """Fetch real-time seismic events from USGS GeoJSON API and wildfire/storm events from NASA FIRMS / EONET,
    and submit them directly into Intake & Triage (`citizen_reports`) via `intake_agent.submit()`."""
    import intake_agent

    submitted = 0
    sources_polled = []
    existing_descriptions = {
        r["description"] for r in read("SELECT description FROM citizen_reports") if r.get("description")
    }

    usgs_params = urllib.parse.urlencode({
        "format": "geojson",
        "minlatitude": "14.0",
        "maxlatitude": "22.5",
        "minlongitude": "70.0",
        "maxlongitude": "78.0",
        "minmagnitude": "2.5",
        "limit": "10",
        "orderby": "time",
    })
    try:
        usgs_bytes, _, _ = fetch_with_etag(f"{USGS_GEOJSON_URL}?{usgs_params}", timeout=6)
        usgs_data = json.loads(usgs_bytes.decode("utf-8"))
        sources_polled.append("USGS GeoJSON API")
        for feat in (usgs_data.get("features") or [])[:8]:
            props = feat.get("properties") or {}
            geom = feat.get("geometry") or {}
            coords = geom.get("coordinates") or []
            if len(coords) < 2:
                continue
            lon, lat = float(coords[0]), float(coords[1])
            mag = float(props.get("mag") or 3.0)
            place = str(props.get("place") or "Western India region")
            zone_name, dist_km = nearest_monitored_zone(lat, lon, max_km=400.0)
            if not zone_name:
                continue
            urgency = "CRITICAL" if mag >= 5.5 else ("HIGH" if mag >= 4.5 else ("MEDIUM" if mag >= 3.5 else "LOW"))
            est_people = max(10, int(round((mag ** 2) * 15)))
            desc = (
                f"[USGS LIVE GEOJSON] M{mag:.1f} earthquake tremor detected near {place} "
                f"({dist_km} km from {zone_name}). Coordinates ({lat:.3f}, {lon:.3f})."
            )
            if desc not in existing_descriptions:
                intake_agent.submit({
                    "source": "usgs_api",
                    "zone": zone_name,
                    "lat": lat,
                    "lon": lon,
                    "people": est_people,
                    "disaster_type": "Earthquake",
                    "urgency": urgency,
                    "description": desc,
                    "synthetic": 0,
                })
                existing_descriptions.add(desc)
                submitted += 1
    except Exception:
        pass

    firms_key = os.environ.get("NASA_FIRMS_KEY", "").strip()
    if firms_key:
        firms_url = f"{NASA_FIRMS_AREA_URL}/{firms_key}/VIIRS_SNPP_NRT/72.0,15.5,75.5,20.5/1"
        try:
            csv_bytes, _, _ = fetch_with_etag(firms_url, timeout=6)
            sources_polled.append("NASA FIRMS VIIRS API")
            reader = csv.DictReader(io.StringIO(csv_bytes.decode("utf-8", errors="ignore")))
            for idx, row in enumerate(reader):
                if idx >= 5:
                    break
                lat = float(row.get("latitude") or 0.0)
                lon = float(row.get("longitude") or 0.0)
                frp = float(row.get("frp") or 10.0)
                zone_name, _ = nearest_monitored_zone(lat, lon, max_km=120.0)
                if not zone_name:
                    continue
                desc = (
                    f"[NASA FIRMS LIVE] VIIRS thermal fire anomaly in {zone_name} "
                    f"(FRP {frp:.1f} MW at {lat:.3f}, {lon:.3f})."
                )
                if desc not in existing_descriptions:
                    intake_agent.submit({
                        "source": "nasa_firms_api",
                        "zone": zone_name,
                        "lat": lat,
                        "lon": lon,
                        "people": max(15, int(round(frp * 2))),
                        "disaster_type": "Fire",
                        "urgency": "HIGH" if frp >= 25 else "MEDIUM",
                        "description": desc,
                        "synthetic": 0,
                    })
                    existing_descriptions.add(desc)
                    submitted += 1
        except Exception:
            pass

    try:
        eonet_bytes, _, _ = fetch_with_etag(f"{NASA_EONET_URL}?status=open&limit=25", timeout=6)
        eonet_data = json.loads(eonet_bytes.decode("utf-8"))
        sources_polled.append("NASA EONET v3 API")
        for ev in eonet_data.get("events") or []:
            title = str(ev.get("title") or "")
            cats = [str(c.get("title") or "") for c in (ev.get("categories") or [])]
            geoms = ev.get("geometry") or []
            if not geoms:
                continue
            coords = geoms[-1].get("coordinates") or []
            if len(coords) < 2 or not isinstance(coords[0], (int, float)):
                continue
            lon, lat = float(coords[0]), float(coords[1])
            zone_name, dist_km = nearest_monitored_zone(lat, lon, max_km=450.0)
            if not zone_name:
                continue
            cat_text = " ".join(cats) + " " + title
            dtype = "Fire" if "fire" in cat_text.lower() else map_cap_hazard(cat_text)
            desc = f"[NASA EONET LIVE] {title} ({dist_km} km from {zone_name}, coords {lat:.3f}, {lon:.3f})."
            if desc not in existing_descriptions:
                intake_agent.submit({
                    "source": "nasa_eonet_api",
                    "zone": zone_name,
                    "lat": lat,
                    "lon": lon,
                    "people": 25,
                    "disaster_type": dtype,
                    "urgency": "HIGH",
                    "description": desc,
                    "synthetic": 0,
                })
                existing_descriptions.add(desc)
                submitted += 1
    except Exception:
        pass

    msg = (
        f"Live Incident Intake API sync ({', '.join(sources_polled) or 'offline'}): "
        f"{submitted} new regional event(s) triaged into Citizen Reports queue."
    )
    log_event("INTAKE_SUBMITTED", msg)
    return {"sources": sources_polled, "submitted": submitted, "message": msg}


def sync_live_roads_osrm():
    """Update corridor travel minutes and road conditions across R1-R20 using OpenRouteService / OSRM Driving API
    combined with live zone flood/cyclone hazard readings."""
    roads = roads_with_positions()
    ors_key = os.environ.get("ORS_API_KEY", "").strip()
    latest_risks = {
        r["zone"]: float(r["probability"] or 0.0)
        for r in read("SELECT zone, probability FROM risk_logs WHERE id IN (SELECT MAX(id) FROM risk_logs GROUP BY zone)")
    }
    updated_count = 0
    api_used = "OSRM / Live Corridor Telemetry"

    for road in roads:
        a_lat, a_lon = road.get("a_lat"), road.get("a_lon")
        b_lat, b_lon = road.get("b_lat"), road.get("b_lon")
        if None in (a_lat, a_lon, b_lat, b_lon):
            continue

        live_minutes = None
        if ors_key:
            try:
                ors_url = (
                    f"{ORS_DIRECTIONS_URL}?api_key={urllib.parse.quote(ors_key)}"
                    f"&start={a_lon:.4f},{a_lat:.4f}&end={b_lon:.4f},{b_lat:.4f}"
                )
                raw, _, _ = fetch_with_etag(ors_url, timeout=4)
                doc = json.loads(raw.decode("utf-8"))
                secs = doc["features"][0]["properties"]["summary"]["duration"]
                live_minutes = max(5.0, round(float(secs) / 60.0, 1))
                api_used = "OpenRouteService Directions API"
            except Exception:
                live_minutes = None

        if live_minutes is None:
            try:
                osrm_url = f"{OSRM_ROUTE_URL}/{a_lon:.4f},{a_lat:.4f};{b_lon:.4f},{b_lat:.4f}?overview=false"
                raw, _, _ = fetch_with_etag(osrm_url, timeout=4)
                doc = json.loads(raw.decode("utf-8"))
                if doc.get("routes"):
                    secs = doc["routes"][0]["duration"]
                    live_minutes = max(5.0, round(float(secs) / 60.0, 1))
                    api_used = "OSRM Routing API"
            except Exception:
                live_minutes = None

        base_mins = float(road["minutes"])
        if live_minutes is not None:
            write("UPDATE roads SET minutes=:m WHERE id=:i", m=live_minutes, i=road["id"])
            updated_count += 1

        end_risk = max(latest_risks.get(road["a"], 0.0), latest_risks.get(road["b"], 0.0))
        if road["status"] != "BLOCKED":
            new_status = "SLOW" if (end_risk >= 0.78 or (live_minutes and live_minutes > base_mins * 1.45)) else "OPEN"
            if new_status != road["status"]:
                write("UPDATE roads SET status=:s WHERE id=:i", s=new_status, i=road["id"])
                updated_count += 1

    msg = f"{api_used} synced {updated_count} corridor travel-time / status updates across R1–R20."
    if updated_count > 0:
        event("ROAD_REOPENED", msg, do_replan=True)
    else:
        log_event("SYSTEM", msg)
    return {"source": api_used, "updated_roads": updated_count, "message": msg}


def sync_all_live_feeds():
    """Execute a complete multi-agency real-time API and CAP 1.2 XML sync across all Saarthi modules:
    1. Open-Meteo / IMD live district weather & hazard probabilities (`risk_logs`)
    2. NDMA SACHET CAP 1.2 XML + Maharashtra/India RSS feeds with ETag caching (`alerts`, `cap_feed_logs`, `risk_logs`)
    3. GDACS UN/EC Multi-Hazard XML feed (`alerts` & `cap_feed_logs`)
    4. USGS Earthquake GeoJSON + NASA FIRMS/EONET active fire feeds (`citizen_reports`)
    5. OpenRouteService / OSRM live road corridor travel durations (`roads`)
    """
    ensure_telemetry_schema()
    meteo = sync_open_meteo_risk()
    sachet = sync_sachet_cap_xml()
    gdacs = sync_gdacs_xml()
    intake = sync_usgs_and_eonet_intake()
    routing = sync_live_roads_osrm()
    summary = (
        f"Live API & XML Sync Complete — Open-Meteo: {meteo['updated_zones']} zones | "
        f"NDMA SACHET CAP XML: {sachet.get('xml_items_logged', 0)} XML items ({sachet['alerts_queued']} district alerts, {sachet['etag_hits']} ETag hits) | "
        f"GDACS XML: {gdacs.get('xml_items_logged', 0)} XML items ({gdacs['alerts_queued']} alerts) | "
        f"USGS/NASA Intake: {intake['submitted']} reports | "
        f"Routing API: {routing['updated_roads']} corridors."
    )
    event("RISK_UPDATED", summary, do_replan=True)
    return {
        "meteo": meteo,
        "sachet": sachet,
        "gdacs": gdacs,
        "intake": intake,
        "routing": routing,
        "summary": summary,
    }


def has_live_telemetry():
    """Return True if the database currently holds live API or CAP XML telemetry rows."""
    ensure_telemetry_schema()
    live_rows = read(
        "SELECT COUNT(*) AS n FROM risk_logs WHERE source NOT IN ('IMD (demo)', 'Operator-entered')"
    )
    if live_rows and int(live_rows[0]["n"] or 0) > 0:
        return True
    cap_rows = read("SELECT COUNT(*) AS n FROM cap_feed_logs")
    if cap_rows and int(cap_rows[0]["n"] or 0) > 0:
        return True
    live_events = read(
        "SELECT COUNT(*) AS n FROM events WHERE message LIKE '%NDMA SACHET%' OR message LIKE '%Open-Meteo%' OR message LIKE '%Live API%'"
    )
    return bool(live_events and int(live_events[0]["n"] or 0) > 0)


# =====================================================================
# VOLUNTEERS & RESCUE TASKS (NGO Teams, Rescue Tasks, Assignments, Dispatch)
# ---------------------------------------------------------------------
# Assignment does not dispatch a team. Dispatch and rescue progress are
# operator actions. SOS incidents are left unchanged.
# =====================================================================
RESCUE_PRIORITIES = ("Critical", "High", "Moderate", "Low")
RESCUE_PROGRESS_STATES = ("IN_PROGRESS", "RESCUED", "COMPLETED")


def register_volunteer_team(team_name, team_lead, head_role, home_zone, focus_area, capacity):
    """Register an NGO / volunteer team as AVAILABLE."""
    ensure_telemetry_schema()
    t_name = str(team_name or "").strip()
    t_lead = str(team_lead or "").strip()
    h_role = str(head_role or "Team lead").strip() or "Team lead"
    z_name = str(home_zone or "").strip()
    f_area = str(focus_area or "Rescue").strip() or "Rescue"
    if not t_name:
        raise ActionError("NGO / team name is required.")
    if not t_lead:
        raise ActionError("Team lead name is required.")
    if z_name not in [z[0] for z in demo.ZONES]:
        raise ActionError("Select a recorded home zone.")
    if isinstance(capacity, bool) or not isinstance(capacity, (int, float)) or int(capacity) < 1:
        raise ActionError("Volunteer capacity must be at least 1.")
    cap = int(capacity)
    with transaction():
        team_id = write(
            """INSERT INTO volunteer_teams(registered_at, team_name, team_lead, head_role, home_zone, focus_area, capacity, status)
               VALUES (:t, :tn, :tl, :hr, :hz, :fa, :c, 'AVAILABLE')""",
            t=now(), tn=t_name, tl=t_lead, hr=h_role, hz=z_name, fa=f_area, c=cap,
        )
        log_event("VOLUNTEER_TEAM_REGISTERED", f"NGO team #{team_id} '{t_name}' ({cap} volunteers, lead {t_lead}) registered in {z_name}")
    return team_id


def create_rescue_task(title, zone, priority, people, location_note=""):
    """Record a new OPEN rescue task with coordinates near the zone centre."""
    ensure_telemetry_schema()
    t_title = str(title or "").strip()
    z_name = str(zone or "").strip()
    prio = str(priority or "Critical").strip()
    loc_note = str(location_note or "").strip()
    if not t_title:
        raise ActionError("Task title is required.")
    if z_name not in [z[0] for z in demo.ZONES]:
        raise ActionError("Select a recorded affected zone.")
    if prio not in RESCUE_PRIORITIES:
        raise ActionError("Priority must be Critical, High, Moderate, or Low.")
    if isinstance(people, bool) or not isinstance(people, (int, float)) or int(people) < 1:
        raise ActionError("People requiring assistance must be at least 1.")
    cnt = int(people)
    lat, lon = demo.zone_position(z_name)
    existing_n = len(read("SELECT id FROM rescue_tasks WHERE zone=:z", z=z_name))
    offset_lat = ((existing_n % 5) - 2) * 0.018 + 0.022
    offset_lon = ((existing_n % 3) - 1) * 0.018 - 0.022
    with transaction():
        task_id = write(
            """INSERT INTO rescue_tasks(created_at, title, zone, lat, lon, priority, people, location_note, status, assigned_team_id, assigned_team_name)
               VALUES (:t, :ti, :z, :la, :lo, :pr, :pe, :ln, 'OPEN', NULL, NULL)""",
            t=now(), ti=t_title, z=z_name, la=round(lat + offset_lat, 4), lo=round(lon + offset_lon, 4),
            pr=prio, pe=cnt, ln=loc_note or "Zone centre sector",
        )
        log_event("RESCUE_TASK_CREATED", f"Rescue task #{task_id} '{t_title}' ({prio}, {cnt} people) recorded in {z_name}")
    return task_id


def assign_volunteer_team(task_id, team_id):
    """Assign an AVAILABLE NGO team to an OPEN rescue task. Assignment does not dispatch the team."""
    ensure_telemetry_schema()
    with transaction():
        tasks = read("SELECT * FROM rescue_tasks WHERE id=:i", i=int(task_id))
        if not tasks:
            raise ActionError("That rescue task does not exist.")
        task = tasks[0]
        if task["status"] != "OPEN":
            raise ActionError("That rescue task is no longer open for assignment.")
        teams = read("SELECT * FROM volunteer_teams WHERE id=:i", i=int(team_id))
        if not teams:
            raise ActionError("That NGO team does not exist.")
        team = teams[0]
        if team["status"] != "AVAILABLE":
            raise ActionError("That NGO team is not currently available.")
        ts = now()
        aid = write(
            """INSERT INTO volunteer_assignments(assigned_at, updated_at, task_id, task_title, zone, priority, people,
               team_id, team_name, team_lead, volunteers, status)
               VALUES (:t, :t, :tk, :tt, :z, :pr, :pe, :tm, :tn, :tl, :v, 'ASSIGNED')""",
            t=ts, tk=task["id"], tt=task["title"], z=task["zone"], pr=task["priority"], pe=task["people"],
            tm=team["id"], tn=team["team_name"], tl=team["team_lead"], v=team["capacity"],
        )
        write("UPDATE rescue_tasks SET status='ASSIGNED', assigned_team_id=:tm, assigned_team_name=:tn WHERE id=:i",
              tm=team["id"], tn=team["team_name"], i=task["id"])
        write("UPDATE volunteer_teams SET status='ASSIGNED' WHERE id=:i", i=team["id"])
        write(
            """INSERT INTO volunteer_status_history(timestamp, assignment_id, task_id, task_title, team_id, team_name, status, note)
               VALUES (:t, :a, :tk, :tt, :tm, :tn, 'ASSIGNED', 'Operator assigned team to rescue task (not dispatched yet)')""",
            t=ts, a=aid, tk=task["id"], tt=task["title"], tm=team["id"], tn=team["team_name"],
        )
        log_event("VOLUNTEER_ASSIGNED", f"Assigned '{team['team_name']}' to rescue task #{task['id']} ({task['zone']}). Not dispatched yet.")
    return aid


def dispatch_volunteer_assignment(assignment_id):
    """Operator action: dispatch an ASSIGNED team to its rescue task."""
    ensure_telemetry_schema()
    with transaction():
        rows = read("SELECT * FROM volunteer_assignments WHERE id=:i", i=int(assignment_id))
        if not rows:
            raise ActionError("That assignment does not exist.")
        row = rows[0]
        if row["status"] != "ASSIGNED":
            raise ActionError("Only an ASSIGNED team waiting for dispatch can be dispatched.")
        ts = now()
        write("UPDATE volunteer_assignments SET status='DISPATCHED', updated_at=:t WHERE id=:i", t=ts, i=row["id"])
        write("UPDATE rescue_tasks SET status='DISPATCHED' WHERE id=:i", i=row["task_id"])
        write("UPDATE volunteer_teams SET status='DEPLOYED' WHERE id=:i", i=row["team_id"])
        write(
            """INSERT INTO volunteer_status_history(timestamp, assignment_id, task_id, task_title, team_id, team_name, status, note)
               VALUES (:t, :a, :tk, :tt, :tm, :tn, 'DISPATCHED', 'Operator dispatched team to field rescue task')""",
            t=ts, a=row["id"], tk=row["task_id"], tt=row["task_title"], tm=row["team_id"], tn=row["team_name"],
        )
        log_event("VOLUNTEER_DISPATCHED", f"Dispatched '{row['team_name']}' for rescue task #{row['task_id']} ({row['zone']})")


def update_rescue_progress(assignment_id, new_status, note=""):
    """Operator action: record IN_PROGRESS, RESCUED, or COMPLETED after dispatch."""
    ensure_telemetry_schema()
    target_status = str(new_status or "").strip().upper()
    if target_status not in RESCUE_PROGRESS_STATES:
        raise ActionError("Status must be IN_PROGRESS, RESCUED, or COMPLETED.")
    with transaction():
        rows = read("SELECT * FROM volunteer_assignments WHERE id=:i", i=int(assignment_id))
        if not rows:
            raise ActionError("That assignment does not exist.")
        row = rows[0]
        if row["status"] == "ASSIGNED":
            raise ActionError("Dispatch a team before recording IN_PROGRESS, RESCUED, or COMPLETED.")
        if row["status"] == "COMPLETED":
            raise ActionError("This rescue assignment is already completed.")
        ts = now()
        clean_note = str(note or "").strip() or f"Operator recorded {target_status}"
        write("UPDATE volunteer_assignments SET status=:s, updated_at=:t WHERE id=:i", s=target_status, t=ts, i=row["id"])
        write("UPDATE rescue_tasks SET status=:s WHERE id=:i", s=target_status, i=row["task_id"])
        if target_status == "COMPLETED":
            write("UPDATE volunteer_teams SET status='AVAILABLE' WHERE id=:i", i=row["team_id"])
        else:
            write("UPDATE volunteer_teams SET status='DEPLOYED' WHERE id=:i", i=row["team_id"])
        write(
            """INSERT INTO volunteer_status_history(timestamp, assignment_id, task_id, task_title, team_id, team_name, status, note)
               VALUES (:t, :a, :tk, :tt, :tm, :tn, :s, :n)""",
            t=ts, a=row["id"], tk=row["task_id"], tt=row["task_title"], tm=row["team_id"], tn=row["team_name"],
            s=target_status, n=clean_note,
        )
        log_event("RESCUE_" + target_status, f"Rescue task #{row['task_id']} ({row['team_name']}): {target_status}")
