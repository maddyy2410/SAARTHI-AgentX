# =====================================================================
# main.py  :  OPTION B BACKEND (Python + FastAPI)
# ---------------------------------------------------------------------
# RUN (inside the backend folder):   uvicorn main:app --reload
# Try every endpoint at:             http://localhost:8000/docs
#
# EVENT-DRIVEN RE-OPTIMISATION (from our notes):
#   event arrives -> update the database -> run the optimiser (engine.py)
#   -> save the new plan -> tell every open dashboard over the WebSocket
# =====================================================================

import asyncio  # lets the WebSocket wait without blocking the server
import json  # read stored lists back from text
from contextlib import asynccontextmanager  # run code when the server starts
from typing import Literal, Optional  # Literal = only these exact words; Optional = may be empty

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect  # web framework pieces
from fastapi.middleware.cors import CORSMiddleware  # lets the React site (another port) call this API
from pydantic import BaseModel, Field  # checks incoming data

import db  # database helpers and re-optimisation (db.py)
import engine as logic  # shared decision logic (engine.py)


@asynccontextmanager  # "run at start, then keep running"
async def lifespan(app):  # called once when the server starts
    db.init_db()  # tables + demo data if needed
    yield  # server runs until stopped


app = FastAPI(title="Disaster Command Center API", lifespan=lifespan)  # the web application
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])  # allow the React site

STATE = {"version": 0, "last_event": ""}  # bumps on every change so WebSockets know to refresh


def event(event_type, message, replan=True):  # EVERY change goes through here
    db.log_event(event_type, message)  # 1. record the event in the feed
    result = db.replan(event_type, message) if replan else None  # 2. re-optimise (engine.py)
    STATE["version"] += 1  # 3. tell open dashboards
    STATE["last_event"] = message  # what happened
    return result  # plan details (used by scenario checks)


@app.websocket("/ws")  # live channel for the React app
async def live_updates(ws: WebSocket):
    await ws.accept()  # accept the browser
    seen = STATE["version"]  # what this browser already has
    try:  # until the browser disconnects
        while True:  # forever
            await asyncio.sleep(1)  # check once per second
            if STATE["version"] != seen:  # something changed
                seen = STATE["version"]  # remember
                await ws.send_json({"type": "refresh", "event": STATE["last_event"]})  # tell the browser
    except (WebSocketDisconnect, RuntimeError):  # tab closed
        pass  # nothing to clean up


# ---------------------------------------------------------------------
# INPUT MODELS: Pydantic rejects bad data before it reaches the database
# ---------------------------------------------------------------------
class RiskIn(BaseModel):  # scraper reading
    zone: str  # zone name
    disaster_type: str  # e.g. Cyclone
    probability: float = Field(ge=0, le=1)  # 0 to 1 only
    source: str = "scraper"  # where it came from


class AlertIn(BaseModel):  # new early warning
    zone: str
    disaster_type: str
    severity: Literal["Red", "Orange", "Yellow"]  # only these
    eta_hours: float = Field(gt=0, le=240)  # hours to impact
    radius_km: float = Field(gt=0, le=500)  # SMS radius


class AlertStatusIn(BaseModel):  # SMS sender reports back
    sms_status: Literal["QUEUED", "SENT", "FAILED"]


class IncidentIn(BaseModel):  # new SOS
    lat: float = Field(ge=-90, le=90)  # REQUIRED position
    lon: float = Field(ge=-180, le=180)  # REQUIRED position
    zone: Optional[str] = None  # zone if known
    need_type: Literal["Medical", "Food", "Water", "Shelter", "Rescue"]
    people: int = Field(ge=1, le=100000)  # for Medical: patients needing a bed


class ClaimIn(BaseModel):  # NGO claims a request
    claimed_by: str = Field(min_length=1)


class RoadIn(BaseModel):  # road condition change
    status: Literal["OPEN", "SLOW", "BLOCKED"]


class HospitalIn(BaseModel):  # hospital capacity change
    available_beds: int = Field(ge=0)
    icu_available: int = Field(ge=0)


class ReceiveIn(BaseModel):  # new stock arrives at a hub
    hub_id: int
    item: str
    qty: int = Field(gt=0)


class ApproveIn(BaseModel):  # approve one planned move (its plan key)
    plan_key: str


# ---------------------------------------------------------------------
# READ ENDPOINTS
# ---------------------------------------------------------------------
@app.get("/api/overview")  # headline numbers
def overview():
    sev = db.severity()  # zone severity
    run = db.read("SELECT * FROM plan_runs ORDER BY version DESC LIMIT 1")  # latest plan
    unplaced = sum(u["patients"] for u in json.loads(run[0]["unplaced"])) if run else 0  # patients without a bed
    return {
        "critical_zones": sum(1 for z in sev if z["level"] == "Critical"),
        "open_requests": db.read("SELECT COUNT(*) AS n FROM incidents WHERE status='OPEN'")[0]["n"],
        "patients_waiting": unplaced,
        "roads_blocked": db.read("SELECT COUNT(*) AS n FROM roads WHERE status='BLOCKED'")[0]["n"],
        "moving_shipments": sum(1 for s in db.current_shipments() if s["status"] in ("DISPATCHED", "IN_TRANSIT")),
        "alerts": db.read("SELECT COUNT(*) AS n FROM alerts")[0]["n"],
        "plan_version": run[0]["version"] if run else 0,
    }


@app.get("/api/severity")  # ranked zones
def get_severity():
    return db.severity()


@app.get("/api/risk-logs")  # scraper readings, newest first
def get_risk_logs(limit: int = 500):
    return db.read("SELECT * FROM risk_logs ORDER BY id DESC LIMIT :l", l=limit)


@app.get("/api/alerts")  # all alerts
def get_alerts():
    return db.read("SELECT * FROM alerts ORDER BY id DESC")


@app.get("/api/incidents")  # all SOS requests
def get_incidents():
    return db.read("SELECT * FROM incidents ORDER BY id DESC")


@app.get("/api/hubs")  # hubs
def get_hubs():
    return db.read("SELECT * FROM hubs ORDER BY id")


@app.get("/api/inventory")  # stock with hub names
def get_inventory():
    return db.read("""SELECT h.name AS hub, i.hub_id, i.item, i.qty FROM inventory i
                      JOIN hubs h ON h.id = i.hub_id ORDER BY h.id, i.item""")


@app.get("/api/hospitals")  # hospitals with capacity
def get_hospitals():
    return db.read("SELECT * FROM hospitals ORDER BY id")


@app.get("/api/roads")  # roads with both ends' positions (for drawing lines on the map)
def get_roads():
    spots = db.places()  # name -> (lat, lon)
    rows = db.read("SELECT * FROM roads ORDER BY id")  # all roads
    for r in rows:  # add positions and current travel time
        r["a_lat"], r["a_lon"] = spots.get(r["a"], (None, None))  # start
        r["b_lat"], r["b_lon"] = spots.get(r["b"], (None, None))  # end
        r["current_minutes"] = logic.road_minutes(r)  # None when blocked
    return rows


@app.get("/api/plan")  # the current plan: supply moves and patient assignments, with "changed" flags
def get_plan():
    run = db.read("SELECT * FROM plan_runs ORDER BY version DESC LIMIT 1")  # latest run
    if not run:  # nothing planned yet
        return {"version": 0, "supply": [], "patients": [], "shortfalls": [], "unplaced": []}
    r = run[0]  # the run
    changed = set(json.loads(r["changed_keys"]))  # keys that changed in this version
    rows = db.plan_rows_of(r["version"])  # its rows
    def pack(kind):  # rows of one kind, with key and changed flag
        return [dict(x["data"], key=x["key"], changed=x["key"] in changed and r["version"] > 1)
                for x in rows if x["kind"] == kind]
    return {"version": r["version"], "created_at": r["created_at"], "trigger_type": r["trigger_type"],
            "trigger": r["trigger_text"], "solver": r["solver"], "changed": r["changed"],
            "supply": pack("supply"), "patients": pack("patient"),
            "shortfalls": json.loads(r["shortfalls"]), "unplaced": json.loads(r["unplaced"])}


@app.get("/api/plan/history")  # the last 10 re-optimisations
def plan_history():
    return db.read("""SELECT version, created_at, trigger_type, trigger_text, solver, changed
                      FROM plan_runs ORDER BY version DESC LIMIT 10""")


@app.get("/api/shipments")  # latest step per shipment, with a map position while moving
def get_shipments():
    spots = db.places()  # positions
    rows = db.current_shipments()  # one row per shipment
    for s in rows:  # add a position halfway along (simple "where is it now")
        a, b = spots.get(s["source_hub"]), spots.get(s["destination"])  # both ends
        if a and b and s["status"] in ("DISPATCHED", "IN_TRANSIT"):  # only while moving
            s["lat"], s["lon"] = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2  # midpoint
    return rows


@app.get("/api/ledger")  # every ledger row
def get_ledger():
    return db.read("SELECT * FROM supply_movement ORDER BY entry_id")


@app.get("/api/ledger/verify")  # tamper check
def verify_ledger():
    rows = db.read("SELECT * FROM supply_movement ORDER BY entry_id")  # all rows
    ok, bad = logic.verify_chain(rows)  # engine checks
    return {"ok": ok, "first_bad_entry": bad, "total": len(rows)}


@app.get("/api/events")  # live feed, newest first
def get_events(limit: int = 20):
    return db.read("SELECT * FROM events ORDER BY id DESC LIMIT :l", l=limit)


@app.get("/api/checks")  # automatic checks on the current plan
def get_checks():
    return current_checks()


def current_checks(result=None, expect_change=True):  # runs engine.validate_plan on the latest plan
    plan = get_plan()  # current plan
    s = result["situation"] if result else db.situation()  # data
    changed = result["changed"] if result else plan["changed"]  # changes in the last run
    return logic.validate_plan(plan["supply"], plan["patients"], plan["shortfalls"], s["hospitals"], s["roads"],
                               s["inventory"], s["severity"], db.ledger_ok(), changed,
                               travel=s["travel"], expect_change=expect_change)


# ---------------------------------------------------------------------
# EVENT ENDPOINTS: each one changes data, then re-optimises
# ---------------------------------------------------------------------
@app.post("/api/risk-logs")  # SCRAPER: new reading
def add_risk_log(r: RiskIn):
    zone = db.read("SELECT lat, lon FROM zones WHERE name=:z", z=r.zone)  # find the zone
    if not zone:  # unknown
        raise HTTPException(404, f"Unknown zone {r.zone}")
    db.write("""INSERT INTO risk_logs(timestamp, zone, lat, lon, disaster_type, probability, source)
                VALUES (:t, :z, :la, :lo, :d, :p, :s)""", t=db.now(), z=r.zone, la=zone[0]["lat"],
             lo=zone[0]["lon"], d=r.disaster_type, p=r.probability, s=r.source)  # save
    event("RISK_UPDATED", f"{r.disaster_type} risk for {r.zone}: {r.probability:.0%} ({r.source})")
    return {"ok": True}


@app.post("/api/alerts")  # operator queues an alert
def add_alert(a: AlertIn):
    new_id = db.insert_returning_id("""INSERT INTO alerts(created_at, zone, disaster_type, severity, eta_hours,
             radius_km, sms_status) VALUES (:t, :z, :d, :s, :e, :r, 'QUEUED')""", t=db.now(), z=a.zone,
                                    d=a.disaster_type, s=a.severity, e=a.eta_hours, r=a.radius_km)  # save as QUEUED
    event("ALERT_QUEUED", f"{a.severity} {a.disaster_type} alert queued for {a.zone} (impact in {a.eta_hours:g}h)", replan=False)
    return {"id": new_id}


@app.patch("/api/alerts/{alert_id}")  # SMS SENDER reports back
def update_alert(alert_id: int, body: AlertStatusIn):
    db.write("UPDATE alerts SET sms_status=:s WHERE id=:i", s=body.sms_status, i=alert_id)
    event("ALERT_" + body.sms_status, f"Alert #{alert_id} SMS status: {body.sms_status}", replan=False)
    return {"ok": True}


@app.post("/api/incidents")  # citizen app / volunteer submits an SOS
def add_incident(i: IncidentIn):
    new_id = db.insert_returning_id("""INSERT INTO incidents(reported_at, lat, lon, zone, need_type, people, status)
             VALUES (:t, :la, :lo, :z, :n, :p, 'OPEN')""", t=db.now(), la=i.lat, lo=i.lon, z=i.zone,
                                    n=i.need_type, p=i.people)  # save as OPEN
    kind = "NEW_PATIENTS" if i.need_type == "Medical" else "NEW_SOS"  # medical requests need hospital beds
    event(kind, f"New {i.need_type} SOS in {i.zone or 'unknown zone'} ({i.people} people)")
    return {"id": new_id}


@app.patch("/api/incidents/{incident_id}/claim")  # NGO takes a request
def claim_incident(incident_id: int, body: ClaimIn):
    rows = db.read("SELECT status FROM incidents WHERE id=:i", i=incident_id)
    if not rows:
        raise HTTPException(404, "Request not found")
    if rows[0]["status"] != "OPEN":
        raise HTTPException(409, "Already claimed by another team")  # 409 = conflict
    db.write("UPDATE incidents SET status='IN_TRANSIT', claimed_by=:c WHERE id=:i", c=body.claimed_by, i=incident_id)
    event("SOS_CLAIMED", f"SOS #{incident_id} claimed by {body.claimed_by}")
    return {"ok": True}


@app.patch("/api/incidents/{incident_id}/resolve")  # close a request
def resolve_incident(incident_id: int):
    db.write("UPDATE incidents SET status='RESOLVED' WHERE id=:i", i=incident_id)
    event("SOS_RESOLVED", f"SOS #{incident_id} resolved")
    return {"ok": True}


@app.patch("/api/roads/{road_id}")  # EVENT: ROAD_BLOCKED / ROAD_SLOW / ROAD_REOPENED
def set_road(road_id: str, body: RoadIn):
    road = db.read("SELECT * FROM roads WHERE id=:i", i=road_id)
    if not road:
        raise HTTPException(404, "Road not found")
    db.write("UPDATE roads SET status=:s WHERE id=:i", s=body.status, i=road_id)
    kind = {"BLOCKED": "ROAD_BLOCKED", "SLOW": "ROAD_SLOW", "OPEN": "ROAD_REOPENED"}[body.status]
    event(kind, f"Road {road_id} ({road[0]['a']} to {road[0]['b']}) is now {body.status}")
    return {"ok": True}


@app.patch("/api/hospitals/{hospital_id}")  # EVENT: HOSPITAL_CAPACITY_CHANGED
def set_hospital(hospital_id: int, body: HospitalIn):
    h = db.read("SELECT * FROM hospitals WHERE id=:i", i=hospital_id)
    if not h:
        raise HTTPException(404, "Hospital not found")
    if body.available_beds > h[0]["total_beds"] or body.icu_available > h[0]["icu_total"]:
        raise HTTPException(400, "Free beds cannot exceed total beds")
    db.write("UPDATE hospitals SET available_beds=:a, icu_available=:c WHERE id=:i",
             a=body.available_beds, c=body.icu_available, i=hospital_id)
    event("HOSPITAL_CAPACITY_CHANGED",
          f"{h[0]['name']}: {body.available_beds} beds and {body.icu_available} ICU beds free")
    return {"ok": True}


@app.post("/api/inventory/receive")  # EVENT: RESOURCE_RECEIVED
def receive_stock(body: ReceiveIn):
    hub = db.read("SELECT name FROM hubs WHERE id=:i", i=body.hub_id)
    if not hub:
        raise HTTPException(404, "Hub not found")
    if db.read("SELECT qty FROM inventory WHERE hub_id=:h AND item=:i", h=body.hub_id, i=body.item):  # item exists
        db.write("UPDATE inventory SET qty = qty + :q WHERE hub_id=:h AND item=:i", q=body.qty, h=body.hub_id, i=body.item)
    else:  # new item at this hub
        db.write("INSERT INTO inventory VALUES (:h, :i, :q)", h=body.hub_id, i=body.item, q=body.qty)
    event("RESOURCE_RECEIVED", f"{body.qty} {body.item} arrived at {hub[0]['name']}")
    return {"ok": True}


@app.post("/api/plan/run")  # re-optimise on demand
def run_plan():
    result = event("MANUAL", "Operator asked for a fresh plan")
    return {"version": result["version"], "changed": result["changed"]}


def create_shipment(item, qty, hub_id, destination):  # shared by "approve" and "approve all"
    hub = db.read("SELECT name FROM hubs WHERE id=:i", i=hub_id)
    stock = db.read("SELECT qty FROM inventory WHERE hub_id=:h AND item=:it", h=hub_id, it=item)
    if not hub or not stock or stock[0]["qty"] < qty:
        raise HTTPException(400, "Not enough stock at that hub")
    count = db.read("SELECT COUNT(DISTINCT shipment_id) AS n FROM supply_movement")[0]["n"]
    sid = f"SHP-{int(count) + 1:04d}"  # next shipment number
    db.add_ledger_entry(sid, item, qty, hub[0]["name"], destination, "ALLOCATED")  # first ledger step
    db.write("UPDATE inventory SET qty = qty - :q WHERE hub_id=:h AND item=:it", q=qty, h=hub_id, it=item)  # stock out
    if stock[0]["qty"] - qty == 0:  # hub ran out
        db.log_event("RESOURCE_DEPLETED", f"{hub[0]['name']} has no {item} left")
    return sid


@app.post("/api/shipments")  # operator approves one planned move
def approve(body: ApproveIn):
    plan = get_plan()  # current plan
    move = next((m for m in plan["supply"] if m["key"] == body.plan_key), None)  # find it
    if move is None:
        raise HTTPException(409, "That move is no longer in the plan. The plan was updated; refresh.")
    sid = create_shipment(move["item"], move["qty"], move["hub_id"], move["zone"])
    event("SHIPMENT_ALLOCATED", f"{sid}: {move['qty']} {move['item']} from {move['from_hub']} to {move['zone']}")
    return {"shipment_id": sid}


@app.post("/api/shipments/approve-all")  # approve the whole supply plan at once
def approve_all():
    plan = get_plan()
    ids = [create_shipment(m["item"], m["qty"], m["hub_id"], m["zone"]) for m in plan["supply"]]
    event("SHIPMENT_ALLOCATED", f"{len(ids)} shipments allocated from plan v{plan['version']}")
    return {"shipments": ids}


@app.post("/api/shipments/{shipment_id}/advance")  # next step for a shipment
def advance_shipment(shipment_id: str):
    rows = db.read("SELECT * FROM supply_movement WHERE shipment_id=:s ORDER BY entry_id DESC LIMIT 1", s=shipment_id)
    if not rows:
        raise HTTPException(404, "Shipment not found")
    r, flow = rows[0], logic.STATUS_FLOW  # latest row, list of steps
    pos = flow.index(r["status"])  # where it is
    if pos == len(flow) - 1:
        raise HTTPException(400, "Shipment already distributed")
    nxt = flow[pos + 1]  # next step
    db.add_ledger_entry(shipment_id, r["item"], r["qty"], r["source_hub"], r["destination"], nxt)  # NEW row only
    event("SHIPMENT_" + nxt, f"{shipment_id} is now {nxt}", replan=(nxt == "DISTRIBUTED"))
    return {"status": nxt}


# ---------------------------------------------------------------------
# SCENARIOS (from our notes) + demo buttons
# ---------------------------------------------------------------------
SCENARIOS = {  # id -> (title, what it does)
    "A": ("Normal conditions", "Reset to the starting situation"),
    "B": ("Hospital capacity decreases", "Ratnagiri Civil Hospital fills up"),
    "C": ("Major road blocked", "Roads R8 and R9 out of Kolhapur Depot are cut"),
    "D": ("New relief resources arrive", "Extra food and medical kits reach Kolhapur Depot"),
    "E": ("Several changes at once", "Cyclone worsens, road R6 blocked, Thane Civil fills, new patients in Raigad"),
}


@app.get("/api/scenarios")  # list of scenarios
def list_scenarios():
    return [{"id": k, "title": t, "description": d} for k, (t, d) in SCENARIOS.items()]


def add_risk(zone, dtype, prob):  # helper: fake scraper reading
    z = db.read("SELECT lat, lon FROM zones WHERE name=:z", z=zone)[0]
    db.write("""INSERT INTO risk_logs(timestamp, zone, lat, lon, disaster_type, probability, source)
                VALUES (:t, :z, :la, :lo, :d, :p, 'IMD (demo)')""", t=db.now(), z=zone, la=z["lat"], lo=z["lon"],
             d=dtype, p=prob)


@app.post("/api/scenarios/{scenario_id}")  # run a scenario, then return the automatic checks
def run_scenario(scenario_id: str):
    if scenario_id not in SCENARIOS:
        raise HTTPException(404, "Unknown scenario")
    title = SCENARIOS[scenario_id][0]
    if scenario_id == "A":  # normal
        db.reset_db()
        result = event("SCENARIO_A", "Scenario A: normal conditions")
    elif scenario_id == "B":  # capacity drop
        db.write("UPDATE hospitals SET available_beds=0, icu_available=0 WHERE id=4")
        result = event("HOSPITAL_CAPACITY_CHANGED", "Scenario B: Ratnagiri Civil Hospital is full")
    elif scenario_id == "C":  # road blocked
        db.write("UPDATE roads SET status='BLOCKED' WHERE id IN ('R8', 'R9')")
        result = event("ROAD_BLOCKED", "Scenario C: roads R8 and R9 out of Kolhapur Depot blocked")
    elif scenario_id == "D":  # resources arrive
        db.write("UPDATE inventory SET qty = qty + 1500 WHERE hub_id=4 AND item='Food kits'")
        db.write("UPDATE inventory SET qty = qty + 600 WHERE hub_id=4 AND item='Medical kits'")
        result = event("RESOURCE_RECEIVED", "Scenario D: 1500 food kits and 600 medical kits reached Kolhapur Depot")
    else:  # E: several at once
        add_risk("Ratnagiri", "Cyclone", 0.95)
        add_risk("Raigad", "Cyclone", 0.85)
        db.write("UPDATE roads SET status='BLOCKED' WHERE id='R6'")
        db.write("UPDATE hospitals SET available_beds=20 WHERE id=2")
        lat, lon = db.read("SELECT lat, lon FROM zones WHERE name='Raigad'")[0].values()
        db.write("""INSERT INTO incidents(reported_at, lat, lon, zone, need_type, people, status)
                    VALUES (:t, :la, :lo, 'Raigad', 'Medical', 40, 'OPEN')""", t=db.now(), la=lat + 0.03, lo=lon + 0.02)
        db.write("""INSERT INTO alerts(created_at, zone, disaster_type, severity, eta_hours, radius_km, sms_status)
                    VALUES (:t, 'Raigad', 'Cyclone', 'Red', 16, 50, 'QUEUED')""", t=db.now())
        result = event("MULTIPLE_EVENTS", "Scenario E: cyclone worsens, R6 blocked, Thane Civil fills, 40 new patients in Raigad")
    return {"scenario": scenario_id, "title": title, "version": result["version"],
            "changed": result["changed"], "checks": current_checks(result, expect_change=scenario_id != "A")}


@app.post("/api/demo/{name}")  # other demo buttons
def demo(name: str):
    if name == "cyclone":  # prediction demo
        add_risk("Ratnagiri", "Cyclone", 0.95)
        add_risk("Sindhudurg", "Cyclone", 0.85)
        db.write("""INSERT INTO alerts(created_at, zone, disaster_type, severity, eta_hours, radius_km, sms_status)
                    VALUES (:t, 'Sindhudurg', 'Cyclone', 'Red', 18, 50, 'QUEUED')""", t=db.now())
        event("RISK_UPDATED", "Scraper: cyclone risk jumped near Ratnagiri. Auto alert queued for Sindhudurg")
    elif name == "tamper":  # secretly edit an old ledger record
        db.write("UPDATE supply_movement SET qty = qty + 500 WHERE entry_id = (SELECT MIN(entry_id) FROM supply_movement)")
        event("TAMPER", "Someone edited a past supply record directly in the database", replan=False)
    else:
        raise HTTPException(404, "Unknown demo")
    return {"ok": True}
