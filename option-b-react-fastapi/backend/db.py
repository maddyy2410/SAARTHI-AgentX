# =====================================================================
# db.py  :  everything about the DATABASE for Option B
# ---------------------------------------------------------------------
# One setting picks the database (DATABASE_URL):
#   SQLite (default, zero setup):   sqlite:///disaster.db
#   PostgreSQL:                     postgresql+psycopg://user:password@localhost:5432/disaster
# SQLAlchemy lets the same Python code talk to either one.
# =====================================================================

import json  # store small lists (routes, keys) as text
import os  # read settings from the environment
from datetime import datetime  # current time

from sqlalchemy import create_engine, text  # connect to a database, write plain SQL safely

import demo_data as demo  # the shared demo world (demo_data.py)
import engine as logic  # the shared decision logic (engine.py)

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///disaster.db")  # which database (SQLite if not set)
IS_SQLITE = DATABASE_URL.startswith("sqlite")  # True for SQLite
db = create_engine(DATABASE_URL, connect_args={"check_same_thread": False} if IS_SQLITE else {})  # connection pool
PK = "INTEGER PRIMARY KEY AUTOINCREMENT" if IS_SQLITE else "SERIAL PRIMARY KEY"  # auto-numbered id, per database

TABLES = ["zones", "risk_logs", "alerts", "incidents", "hubs", "inventory", "hospitals", "roads",
          "supply_movement", "events", "plans", "plan_runs"]  # every table (used by reset)

SCHEMA = [  # the contract with the rest of the team: one CREATE TABLE per table
    "CREATE TABLE IF NOT EXISTS zones(name TEXT PRIMARY KEY, lat REAL, lon REAL, population INTEGER, vulnerability REAL)",
    f"""CREATE TABLE IF NOT EXISTS risk_logs(id {PK}, timestamp TEXT, zone TEXT, lat REAL, lon REAL,
        disaster_type TEXT, probability REAL, source TEXT)""",  # scraper readings (probability 0 to 1)
    f"""CREATE TABLE IF NOT EXISTS alerts(id {PK}, created_at TEXT, zone TEXT, disaster_type TEXT, severity TEXT,
        eta_hours REAL, radius_km REAL, sms_status TEXT)""",  # early warnings (QUEUED / SENT / FAILED)
    f"""CREATE TABLE IF NOT EXISTS incidents(id {PK}, reported_at TEXT, lat REAL NOT NULL, lon REAL NOT NULL,
        zone TEXT, need_type TEXT, people INTEGER, status TEXT, claimed_by TEXT)""",  # SOS (OPEN / IN_TRANSIT / RESOLVED)
    "CREATE TABLE IF NOT EXISTS hubs(id INTEGER PRIMARY KEY, name TEXT, lat REAL, lon REAL)",  # warehouses
    "CREATE TABLE IF NOT EXISTS inventory(hub_id INTEGER, item TEXT, qty INTEGER, PRIMARY KEY(hub_id, item))",  # stock
    """CREATE TABLE IF NOT EXISTS hospitals(id INTEGER PRIMARY KEY, name TEXT, zone TEXT, lat REAL, lon REAL,
        total_beds INTEGER, available_beds INTEGER, icu_total INTEGER, icu_available INTEGER, oxygen INTEGER)""",  # capacity
    "CREATE TABLE IF NOT EXISTS roads(id TEXT PRIMARY KEY, a TEXT, b TEXT, minutes REAL, status TEXT)",  # OPEN / SLOW / BLOCKED
    f"""CREATE TABLE IF NOT EXISTS supply_movement(entry_id {PK}, shipment_id TEXT, timestamp TEXT, item TEXT,
        qty INTEGER, source_hub TEXT, destination TEXT, status TEXT, prev_hash TEXT, hash TEXT)""",  # ledger
    f"CREATE TABLE IF NOT EXISTS events(id {PK}, timestamp TEXT, type TEXT, message TEXT)",  # typed live feed
    f"""CREATE TABLE IF NOT EXISTS plans(id {PK}, version INTEGER, kind TEXT, plan_key TEXT, summary TEXT,
        data TEXT)""",  # one row per allocation in each plan version
    """CREATE TABLE IF NOT EXISTS plan_runs(version INTEGER PRIMARY KEY, created_at TEXT, trigger_type TEXT,
        trigger_text TEXT, solver TEXT, changed INTEGER, changed_keys TEXT, shortfalls TEXT, unplaced TEXT)""",  # plan history
]


# ---------------------------------------------------------------------
# Small helpers used by every other file
# ---------------------------------------------------------------------
def read(sql, **params):  # SELECT -> list of dictionaries
    with db.connect() as conn:  # open a connection (closes itself)
        return [dict(r) for r in conn.execute(text(sql), params).mappings().all()]  # rows as dicts


def write(sql, **params):  # INSERT / UPDATE / DELETE, saved automatically
    with db.begin() as conn:  # begin() commits at the end of the block
        return conn.execute(text(sql), params)  # run it (":name" filled safely from params)


def insert_returning_id(sql, **params):  # INSERT that gives back the new id
    with db.begin() as conn:  # open and auto-save
        return conn.execute(text(sql + " RETURNING id"), params).scalar()  # works in SQLite and PostgreSQL


def now():  # current time as text
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")  # e.g. "2026-09-26 15:30:00"


def log_event(event_type, message):  # one line in the live feed
    write("INSERT INTO events(timestamp, type, message) VALUES (:t, :ty, :m)", t=now(), ty=event_type, m=message)


# ---------------------------------------------------------------------
# Ledger (fingerprints come from engine.py)
# ---------------------------------------------------------------------
def add_ledger_entry(shipment_id, item, qty, source, dest, status):  # add one step to the ledger
    last = read("SELECT hash FROM supply_movement ORDER BY entry_id DESC LIMIT 1")  # newest fingerprint
    prev = last[0]["hash"] if last else "GENESIS"  # or the starting word
    row = {"shipment_id": shipment_id, "timestamp": now(), "item": item, "qty": int(qty),
           "source_hub": source, "destination": dest, "status": status, "prev_hash": prev}  # the new record
    row["hash"] = logic.make_hash(prev, logic.ledger_record(row))  # its fingerprint
    write("""INSERT INTO supply_movement(shipment_id, timestamp, item, qty, source_hub, destination, status,
             prev_hash, hash) VALUES (:shipment_id, :timestamp, :item, :qty, :source_hub, :destination, :status,
             :prev_hash, :hash)""", **row)  # save it


def ledger_ok():  # True if nobody edited a past record
    return logic.verify_chain(read("SELECT * FROM supply_movement ORDER BY entry_id"))[0]  # engine checks the chain


# ---------------------------------------------------------------------
# Reading the current situation (plain lists for the engine)
# ---------------------------------------------------------------------
def current_shipments():  # latest step of each shipment
    return read("""SELECT * FROM supply_movement WHERE entry_id IN
                   (SELECT MAX(entry_id) FROM supply_movement GROUP BY shipment_id) ORDER BY entry_id DESC""")


def places():  # every place on the road network with its position: name -> (lat, lon)
    spots = {z["name"]: (z["lat"], z["lon"]) for z in read("SELECT name, lat, lon FROM zones")}  # zones
    spots.update({h["name"]: (h["lat"], h["lon"]) for h in read("SELECT name, lat, lon FROM hubs")})  # hubs
    spots.update({h["name"]: (h["lat"], h["lon"]) for h in read("SELECT name, lat, lon FROM hospitals")})  # hospitals
    return spots  # the lookup


def severity():  # severity of every zone (engine decides)
    zones = read("SELECT * FROM zones")  # all zones
    latest = read("""SELECT zone, disaster_type, probability FROM risk_logs
                     WHERE id IN (SELECT MAX(id) FROM risk_logs GROUP BY zone)""")  # newest reading per zone
    counts = read("SELECT zone, COUNT(*) AS n FROM incidents WHERE status='OPEN' GROUP BY zone")  # open SOS per zone
    return logic.compute_severity(zones, {r["zone"]: r for r in latest}, {r["zone"]: r["n"] for r in counts})


def situation():  # everything the engine needs, computed fresh
    roads = read("SELECT * FROM roads ORDER BY id")  # road network
    hubs = read("SELECT * FROM hubs ORDER BY id")  # hubs
    hospitals = read("SELECT * FROM hospitals ORDER BY id")  # hospitals
    inventory = read("SELECT * FROM inventory")  # stock
    zones = [z["name"] for z in read("SELECT name FROM zones")]  # zone names
    travel = logic.travel_table(roads, zones + [h["name"] for h in hubs],
                                zones + [h["name"] for h in hospitals])  # fastest routes
    sev = severity()  # zone severity
    active = [s for s in current_shipments() if s["status"] != "DISTRIBUTED"]  # shipments still travelling
    groups = [{"id": i["id"], "zone": i["zone"], "people": int(i["people"])} for i in
              read("SELECT * FROM incidents WHERE need_type='Medical' AND status != 'RESOLVED' ORDER BY id")]  # patients
    return {"roads": roads, "hubs": hubs, "hospitals": hospitals, "inventory": inventory, "travel": travel,
            "severity": sev, "active": active, "groups": groups}  # the bundle


# ---------------------------------------------------------------------
# RE-OPTIMISATION: run the engine, save a new plan version, report what changed
# ---------------------------------------------------------------------
def latest_version():  # newest plan version number (0 if none yet)
    rows = read("SELECT MAX(version) AS v FROM plan_runs")  # highest version
    return int(rows[0]["v"] or 0)  # 0 when empty


def plan_rows_of(version):  # the rows of one plan version
    return [dict(r, data=json.loads(r["data"]), key=r["plan_key"]) for r in
            read("SELECT * FROM plans WHERE version=:v ORDER BY id", v=version)]  # data back from text


def replan(trigger_type, trigger_text):  # event -> update state (already done) -> optimise -> save -> report
    s = situation()  # fresh data
    recs, shortfalls, solver = logic.plan_supplies(s["severity"], s["hubs"], s["inventory"], s["active"], s["travel"])  # supplies
    scores = {z["name"]: z["score"] for z in s["severity"]}  # zone -> score
    assignments, unplaced, _ = logic.plan_patients(s["groups"], s["hospitals"], s["travel"], scores)  # patients
    rows = logic.plan_rows(recs, assignments)  # both plans as rows
    old_version = latest_version()  # previous version
    diff = logic.diff_plans(plan_rows_of(old_version), rows)  # what changed
    version = old_version + 1  # the new version number
    for r in rows:  # save each allocation
        write("INSERT INTO plans(version, kind, plan_key, summary, data) VALUES (:v, :k, :key, :s, :d)",
              v=version, k=r["kind"], key=r["key"], s=r["summary"], d=json.dumps(r["data"]))
    write("DELETE FROM plans WHERE version < :v", v=version - 1)  # keep only this and the previous version
    write("""INSERT INTO plan_runs(version, created_at, trigger_type, trigger_text, solver, changed, changed_keys,
             shortfalls, unplaced) VALUES (:v, :t, :tt, :tx, :s, :c, :ck, :sf, :up)""",
          v=version, t=now(), tt=trigger_type, tx=trigger_text, s=solver, c=diff["count"],
          ck=json.dumps(diff["changed"] + diff["removed"]), sf=json.dumps(shortfalls), up=json.dumps(unplaced))  # history
    log_event("REOPTIMIZED", f"Plan v{version}: {diff['count']} allocations changed after {trigger_type}")  # feed line
    return {"version": version, "changed": diff["count"], "situation": s, "recs": recs, "assignments": assignments,
            "shortfalls": shortfalls, "unplaced": unplaced}  # details for the scenario checks


# ---------------------------------------------------------------------
# Demo data
# ---------------------------------------------------------------------
def seed_demo_data():  # fill an empty database from demo_data.py
    start = datetime.now()  # "now" for the history
    for name, lat, lon, pop, vul in demo.ZONES:  # zones
        write("INSERT INTO zones VALUES (:n, :la, :lo, :p, :v)", n=name, la=lat, lo=lon, p=pop, v=vul)
    for hid, name, lat, lon in demo.HUBS:  # hubs and their stock
        write("INSERT INTO hubs VALUES (:i, :n, :la, :lo)", i=hid, n=name, la=lat, lo=lon)
        for item, qty in demo.INVENTORY[hid].items():  # each item
            write("INSERT INTO inventory VALUES (:h, :i, :q)", h=hid, i=item, q=qty)
    for h in demo.HOSPITALS:  # hospitals
        write("""INSERT INTO hospitals VALUES (:i, :n, :z, :la, :lo, :tb, :ab, :it, :ia, :ox)""",
              i=h[0], n=h[1], z=h[2], la=h[3], lo=h[4], tb=h[5], ab=h[6], it=h[7], ia=h[8], ox=h[9])
    for rid, a, b, mins in demo.ROADS:  # roads, all OPEN
        write("INSERT INTO roads VALUES (:i, :a, :b, :m, 'OPEN')", i=rid, a=a, b=b, m=mins)
    for ts, zone, lat, lon, dtype, prob, src in demo.risk_history(start):  # 24 hours of readings
        write("""INSERT INTO risk_logs(timestamp, zone, lat, lon, disaster_type, probability, source)
                 VALUES (:t, :z, :la, :lo, :d, :p, :s)""", t=ts, z=zone, la=lat, lo=lon, d=dtype, p=prob, s=src)
    for ts, lat, lon, zone, need, people in demo.incident_rows(start):  # SOS requests
        write("""INSERT INTO incidents(reported_at, lat, lon, zone, need_type, people, status)
                 VALUES (:t, :la, :lo, :z, :n, :p, 'OPEN')""", t=ts, la=lat, lo=lon, z=zone, n=need, p=people)
    write("""INSERT INTO alerts(created_at, zone, disaster_type, severity, eta_hours, radius_km, sms_status)
             VALUES (:t, 'Ratnagiri', 'Cyclone', 'Red', 22, 40, 'SENT')""", t=now())  # one alert already sent
    add_ledger_entry("SHP-0001", "Medical kits", 150, "Kolhapur Depot", "Ratnagiri", "ALLOCATED")  # demo shipment
    add_ledger_entry("SHP-0001", "Medical kits", 150, "Kolhapur Depot", "Ratnagiri", "DISPATCHED")  # next step
    log_event("SYSTEM", "System started with demo data")  # first feed line
    replan("SYSTEM_START", "System started")  # first plan


def init_db():  # create missing tables; fill demo data if empty
    for statement in SCHEMA:  # each table
        write(statement)  # create if missing
    if read("SELECT COUNT(*) AS n FROM zones")[0]["n"] == 0:  # new database?
        seed_demo_data()  # fill it


def reset_db():  # wipe and start again
    for table in TABLES:  # every table
        write(f"DROP TABLE IF EXISTS {table}")  # delete it
    init_db()  # recreate with demo data
