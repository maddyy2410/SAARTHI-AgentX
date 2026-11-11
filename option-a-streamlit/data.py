# =====================================================================
# data.py  :  OPTION A's database and actions (SQLite, no web server)
# ---------------------------------------------------------------------
# app.py (the screens) calls these functions. They save changes to the
# SQLite file, and every change is an EVENT that re-runs the optimiser
# in engine.py and stores a new plan version: the same flow as Option B.
# =====================================================================

import json  # store small lists as text
import os  # file paths and settings
import sqlite3  # built-in SQLite database library
import threading  # one open transaction per thread
from contextlib import contextmanager  # transaction()
from datetime import datetime  # current time

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


# ---------------------------------------------------------------------
# Volunteers: NGO teams and rescue tasks. Additive tables only.
# ---------------------------------------------------------------------
VOLUNTEER_SCHEMA = """
CREATE TABLE IF NOT EXISTS ngo_teams(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    name TEXT NOT NULL,
    lead_name TEXT,
    lead_role TEXT NOT NULL,
    home_zone TEXT,
    focus TEXT,
    capacity INTEGER NOT NULL,
    available INTEGER NOT NULL,
    status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rescue_tasks(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    title TEXT NOT NULL,
    zone TEXT NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    priority TEXT NOT NULL,
    people INTEGER NOT NULL,
    status TEXT NOT NULL,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS rescue_assignments(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    team_id INTEGER NOT NULL,
    volunteers INTEGER NOT NULL,
    assigned_at TEXT NOT NULL,
    dispatched_at TEXT,
    status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rescue_status_log(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    status TEXT NOT NULL,
    changed_at TEXT NOT NULL,
    note TEXT
);
"""

RESCUE_STATUSES = ("PENDING", "ASSIGNED", "DISPATCHED", "IN_PROGRESS", "RESCUED", "COMPLETED")
RESCUE_PRIORITIES = ("Critical", "High", "Moderate", "Low")
_RESCUE_ORDER = {name: index for index, name in enumerate(RESCUE_STATUSES)}


def ensure_volunteer_schema():
    """Create volunteer tables when missing. Existing tables and rows stay in place."""
    conn = sqlite3.connect(get_db_path(), timeout=10)
    try:
        conn.executescript(VOLUNTEER_SCHEMA)
        conn.commit()
    finally:
        conn.close()


def _clean_text(value, label):
    text = str(value or "").strip()
    if not text:
        raise ActionError(f"{label} is required.")
    return text


def register_ngo_team(name, lead_name, lead_role, home_zone, focus, capacity):
    """Register one NGO team. Available strength starts at full capacity."""
    ensure_volunteer_schema()
    name = _clean_text(name, "Team name")
    role = _clean_text(lead_role, "Head role")
    capacity = int(capacity)
    if capacity < 1:
        raise ActionError("Volunteer capacity must be at least 1.")
    zone = _clean_text(home_zone, "Home zone")
    if not read("SELECT name FROM zones WHERE name=:n", n=zone):
        raise ActionError(f"{zone} is not a recorded zone.")
    team_id = write(
        """INSERT INTO ngo_teams(created_at, name, lead_name, lead_role, home_zone, focus,
              capacity, available, status)
           VALUES (:t, :n, :lead, :role, :zone, :focus, :cap, :cap, 'AVAILABLE')""",
        t=now(), n=name, lead=str(lead_name or "").strip(), role=role, zone=zone,
        focus=str(focus or "").strip(), cap=capacity)
    event("VOLUNTEER_TEAM", f"NGO team registered: {name} ({capacity} volunteers)", do_replan=False)
    return team_id


def create_rescue_task(title, zone, priority, people, detail=""):
    """Record a rescue task at the affected zone. Status starts at PENDING."""
    ensure_volunteer_schema()
    title = _clean_text(title, "Task title")
    zone = _clean_text(zone, "Affected zone")
    if priority not in RESCUE_PRIORITIES:
        raise ActionError("Choose a recorded priority.")
    people = int(people)
    if people < 1:
        raise ActionError("People requiring assistance must be at least 1.")
    with transaction():
        spots = read("SELECT lat, lon FROM zones WHERE name=:n", n=zone)
        if not spots or spots[0]["lat"] is None or spots[0]["lon"] is None:
            raise ActionError(f"{zone} has no recorded location.")
        placed = read("SELECT COUNT(*) AS n FROM rescue_tasks WHERE zone=:z", z=zone)[0]["n"]
        lat = float(spots[0]["lat"])
        lon = float(spots[0]["lon"]) + 0.045 * (int(placed) + 1)
        stamp = now()
        task_id = write(
            """INSERT INTO rescue_tasks(created_at, title, zone, lat, lon, priority, people, status, detail)
               VALUES (:t, :title, :zone, :lat, :lon, :priority, :people, 'PENDING', :detail)""",
            t=stamp, title=title, zone=zone, lat=lat, lon=lon, priority=priority,
            people=people, detail=str(detail or "").strip())
        write("""INSERT INTO rescue_status_log(task_id, status, changed_at, note)
                 VALUES (:i, 'PENDING', :t, :n)""",
              i=task_id, t=stamp, n="Rescue task recorded.")
    event("RESCUE_TASK", f"Rescue task #{task_id} recorded in {zone}", do_replan=False)
    return task_id


def list_ngo_teams():
    ensure_volunteer_schema()
    return read("SELECT * FROM ngo_teams ORDER BY id DESC")


def list_rescue_tasks():
    ensure_volunteer_schema()
    return read("SELECT * FROM rescue_tasks ORDER BY id DESC")


def list_rescue_assignments():
    ensure_volunteer_schema()
    return read("""SELECT a.*, t.title, t.zone, t.status AS task_status, n.name AS team_name
                   FROM rescue_assignments a
                   JOIN rescue_tasks t ON t.id = a.task_id
                   JOIN ngo_teams n ON n.id = a.team_id
                   ORDER BY a.id DESC""")


def rescue_status_log(task_id=None):
    ensure_volunteer_schema()
    if task_id is None:
        return read("SELECT * FROM rescue_status_log ORDER BY id DESC")
    return read("SELECT * FROM rescue_status_log WHERE task_id=:i ORDER BY id", i=int(task_id))


def assignable_teams():
    ensure_volunteer_schema()
    return read("""SELECT * FROM ngo_teams
                   WHERE status='AVAILABLE' AND available > 0
                   ORDER BY name""")


def open_rescue_tasks():
    """Tasks that can still take a team. Completed and rescued work stays closed."""
    ensure_volunteer_schema()
    return read("""SELECT * FROM rescue_tasks
                   WHERE status IN ('PENDING', 'ASSIGNED', 'DISPATCHED', 'IN_PROGRESS')
                   ORDER BY id""")


def dispatchable_assignments():
    ensure_volunteer_schema()
    return read("""SELECT a.*, t.title, t.zone, t.status AS task_status, n.name AS team_name
                   FROM rescue_assignments a
                   JOIN rescue_tasks t ON t.id = a.task_id
                   JOIN ngo_teams n ON n.id = a.team_id
                   WHERE a.status='ASSIGNED' AND t.status != 'COMPLETED'
                   ORDER BY a.id""")


def progressable_tasks():
    ensure_volunteer_schema()
    return read("""SELECT * FROM rescue_tasks
                   WHERE status IN ('DISPATCHED', 'IN_PROGRESS', 'RESCUED')
                   ORDER BY id""")


def next_rescue_statuses(status):
    """Forward rescue states an operator can set after dispatch."""
    rank = _RESCUE_ORDER.get(status, -1)
    return [name for name in ("IN_PROGRESS", "RESCUED", "COMPLETED") if _RESCUE_ORDER[name] > rank]


def rescue_task_markers():
    """Locations for the operations map. Reads volunteer rows only."""
    ensure_volunteer_schema()
    return read("""SELECT id, title, zone, lat, lon, priority, people, status
                   FROM rescue_tasks ORDER BY id""")


def volunteer_dashboard():
    ensure_volunteer_schema()
    teams = read("SELECT capacity, available, status FROM ngo_teams")
    tasks = read("SELECT status FROM rescue_tasks")
    return {
        "teams": len(teams),
        "volunteers": sum(int(row["capacity"] or 0) for row in teams),
        "available": sum(int(row["available"] or 0) for row in teams),
        "active_tasks": sum(1 for row in tasks if row["status"] != "COMPLETED"),
        "deployed": sum(1 for row in teams if row["status"] == "DEPLOYED"),
        "completed": sum(1 for row in tasks if row["status"] == "COMPLETED"),
    }


def assign_rescue_team(task_id, team_id, volunteers):
    """Assign volunteers from an available team. This does not dispatch them."""
    ensure_volunteer_schema()
    volunteers = int(volunteers)
    if volunteers < 1:
        raise ActionError("Assign at least 1 volunteer.")
    with transaction():
        tasks = read("SELECT * FROM rescue_tasks WHERE id=:i", i=int(task_id))
        if not tasks:
            raise ActionError("Rescue task was not found.")
        task = tasks[0]
        if task["status"] in ("RESCUED", "COMPLETED"):
            raise ActionError("This rescue task is no longer open for assignment.")
        teams = read("SELECT * FROM ngo_teams WHERE id=:i", i=int(team_id))
        if not teams:
            raise ActionError("NGO team was not found.")
        team = teams[0]
        if team["status"] != "AVAILABLE":
            raise ActionError(f"{team['name']} is deployed. Choose an available team.")
        available = int(team["available"])
        if volunteers > available:
            raise ActionError(
                f"{team['name']} has {available} volunteers available. "
                f"That is below the {volunteers} requested.")
        already = read("""SELECT id FROM rescue_assignments
                          WHERE task_id=:task AND team_id=:team""",
                       task=task["id"], team=team["id"])
        if already:
            raise ActionError(f"{team['name']} is already assigned to this rescue task.")
        write("UPDATE ngo_teams SET available=:a WHERE id=:i",
              a=available - volunteers, i=team["id"])
        write("""INSERT INTO rescue_assignments(task_id, team_id, volunteers, assigned_at, dispatched_at, status)
                 VALUES (:task, :team, :n, :t, NULL, 'ASSIGNED')""",
              task=task["id"], team=team["id"], n=volunteers, t=now())
        if task["status"] == "PENDING":
            stamp = now()
            write("UPDATE rescue_tasks SET status='ASSIGNED' WHERE id=:i", i=task["id"])
            write("""INSERT INTO rescue_status_log(task_id, status, changed_at, note)
                     VALUES (:i, 'ASSIGNED', :t, :n)""",
                  i=task["id"], t=stamp,
                  n=f"Assigned {volunteers} volunteers from {team['name']}.")
    event("VOLUNTEER_ASSIGNED",
          f"Assigned {team['name']} ({volunteers}) to rescue task #{task['id']}",
          do_replan=False)


def dispatch_rescue_assignment(assignment_id):
    """Operator dispatch. Assignment alone never sets this state."""
    ensure_volunteer_schema()
    with transaction():
        rows = read("SELECT * FROM rescue_assignments WHERE id=:i", i=int(assignment_id))
        if not rows:
            raise ActionError("Assignment was not found.")
        row = rows[0]
        if row["status"] != "ASSIGNED":
            raise ActionError("This assignment is already dispatched.")
        tasks = read("SELECT * FROM rescue_tasks WHERE id=:i", i=row["task_id"])
        if not tasks:
            raise ActionError("Rescue task was not found.")
        task = tasks[0]
        if task["status"] == "COMPLETED":
            raise ActionError("This rescue task is already completed.")
        team = read("SELECT * FROM ngo_teams WHERE id=:i", i=row["team_id"])[0]
        stamp = now()
        write("""UPDATE rescue_assignments SET status='DISPATCHED', dispatched_at=:t WHERE id=:i""",
              t=stamp, i=row["id"])
        write("UPDATE ngo_teams SET status='DEPLOYED' WHERE id=:i", i=team["id"])
        if task["status"] in ("PENDING", "ASSIGNED"):
            write("UPDATE rescue_tasks SET status='DISPATCHED' WHERE id=:i", i=task["id"])
            write("""INSERT INTO rescue_status_log(task_id, status, changed_at, note)
                     VALUES (:i, 'DISPATCHED', :t, :n)""",
                  i=task["id"], t=stamp, n=f"Dispatched {team['name']}.")
    event("VOLUNTEER_DISPATCHED",
          f"Dispatched {team['name']} on rescue task #{task['id']}",
          do_replan=False)


def _release_task_volunteers(task_id):
    """Return assigned strength when a rescue is completed. Call inside a transaction."""
    for row in read("SELECT * FROM rescue_assignments WHERE task_id=:i", i=int(task_id)):
        team = read("SELECT * FROM ngo_teams WHERE id=:i", i=row["team_id"])[0]
        available = min(int(team["capacity"]), int(team["available"]) + int(row["volunteers"]))
        still = read("""SELECT COUNT(*) AS n
                        FROM rescue_assignments a
                        JOIN rescue_tasks t ON t.id = a.task_id
                        WHERE a.team_id=:team AND a.dispatched_at IS NOT NULL
                          AND t.status IN ('DISPATCHED', 'IN_PROGRESS', 'RESCUED')
                          AND t.id != :task""",
                     team=row["team_id"], task=int(task_id))[0]["n"]
        write("UPDATE ngo_teams SET available=:a, status=:s WHERE id=:i",
              a=available, s="DEPLOYED" if still else "AVAILABLE", i=team["id"])


def update_rescue_progress(task_id, status):
    """Move a dispatched rescue forward. Dispatch itself stays a separate action."""
    ensure_volunteer_schema()
    status = str(status or "").strip()
    if status not in ("IN_PROGRESS", "RESCUED", "COMPLETED"):
        raise ActionError("Choose IN_PROGRESS, RESCUED, or COMPLETED.")
    with transaction():
        tasks = read("SELECT * FROM rescue_tasks WHERE id=:i", i=int(task_id))
        if not tasks:
            raise ActionError("Rescue task was not found.")
        task = tasks[0]
        if _RESCUE_ORDER[task["status"]] < _RESCUE_ORDER["DISPATCHED"]:
            raise ActionError("Dispatch the assigned team before updating rescue progress.")
        if _RESCUE_ORDER[status] <= _RESCUE_ORDER[task["status"]]:
            raise ActionError("Rescue progress only moves forward.")
        if status == "COMPLETED":
            _release_task_volunteers(task["id"])
        stamp = now()
        write("UPDATE rescue_tasks SET status=:s WHERE id=:i", s=status, i=task["id"])
        write("""INSERT INTO rescue_status_log(task_id, status, changed_at, note)
                 VALUES (:i, :s, :t, :n)""",
              i=task["id"], s=status, t=stamp, n=f"Status set to {status}.")
    event("RESCUE_PROGRESS", f"Rescue task #{task['id']} is {status}", do_replan=False)


def init_db():  # create tables; add demo data if empty
    conn = sqlite3.connect(get_db_path())
    conn.executescript(SCHEMA)
    from intake_agent import INTAKE_SCHEMA  # new table only; existing tables are not dropped
    conn.executescript(INTAKE_SCHEMA)
    conn.executescript(VOLUNTEER_SCHEMA)
    conn.commit()
    conn.close()
    if read("SELECT COUNT(*) AS n FROM zones")[0]["n"] == 0:
        seed_demo_data()
    from intake_agent import ensure_schema  # add score columns on a database that already has reports
    ensure_schema()


def reset_db():  # delete the file and start again
    path = get_db_path()
    if os.path.exists(path):
        os.remove(path)
    init_db()
