# =====================================================================
# app.py  :  OPTION A (Streamlit + SQLite)  :  the Command Center screens
# ---------------------------------------------------------------------
# HOW TO RUN (in a terminal, inside this folder):
#     pip install -r requirements.txt
#     streamlit run app.py
#
# HOW THE PIECES FIT:
#     app.py        -> draws the screens and passes clicks to data.py   (YOU)
#     data.py       -> saves changes, then asks engine.py for a new plan
#     engine.py     -> all decisions: routes, severity, OR-Tools plans, checks
#     demo_data.py  -> the made-up demo world
#     disaster.db   -> the SQLite database file (created automatically)
#
# app.py never makes decisions. It shows what data.py and engine.py return,
# so the algorithm can change without touching these screens.
# =====================================================================

import pandas as pd  # pandas: tables of data (like Excel inside Python)
import plotly.express as px  # plotly express: quick charts
import plotly.graph_objects as go  # plotly graph objects: charts with full control
import pydeck as pdk  # pydeck: the map
import streamlit as st  # streamlit: turns this file into a web page

import data  # our database and actions (data.py)
import engine  # our decision logic (engine.py), used here only for small helpers
import intake_agent  # citizen reports: classify, validate and prioritise
import ripple_agent  # read-only cascade analysis; it does not change records
import whatif_agent  # hypothetical scenarios on an in-memory copy; it does not write

# ---------------------------------------------------------------------
# 1. SETTINGS
# ---------------------------------------------------------------------
REFRESH_SECONDS = 10  # live mode re-reads data this often
LEVEL_HEX = {"Critical": "#b91c1c", "High": "#d9580d", "Moderate": "#c99a06", "Low": "#2f7d4f"}  # level colours for charts
LEVEL_RGB = {"Critical": [185, 28, 28], "High": [217, 88, 13], "Moderate": [201, 154, 6], "Low": [47, 125, 79]}  # same, for the map
ROAD_RGB = {"OPEN": [107, 114, 128], "SLOW": [217, 88, 13], "BLOCKED": [185, 28, 28]}  # road colours: grey, orange, red
SOS_RGB = {"OPEN": [185, 28, 28], "IN_TRANSIT": [217, 88, 13], "RESOLVED": [47, 125, 79]}  # SOS dot colours
EVENT_ICON = {"ROAD_BLOCKED": "⛔", "ROAD_SLOW": "🐢", "ROAD_REOPENED": "✅", "HOSPITAL_CAPACITY_CHANGED": "🏥",
              "NEW_PATIENTS": "🚑", "RESOURCE_RECEIVED": "📦", "RESOURCE_DEPLETED": "⚠️", "REOPTIMIZED": "🔁",
              "RISK_UPDATED": "🌀", "TAMPER": "🚨", "INTAKE_SUBMITTED": "📥", "INTAKE_APPROVED": "✅",
              "INTAKE_REJECTED": "🚫"}  # small symbol per event type in the feed

st.set_page_config(page_title="Disaster Command Center", page_icon="🚨", layout="wide")  # tab title, icon, full width


# ---------------------------------------------------------------------
# 2. SMALL HELPERS
# ---------------------------------------------------------------------
def df(rows):  # list of dicts -> pandas table
    return pd.DataFrame(rows)  # pandas does the conversion


def act(fn, *args, ok="Done"):  # runs an action from data.py, then redraws the page
    try:  # the action may refuse (for example: not enough stock)
        fn(*args)  # do it
        st.toast(ok)  # small confirmation pop-up
        st.rerun()  # redraw everything with the new data
    except data.ActionError as e:  # refused on purpose
        st.error(str(e))  # show why, in red


def draw_map(layers, height=480, key="map"):  # map with switchable layers
    choices = ["Risk zones", "Roads", "Hospitals", "Supply hubs", "SOS requests", "Shipments"]  # every layer
    shown = st.multiselect("Map layers", choices, default=layers, key=key)  # operator picks which to show
    deck_layers = []  # the pydeck layers to draw, bottom first
    if "Risk zones" in shown:  # big circles for zones
        z = df(data.severity())  # zones with scores
        z["color"] = z["level"].map(LEVEL_RGB)  # colour by level
        z["radius"] = 8000 + z["score"] * 500  # bigger = worse (metres)
        z["label"] = z["name"] + ": " + z["level"] + ", score " + z["score"].astype(str)  # hover text
        deck_layers.append(pdk.Layer("ScatterplotLayer", z, get_position="[lon, lat]", get_fill_color="color",
                                     get_radius="radius", opacity=0.25, pickable=True))  # add the layer
    if "Roads" in shown:  # one line per road
        r = df(data.roads_with_positions())  # roads with both ends' positions
        r["path"] = r.apply(lambda x: [[x["a_lon"], x["a_lat"]], [x["b_lon"], x["b_lat"]]], axis=1)  # line points
        r["color"] = r["status"].map(ROAD_RGB)  # colour by condition
        r["width"] = r["status"].map({"OPEN": 3, "SLOW": 6, "BLOCKED": 6})  # problem roads drawn thicker
        r["label"] = r["id"] + ": " + r["a"] + " to " + r["b"] + ", " + r["status"]  # hover text
        deck_layers.append(pdk.Layer("PathLayer", r, get_path="path", get_color="color", get_width="width",
                                     width_units="pixels", pickable=True))  # add the layer
    if "Hospitals" in shown:  # purple dots
        h = df(data.read("SELECT * FROM hospitals"))  # hospitals
        h["color"] = h["available_beds"].apply(lambda b: [107, 79, 187] if b > 0 else [156, 163, 175])  # grey when full
        h["label"] = h["name"] + ": " + h["available_beds"].astype(str) + "/" + h["total_beds"].astype(str) + " beds free"
        deck_layers.append(pdk.Layer("ScatterplotLayer", h, get_position="[lon, lat]", get_fill_color="color",
                                     get_radius=4500, pickable=True))  # add the layer
    if "Supply hubs" in shown:  # blue dots
        hb = df(data.read("SELECT * FROM hubs"))  # hubs
        hb["label"] = hb["name"] + " (supply hub)"  # hover text
        deck_layers.append(pdk.Layer("ScatterplotLayer", hb, get_position="[lon, lat]", get_fill_color=[31, 95, 209],
                                     get_radius=5000, pickable=True))  # add the layer
    if "SOS requests" in shown:  # small SOS dots
        i = df(data.read("SELECT * FROM incidents WHERE status != 'RESOLVED'"))  # active requests
        if len(i):  # only if any
            i["color"] = i["status"].map(SOS_RGB)  # red open, orange in transit
            i["label"] = "SOS #" + i["id"].astype(str) + " " + i["need_type"] + ": " + i["people"].astype(str) + " people"
            deck_layers.append(pdk.Layer("ScatterplotLayer", i, get_position="[lon, lat]", get_fill_color="color",
                                         get_radius=2200, pickable=True))  # add the layer
    if "Shipments" in shown:  # yellow dots halfway along moving shipments
        s = df([x for x in data.shipments_with_positions() if "lat" in x])  # moving ones only
        if len(s):  # only if any
            s["label"] = s["shipment_id"] + ": " + s["qty"].astype(str) + " " + s["item"] + " to " + s["destination"]
            deck_layers.append(pdk.Layer("ScatterplotLayer", s, get_position="[lon, lat]", get_fill_color=[242, 193, 78],
                                         get_line_color=[17, 24, 39], stroked=True, line_width_min_pixels=2,
                                         get_radius=3500, pickable=True))  # add the layer
    st.pydeck_chart(pdk.Deck(layers=deck_layers,  # put the layers together
                             initial_view_state=pdk.ViewState(latitude=18.2, longitude=73.4, zoom=6.3),  # start view
                             tooltip={"text": "{label}"}, map_style=None), height=height)  # hover text, theme map
    st.caption("Zones: red critical, orange high, yellow moderate, green low. Roads: grey open, orange slow, "
               "red blocked. Purple: hospitals (grey when full). Blue: hubs. Yellow: shipments on the move.")  # legend


# ---------------------------------------------------------------------
# 3. PAGES
# ---------------------------------------------------------------------
def page_overview():  # PAGE 1: whole situation
    st.title("🚨 Situation overview")  # heading
    sev = data.severity()  # zones
    plan = data.get_plan()  # current plan
    c = st.columns(5)  # five number tiles
    c[0].metric("Critical zones", sum(1 for z in sev if z["level"] == "Critical"))  # tile 1
    c[1].metric("Open SOS requests", data.read("SELECT COUNT(*) AS n FROM incidents WHERE status='OPEN'")[0]["n"])  # tile 2
    c[2].metric("Patients without a bed", sum(u["patients"] for u in plan["unplaced"]))  # tile 3
    c[3].metric("Roads blocked", data.read("SELECT COUNT(*) AS n FROM roads WHERE status='BLOCKED'")[0]["n"])  # tile 4
    c[4].metric("Shipments on the move",
                sum(1 for s in data.current_shipments() if s["status"] in ("DISPATCHED", "IN_TRANSIT")))  # tile 5
    left, right = st.columns([2, 1])  # map (wide) and feed (narrow)
    with left:  # left column
        draw_map(["Risk zones", "Roads", "Hospitals", "Supply hubs", "SOS requests", "Shipments"], key="ov_map")  # map
    with right:  # right column
        st.subheader("System events")  # feed heading
        for e in data.read("SELECT * FROM events ORDER BY id DESC LIMIT 14"):  # 14 newest events
            icon = EVENT_ICON.get(e["type"], "•")  # symbol for its type
            st.markdown(f"`{e['timestamp'][11:16]}` {icon} **{e['type'].replace('_', ' ').lower()}**  \n{e['message']}")  # one line
    st.subheader("Risk probability over the last 24 hours")  # chart heading
    logs = df(data.read("SELECT timestamp, zone, probability FROM risk_logs ORDER BY id"))  # all readings
    fig = px.line(logs, x="timestamp", y="probability", color="zone", markers=True)  # one line per zone (Plotly)
    fig.update_layout(yaxis_tickformat=".0%", yaxis_range=[0, 1], height=320, margin=dict(t=10, b=10))  # % axis
    st.plotly_chart(fig, use_container_width=True)  # show it


def page_alerts():  # PAGE 2: early warnings
    st.title("⚠️ Early warnings (24-hour alerts)")  # heading
    alerts = df(data.read("SELECT * FROM alerts ORDER BY id DESC"))  # all alerts
    if len(alerts):  # any?
        deadline = pd.to_datetime(alerts["created_at"]) + pd.to_timedelta(alerts["eta_hours"], unit="h")  # impact time
        left = (deadline - pd.Timestamp.now()).dt.total_seconds() / 3600  # hours from now
        alerts["time_to_impact"] = left.apply(lambda h: "Impact window reached" if h <= 0 else f"{int(h)}h {int((h % 1) * 60)}m")
        st.dataframe(alerts[["id", "created_at", "zone", "disaster_type", "severity", "time_to_impact", "radius_km",
                             "sms_status"]], use_container_width=True, hide_index=True)  # table
    st.subheader("Issue a new alert")  # form heading
    st.caption("Saved as QUEUED. The backend SMS sender (for example Twilio) sends it and marks it SENT.")  # explanation
    with st.form("new_alert", clear_on_submit=True):  # nothing saves until the button is pressed
        c1, c2, c3 = st.columns(3)  # three columns
        zone = c1.selectbox("Zone", [z[0] for z in data.demo.ZONES])  # zone
        dtype = c2.selectbox("Disaster type", data.demo.DISASTER_TYPES)  # type
        sev = c3.selectbox("Severity", ["Red", "Orange", "Yellow"])  # severity
        eta = st.slider("Expected impact in (hours)", 1, 72, 24)  # hours to impact
        radius = st.slider("SMS radius (km)", 5, 100, 30)  # radius
        if st.form_submit_button("Queue alert", type="primary"):  # pressed
            act(data.add_alert, zone, dtype, sev, eta, radius, ok="Alert queued for SMS")  # save
    st.subheader("Latest risk readings from the scraper")  # heading
    st.dataframe(df(data.read("SELECT timestamp, zone, disaster_type, probability, source FROM risk_logs "
                              "ORDER BY id DESC LIMIT 20")), use_container_width=True, hide_index=True)  # table


def page_severity():  # PAGE 3: which area is worst
    st.title("📊 Severity and vulnerability")  # heading
    sev = df(data.severity())  # zones
    fig = px.bar(sev.sort_values("score"), x="score", y="name", orientation="h", color="level",
                 color_discrete_map=LEVEL_HEX, text="score", labels={"name": "", "score": "Severity score"})  # bars
    fig.update_layout(height=360, margin=dict(t=10, b=10), xaxis_range=[0, 100])  # size and scale
    st.plotly_chart(fig, use_container_width=True)  # show it
    st.dataframe(sev[["name", "level", "score", "disaster_type", "probability", "vulnerability", "population",
                      "open_requests"]], use_container_width=True, hide_index=True)  # details
    with st.expander("How is the score calculated?"):  # click to open
        st.markdown("**Severity = probability × vulnerability × 100**, plus up to 10 points for open SOS requests. "
                    "Critical 50+, High 30+, Moderate 15+, otherwise Low. The formula lives in engine.py.")  # text


def page_allocation():  # PAGE 4: the optimiser's decisions with reasons
    st.title("🧭 Allocation")  # heading
    plan = data.get_plan()  # current plan
    head, buttons = st.columns([3, 1])  # summary + buttons
    head.markdown(f"**Plan v{plan['version']}** · {plan.get('created_at', '')[11:16]} · "
                  f"**{plan['changed']} allocations changed** after *{plan.get('trigger_type', '').replace('_', ' ').lower()}*  \n"
                  f"{plan.get('trigger', '')} · Solver: {plan.get('solver', '')}")  # summary text
    if buttons.button("Re-optimise now"):  # manual re-run
        act(data.event, "MANUAL", "Operator asked for a fresh plan", ok="Plan recalculated")
    if buttons.button("Approve all moves", type="primary", disabled=not plan["supply"]):  # approve everything
        act(data.approve_all, ok="Every supply move approved")

    st.subheader("Supply plan")  # section
    if not plan["supply"]:  # nothing to move
        st.success("No moves needed right now. Every high-risk zone is covered.")
    for n, r in enumerate(plan["supply"]):  # one card per move
        box = st.container(border=True)  # bordered card
        a, b = box.columns([5, 1])  # text | button
        tag = " 🔵 **Changed**" if r["changed"] else ""  # marks moves that changed in this version
        a.markdown(f"**{r['level']}**{tag} · **Move {r['qty']} {r['item']}** from *{r['from_hub']}* to **{r['zone']}** "
                   f"· {engine.fmt_minutes(r['minutes'])}  \n{r['reason']}")  # move + reason
        if b.button("Approve", key=f"ap_{n}_{r['key']}", type="primary"):  # approve this one
            act(data.approve, r["key"], ok=f"{r['qty']} {r['item']} approved")
    for s in plan["shortfalls"]:  # anything nobody could supply
        st.error(f"Could not cover {s['missing']} {s['item']} for {s['zone']}: {s['why']}")

    st.subheader("Patient plan (hospitals)")  # section
    if not plan["patients"]:  # no medical requests
        st.info("No medical requests waiting for a bed.")
    for a in plan["patients"]:  # one card per assignment
        tag = " 🔵 **Changed**" if a["changed"] else ""  # changed marker
        st.container(border=True).markdown(
            f"**{a['patients']} patients**{tag} from SOS #{a['incident_id']} ({a['zone']}) → **{a['hospital']}** "
            f"· {engine.fmt_minutes(a['minutes'])}  \n{a['reason']}")  # assignment + reason
    for u in plan["unplaced"]:  # patients with no bed
        st.error(f"No bed found for {u['patients']} patients from SOS #{u['incident_id']} ({u['zone']})")

    st.subheader("Recent re-optimisations")  # history
    st.dataframe(df(data.plan_history()), use_container_width=True, hide_index=True)  # table


def page_network():  # PAGE 5: roads, hospitals, stock (every change is an event)
    st.title("🛣️ Roads and hospitals")  # heading
    roads = data.roads_with_positions()  # roads
    st.subheader("Road network")  # section
    show = df(roads)[["id", "a", "b", "minutes", "current_minutes", "status"]]  # columns to show
    st.dataframe(show, use_container_width=True, hide_index=True)  # table
    with st.form("road_form"):  # change one road
        c1, c2, c3 = st.columns([2, 2, 1])  # road | condition | button
        rid = c1.selectbox("Road", [r["id"] + ": " + r["a"] + " to " + r["b"] for r in roads])  # choose road
        status = c2.selectbox("Condition", ["OPEN", "SLOW", "BLOCKED"])  # choose condition
        if c3.form_submit_button("Update road", type="primary"):  # pressed
            act(data.set_road, rid.split(":")[0], status, ok="Road updated, plan recalculated")  # event

    st.subheader("Hospital capacity")  # section
    hs = df(data.read("SELECT * FROM hospitals ORDER BY id"))  # hospitals
    fig = go.Figure([go.Bar(name="Occupied beds", x=hs["name"], y=hs["total_beds"] - hs["available_beds"], marker_color="#9ca3af"),
                     go.Bar(name="Free beds", x=hs["name"], y=hs["available_beds"], marker_color="#6b4fbb")])  # stacked bars
    fig.update_layout(barmode="stack", height=300, margin=dict(t=10, b=10))  # stack them
    st.plotly_chart(fig, use_container_width=True)  # show it
    with st.form("hospital_form"):  # change one hospital
        c1, c2, c3, c4 = st.columns([3, 1, 1, 1])  # hospital | beds | ICU | button
        name = c1.selectbox("Hospital", hs["name"].tolist())  # choose hospital
        row = hs[hs["name"] == name].iloc[0]  # its row
        beds = c2.number_input("Free beds", 0, int(row["total_beds"]), int(row["available_beds"]))  # beds
        icu = c3.number_input("Free ICU", 0, int(row["icu_total"]), int(row["icu_available"]))  # ICU
        if c4.form_submit_button("Save", type="primary"):  # pressed
            act(data.set_hospital, int(row["id"]), int(beds), int(icu), ok="Capacity updated, plan recalculated")

    st.subheader("Stock by hub")  # section
    inv = df(data.read("SELECT h.name AS hub, i.item, i.qty FROM inventory i JOIN hubs h ON h.id=i.hub_id"))  # stock
    st.dataframe(inv.pivot(index="hub", columns="item", values="qty"), use_container_width=True)  # hubs x items
    with st.form("receive_form"):  # new supplies arrive
        c1, c2, c3, c4 = st.columns([2, 2, 1, 1])  # hub | item | qty | button
        hubs = data.read("SELECT id, name FROM hubs ORDER BY id")  # hubs
        hub = c1.selectbox("Hub", [h["name"] for h in hubs])  # choose hub
        item = c2.selectbox("Item", data.demo.ITEMS)  # choose item
        qty = c3.number_input("Quantity", 1, 100000, 500)  # how many
        if c4.form_submit_button("Add stock", type="primary"):  # pressed
            act(data.receive_stock, next(h["id"] for h in hubs if h["name"] == hub), item, int(qty), ok="Stock received")


def page_tracking():  # PAGE 6: shipments + tamper check
    st.title("🚚 Supply tracking (tamper-evident)")  # heading
    ok, bad, total = data.verify_ledger()  # check the chain
    if ok:  # intact
        st.success(f"Ledger verified: all {total} records intact. No record has been altered.")
    else:  # broken
        st.error(f"Tampering detected at ledger record #{bad}. The hash chain no longer matches.")
    ships = df(data.current_shipments())  # latest step per shipment
    if not len(ships):  # none
        st.info("No shipments yet. Approve a move on the Allocation page.")
        return  # stop here
    st.dataframe(ships[["shipment_id", "item", "qty", "source_hub", "destination", "status", "timestamp"]],
                 use_container_width=True, hide_index=True)  # table
    sid = st.selectbox("Open a shipment", ships["shipment_id"].tolist())  # choose one
    row = ships[ships["shipment_id"] == sid].iloc[0]  # its row
    flow = engine.STATUS_FLOW  # steps
    pos = flow.index(row["status"]) if row["status"] in flow else 0  # where it is
    cols = st.columns(len(flow))  # one column per step
    for k, step in enumerate(flow):  # draw the steps
        cols[k].markdown(("✅ " if k <= pos else "⬜ ") + step.replace("_", " ").title())  # tick or box
    if pos < len(flow) - 1 and st.button(f"Mark as {flow[pos + 1].replace('_', ' ').title()}", type="primary"):  # next step
        act(data.advance_shipment, sid, ok="Shipment updated")
    with st.expander("Full ledger (every status change, chained by SHA-256 fingerprint)"):  # click to open
        led = df(data.read("SELECT * FROM supply_movement ORDER BY entry_id"))  # all rows
        led["prev_hash"] = led["prev_hash"].str[:12] + "..."  # shorten
        led["hash"] = led["hash"].str[:12] + "..."  # shorten
        st.dataframe(led, use_container_width=True, hide_index=True)  # table


def page_requests():  # PAGE 7: SOS from the field
    st.title("🆘 Field requests")  # heading
    draw_map(["Hospitals", "Supply hubs", "SOS requests"], height=400, key="rq_map")  # map
    rows = data.read("SELECT * FROM incidents ORDER BY id DESC")  # all requests
    open_rows = [r for r in rows if r["status"] == "OPEN"]
    open_ids = data.incident_ids(rows, "OPEN")  # empty on a fresh database must not reach selectbox
    transit_ids = data.incident_ids(rows, "IN_TRANSIT")
    st.subheader("Open requests")
    st.caption("A request logged from Intake & Triage appears here while its status is OPEN. "
               "Claim and resolve are unchanged. Nothing on this page dispatches a responder.")
    if open_rows:
        st.dataframe(df(open_rows)[["id", "reported_at", "zone", "need_type", "people", "status", "claimed_by"]],
                     use_container_width=True, hide_index=True)
    else:
        st.info("No open requests.")
    st.subheader("All requests")
    if rows:
        st.dataframe(df(rows)[["id", "reported_at", "zone", "need_type", "people", "status", "claimed_by"]],
                     use_container_width=True, hide_index=True)  # table
    else:
        st.info("No field requests yet.")
    c1, c2 = st.columns(2)  # claim | close
    with c1:  # claim form
        st.subheader("Claim a request")
        if not open_ids:
            st.info("No open requests to claim.")
        else:
            with st.form("claim"):
                rid = st.selectbox("Request #", open_ids)  # open ones
                team = st.text_input("Team / NGO name", "Red Cross Team 3")  # who
                if st.form_submit_button("Claim (mark in transit)", type="primary") and rid:  # pressed
                    act(data.claim_incident, int(rid), team, ok=f"SOS #{rid} claimed")
    with c2:  # close form
        st.subheader("Close a request")
        if not transit_ids:
            st.info("No claimed requests to close.")
        else:
            with st.form("resolve"):
                rid2 = st.selectbox("Request #", transit_ids)  # in transit
                if st.form_submit_button("Mark resolved") and rid2:  # pressed
                    act(data.resolve_incident, int(rid2), ok=f"SOS #{rid2} resolved")
    with st.expander("Log a new SOS (Medical requests need hospital beds and go into the patient plan)"):  # new SOS
        with st.form("new_sos", clear_on_submit=True):
            zone = st.selectbox("Zone", [z[0] for z in data.demo.ZONES])  # zone
            need = st.selectbox("Need", data.demo.NEED_TYPES)  # need
            people = st.number_input("People affected (for Medical: patients)", 1, 5000, 10)  # how many
            if st.form_submit_button("Submit SOS"):  # pressed
                act(data.add_incident, zone, need, int(people), ok="SOS logged, plan recalculated")


def page_scenarios():  # PAGE 8: the scenario tests from our notes
    st.title("🧪 Scenario tests")  # heading
    st.warning("LIVE DEMO DATABASE MODIFICATION. Scenarios A–E can change actual demo database records, "
               "including roads, hospital beds, stock, risk readings, incidents, alerts, and the stored plan. "
               "This is separate from What-If, which does not write to the database.")
    st.caption("Run a scenario and the system re-plans by itself. The checks confirm the new plan is safe. "
               "Scenarios add up: run A to start fresh.")  # explanation
    cols = st.columns(len(data.SCENARIOS))  # one card per scenario
    for col, (sid, (title, desc)) in zip(cols, data.SCENARIOS.items()):  # each scenario
        with col.container(border=True):  # card
            st.markdown(f"**Scenario {sid}**  \n{title}")  # title
            st.caption(desc)  # description
            if st.button("Run", key=f"sc_{sid}", type="primary"):  # run it
                st.session_state["scenario_result"] = data.run_scenario(sid)  # keep the result for after the redraw
                st.rerun()  # redraw
    res = st.session_state.get("scenario_result")  # last result, if any
    if res:  # show it
        st.subheader(f"Scenario {res['scenario']}: {res['title']}")  # heading
        st.markdown(f"Plan v{res['version']}: **{res['changed']} allocations changed.** See the Allocation page for details.")
        for c in res["checks"]:  # one line per check
            (st.success if c["ok"] else st.error)(f"{'✓' if c['ok'] else '✗'} {c['check']}: {c['detail']}")  # green or red


# ---------------------------------------------------------------------
# 4. SIDEBAR: menu, live mode, demo buttons
# ---------------------------------------------------------------------
def page_intake():  # PAGE 9: Intake and Triage agent
    st.title("📥 Intake and triage")
    st.caption("Rule-based triage of reports already in the demo database and of new practice reports. "
               "It does not predict a disaster, send an alert, or dispatch a responder. "
               "A person approves or rejects every report.")
    if st.session_state.get("intake_notice"):
        st.success(st.session_state.pop("intake_notice"))
    if st.session_state.get("intake_error"):
        st.error(st.session_state.pop("intake_error"))
    reports = intake_agent.list_reports()
    pending = [r for r in reports if intake_agent.queue_state(r) == "Pending"]
    approved_only = [r for r in reports if intake_agent.queue_state(r) == "Approved, not logged"]
    logged = [r for r in reports if r["sos_logged"]]
    rejected = [r for r in reports if r["review_status"] == "rejected"]
    c = st.columns(4)
    c[0].metric("Pending", len(pending))
    c[1].metric("Approved, not logged", len(approved_only))
    c[2].metric("Logged as field requests", len(logged))
    c[3].metric("Rejected", len(rejected))
    left, right = st.columns(2)
    if left.button("Import existing SOS", type="primary"):
        added = intake_agent.import_open_sos()
        st.toast(f"Imported {len(added)} existing SOS records" if added else "Existing SOS records are already in the queue")
        st.rerun()
    if right.button("Load labelled synthetic examples"):
        added = intake_agent.load_synthetic_examples()
        st.toast(f"Loaded {len(added)} synthetic examples" if added else "Synthetic examples are already loaded")
        st.rerun()
    if reports:
        view = st.radio("Show", ["Pending", "Approved, not logged", "Logged", "Rejected", "All"], horizontal=True)
        if view == "Logged":
            visible = [r for r in reports if r["sos_logged"]]
        elif view == "All":
            visible = reports
        else:
            visible = [r for r in reports if intake_agent.queue_state(r) == view]
        if not visible:
            st.info(f"No {view.lower()} reports.")
        else:
            table = []
            for r in visible:
                table.append({"id": r["id"], "record": "Synthetic demo" if r["synthetic"] else "Operator entry",
                              "queue": intake_agent.queue_state(r), "zone": r["zone"], "classification": r["classification"],
                              "severity": r["severity"], "urgency": intake_agent.urgency_label(r["source"], r["urgency"]),
                              "people": r["people"], "priority": r["priority"], "priority_score": r["priority_score"],
                              "validation_status": r["validation_status"]})
            st.dataframe(df(table), use_container_width=True, hide_index=True)
            choice = st.selectbox("Open a report", [f"#{r['id']} · {intake_agent.queue_state(r)} · {r['priority']} · {r['zone'] or 'no zone'}" for r in visible])
            row = next(r for r in visible if choice.startswith(f"#{r['id']} ·"))
            st.markdown(f"**{intake_agent.queue_state(row)}** · **{row['priority']}** · score {row['priority_score']} · "
                        f"{'Synthetic demo' if row['synthetic'] else 'Operator entry'}")
            st.markdown(f"**Urgency:** {intake_agent.urgency_label(row['source'], row['urgency'])}")
            parts = df([
                {"Part": "Urgency", "Contribution": row["score_urgency"]},
                {"Part": "People affected", "Contribution": row["score_people"]},
                {"Part": "Severity", "Contribution": row["score_severity"]},
                {"Part": "Zone vulnerability", "Contribution": row["score_vulnerability"]},
                {"Part": "Total", "Contribution": row["priority_score"]},
            ])
            st.dataframe(parts, use_container_width=True, hide_index=True)
            st.caption("This is a rule-based prioritisation score, not an AI prediction or a disaster forecast.")
            st.write(row["priority_reason"])
            if row["validation_notes"]:
                st.warning(row["validation_notes"])
            else:
                st.success("No validation warnings.")
            reviewer = st.text_input("Reviewer", "Duty officer", key=f"reviewer_{row['id']}")
            if row["sos_logged"] and row["incident_id"]:
                st.success(f"Logged as field request #{row['incident_id']}. It is listed under Open requests on Field Requests "
                           "until someone claims it. No alert was sent and no responder was dispatched.")
            elif row["review_status"] == "pending":
                a, b, c3 = st.columns(3)
                if a.button("Approve", type="primary"):
                    act(intake_agent.review_report, int(row["id"]), "approved", reviewer, ok=f"Report #{row['id']} approved. It is not a field request yet.")
                if b.button("Reject"):
                    act(intake_agent.review_report, int(row["id"]), "rejected", reviewer, ok=f"Report #{row['id']} rejected. Nothing was dispatched.")
                if c3.button("Approve and log SOS"):
                    _log_field_request(int(row["id"]), reviewer)
            elif row["review_status"] == "approved":
                st.warning(f"Approved by {row['reviewed_by'] or 'an operator'}. This report is not on Field Requests yet.")
                if st.button("Log as field request"):
                    _log_field_request(int(row["id"]), reviewer)
            else:
                st.info(f"Rejected by {row['reviewed_by'] or 'an operator'}. Nothing was dispatched.")
        mapped = [r for r in reports if r["lat"] is not None and r["lon"] is not None]
        if mapped:
            colours = {"CRITICAL": [185, 28, 28], "HIGH": [217, 88, 13], "MEDIUM": [201, 154, 6], "LOW": [47, 125, 79]}
            points = df(mapped)
            points["color"] = points["priority"].map(colours)
            points["label"] = points["priority"] + " " + points["classification"].fillna("") + " #" + points["id"].astype(str)
            st.pydeck_chart(pdk.Deck(layers=[pdk.Layer("ScatterplotLayer", points, get_position="[lon, lat]",
                                                       get_fill_color="color", get_radius=6000, pickable=True)],
                                     initial_view_state=pdk.ViewState(latitude=18.2, longitude=73.4, zoom=6.3),
                                     tooltip={"text": "{label}"}, map_style=None), height=320)
    else:
        st.info("No reports yet. Import the existing SOS records, load the labelled examples, or enter a report below.")
    with st.expander("Enter a citizen report"):
        with st.form("intake_report", clear_on_submit=True):
            zones = ["Unknown"] + [z["name"] for z in data.read("SELECT name FROM zones ORDER BY name")]
            c1, c2, c3 = st.columns(3)
            zone = c1.selectbox("Zone", zones)
            disaster = c2.selectbox("Disaster type", ["Not sure", "Flood", "Heatwave", "Cyclone", "Earthquake", "Fire", "Landslide"])
            urgency = c3.selectbox("Urgency", ["Not specified", "CRITICAL", "HIGH", "MEDIUM", "LOW"])
            d1, d2, d3 = st.columns(3)
            lat = d1.text_input("Latitude", "")
            lon = d2.text_input("Longitude", "")
            people = d3.text_input("People affected", "")
            description = st.text_area("Description", "")
            st.caption("Urgency is required. Not specified is left missing and is not scored as LOW.")
            if st.form_submit_button("Triage this report", type="primary"):
                form_error = intake_agent.citizen_form_error(urgency)
                if form_error:
                    st.session_state["intake_error"] = form_error
                else:
                    intake_agent.submit({"source": "citizen", "zone": zone, "lat": lat, "lon": lon, "people": people,
                                         "disaster_type": disaster, "urgency": urgency, "description": description, "synthetic": 0})
                    st.session_state["intake_notice"] = "Report triaged and added to the pending queue. It is not a field request until you log it."
                st.rerun()


def _log_field_request(report_id, reviewer):
    """Save one open field request and keep its id on screen after the redraw."""
    try:
        saved = intake_agent.approve_and_log_sos(report_id, reviewer)
        if saved["source"] == "existing_sos":
            st.session_state["intake_notice"] = (
                f"Field request #{saved['incident_id']} is already stored from the original SOS. "
                "No second request was created. Open Field Requests and look under Open requests."
            )
        else:
            st.session_state["intake_notice"] = (
                f"Field request #{saved['incident_id']} was stored as OPEN for {saved['zone']}. "
                "Open Field Requests and look under Open requests. No alert was sent and no responder was dispatched."
            )
        st.rerun()
    except data.ActionError as exc:
        st.error(str(exc))


def page_ripple():  # PAGE 10: read-only cascade analysis
    st.title("🌊 Ripple")
    st.caption("Deterministic scenario analysis of the stored coastal Maharashtra network. "
               "Recorded facts and simulated consequences are labelled separately. "
               "A road or facility is described as disrupted only when its stored status says so. "
               "This page does not change incidents, roads, hospitals, stock, shipments, or the plan, "
               "and it does not send an alert or dispatch a responder.")
    names = [z["name"] for z in data.read("SELECT name FROM zones ORDER BY name")]
    origin = st.selectbox("Origin zone", names, index=names.index("Ratnagiri") if "Ratnagiri" in names else 0)
    result = ripple_agent.analyse(origin)
    st.info(result["disclaimer"])
    c = st.columns(4)
    c[0].metric("Primary zone", result["primary"]["zone"])
    c[1].metric("Secondary zones", len(result["secondary"]))
    c[2].metric("Extended zones", len(result["extended"]))
    c[3].metric("Plan version read", result["plan_version"])
    _ripple_map(result)
    left, right = st.columns(2)
    with left:
        st.subheader("Primary impact")
        st.caption("Recorded facts at the origin.")
        _ripple_zone_card(result["primary"])
    with right:
        st.subheader("Secondary impact")
        st.caption("One stored zone-hop away. A neighbour is a simulated consequence, not a confirmed impact.")
        if result["secondary"]:
            for zone in result["secondary"]:
                _ripple_zone_card(zone)
        else:
            st.info("No secondary zone is connected by a stored zone-to-zone road.")
    st.subheader("Extended impact")
    if result["extended"]:
        st.write(", ".join(zone["zone"] for zone in result["extended"]))
    else:
        st.info("No zone is two hops away on the stored zone roads.")
    st.subheader("Cascade timeline")
    for stage in result["timeline"]:
        with st.container(border=True):
            st.markdown(f"**{stage['stage']}**")
            st.caption(stage["reason"])
            for line in stage["lines"]:
                st.markdown(f"**{line['tag']}.** {line['text']}")
    st.subheader("Full-network and zone-only bridges")
    st.caption("Both checks remove a road only in memory. The stored road status is not changed. "
               "A zone-only bridge can still have an alternate route through a hub.")
    full_rows = [{"Road": b["id"], "Ends": f"{b['a']} — {b['b']}",
                  "Unreachable from first end": ", ".join(b["unreachable_from_a"])}
                 for b in result["bridges"]["full_network"]]
    zone_rows = [{"Road": b["id"], "Ends": f"{b['a']} — {b['b']}", "Note": b["reason"]} for b in result["bridges"]["zone_graph"]]
    fcol, zcol = st.columns(2)
    fcol.markdown("**Full network, including hubs and hospitals**")
    fcol.dataframe(df(full_rows), use_container_width=True, hide_index=True)
    zcol.markdown("**Zone names only**")
    zcol.dataframe(df(zone_rows), use_container_width=True, hide_index=True)
    for compared in result["bridges"]["comparisons"]:
        if compared["zone_graph_bridge"] and not compared["full_network_bridge"]:
            st.markdown(f"**{compared['id']}** · {compared['note']}")
    st.subheader("Hospital pressure")
    st.dataframe(df([{"Zone": h["zone"], "Record": h["text"], "ICU": h["icu"], "Oxygen": h["oxygen"]}
                     for h in result["hospitals"]]), use_container_width=True, hide_index=True)
    st.subheader("Supply reach")
    st.caption("Demand comes from the existing supply-need rule. No allocation is created and inventory is not changed. "
               "A missing stock row is not shown as zero.")
    for row in result["supply"]:
        st.markdown(f"**{row['zone']}.** {row['text']}")
    for hub in result["reach"]:
        with st.expander(hub["route"]):
            st.dataframe(df([{"Item": s["item"], "Stock": s["text"]} for s in hub["stock"]]),
                         use_container_width=True, hide_index=True)
    st.subheader("Heuristic attention score")
    st.caption("Score = severity (max 40) + hospital pressure (max 25) + open medical patients (max 20) "
               "+ full-network connectivity (15, 8, or 0). A missing contribution is not treated as zero, and that zone is not ranked.")
    ranked = result["recommendations"]["ranked"]
    if ranked:
        st.dataframe(df([{"Rank": i + 1, "Zone": r["zone"], "Score": r["score"], "Severity": r["severity"],
                          "Hospital": r["hospital"], "Medical": r["medical"], "Connectivity": r["connectivity"]}
                         for i, r in enumerate(ranked)]), use_container_width=True, hide_index=True)
        choice = st.selectbox("Why this rank", [r["zone"] for r in ranked])
        chosen = next(r for r in ranked if r["zone"] == choice)
        for reason in chosen["reasons"]:
            st.write(reason)
    else:
        st.info("No affected zone has every contribution recorded.")
    if result["recommendations"]["unranked"]:
        st.markdown("**Not ranked — missing data**")
        st.dataframe(df([{"Zone": r["zone"], "Missing": ", ".join(r["missing"]), "Hospital record": r["hospital_text"]}
                         for r in result["recommendations"]["unranked"]]), use_container_width=True, hide_index=True)


def _ripple_map(result):
    roles = {result["primary"]["zone"]: "primary"}
    roles.update({z["zone"]: "secondary" for z in result["secondary"]})
    roles.update({z["zone"]: "extended" for z in result["extended"]})
    colours = {"primary": [185, 28, 28], "secondary": [217, 88, 13], "extended": [201, 154, 6], "other": [156, 163, 175]}
    points = []
    for zone in data.read("SELECT name, lat, lon FROM zones"):
        role = roles.get(zone["name"], "other")
        points.append({"lat": zone["lat"], "lon": zone["lon"], "color": colours[role],
                       "radius": 14000 if role == "primary" else 9000, "label": f"{zone['name']}: {role}"})
    roads = df(data.roads_with_positions())
    roads["path"] = roads.apply(lambda x: [[x["a_lon"], x["a_lat"]], [x["b_lon"], x["b_lat"]]], axis=1)
    roads["color"] = roads["status"].map(ROAD_RGB)
    roads["width"] = roads["status"].map({"OPEN": 3, "SLOW": 6, "BLOCKED": 6})
    roads["label"] = roads["id"] + ": " + roads["a"] + " to " + roads["b"] + ", " + roads["status"]
    st.pydeck_chart(pdk.Deck(layers=[
        pdk.Layer("PathLayer", roads, get_path="path", get_color="color", get_width="width", width_units="pixels", pickable=True),
        pdk.Layer("ScatterplotLayer", df(points), get_position="[lon, lat]", get_fill_color="color", get_radius="radius",
                  opacity=0.8, pickable=True),
    ], initial_view_state=pdk.ViewState(latitude=18.2, longitude=73.4, zoom=6.3), tooltip={"text": "{label}"},
        map_style=None), height=420)
    st.caption("Red: primary zone. Orange: secondary. Yellow: extended. Grey: outside this ring. "
               "Roads use the stored status: grey open, orange slow, red blocked.")


def _ripple_zone_card(zone):
    risk = zone["risk"]
    if risk is None:
        reading = "No risk reading recorded."
    else:
        reading = f"Recorded reading: {risk['disaster_type']} probability {float(risk['probability']):.2f}."
    st.markdown(f"**{zone['zone']}** · {reading} {zone['hospital']['text']}.")
    open_roads = ", ".join(f"{r['id']} {r['status']}" for r in zone["roads"]) or "No road recorded"
    st.caption(open_roads)


def page_whatif():  # PAGE 11: hypothetical scenarios, never written to the database
    st.title("🔀 What-If")
    st.warning(whatif_agent.DISCLAIMER)
    st.caption("Each run reads the database again and applies the hypothetical list to a new in-memory copy. "
               "Reset clears that list. It does not run Scenario A and it does not rewrite the demo database.")
    modifications = st.session_state.setdefault("whatif_mods", [])
    _whatif_controls(modifications)
    if modifications:
        st.subheader("Hypothetical modifications")
        for note_index, modification in enumerate(modifications, start=1):
            st.markdown(f"{note_index}. `{modification['kind']}` · {modification}")
    else:
        st.info("No hypothetical changes. The comparison below is the RECORDED baseline.")
    if st.button("Reset to recorded baseline"):
        st.session_state["whatif_mods"] = whatif_agent.reset_modifications()
        st.rerun()
    try:
        result = whatif_agent.compare(modifications)
    except whatif_agent.ScenarioError as exc:
        st.error(str(exc))
        return
    st.caption(f"RECORDED plan version {result['recorded_plan_version']} was read and not replaced.")
    for note in result["modifications"]:
        st.markdown(f"**SIMULATED.** {note}")
    _whatif_maps(result)
    st.subheader("Routes")
    changed = result["routes"]
    if changed:
        st.dataframe(df([{"From": r["from"], "To": r["to"], "RECORDED minutes": r["recorded_minutes"],
                          "SIMULATED minutes": r["simulated_minutes"], "RECORDED roads": ", ".join(r["recorded_roads"]),
                          "SIMULATED roads": ", ".join(r["simulated_roads"])} for r in changed]),
                     use_container_width=True, hide_index=True)
    else:
        st.success("No route minutes changed.")
    isolated = [row for row in result["isolation"] if row["label"] == "SIMULATED"]
    st.subheader("Isolated zones and inaccessible facilities")
    if isolated:
        for row in isolated:
            st.markdown(row["text"])
    else:
        st.info("This hypothetical list does not newly isolate a zone or cut off a facility that was reachable.")
    st.subheader("Hospital capacity")
    st.dataframe(df([{"Hospital": h["name"], "Zone": h["zone"], "RECORDED free beds": h["recorded_free"],
                      "SIMULATED free beds": h["simulated_free"], "RECORDED occupancy": h["recorded_occupancy"],
                      "SIMULATED occupancy": h["simulated_occupancy"], "ICU": h["icu"], "Label": h["label"]}
                     for h in result["hospitals"]]), use_container_width=True, hide_index=True)
    st.caption(f"RECORDED unplaced patients: {result['patients']['recorded_unplaced']}. "
               f"SIMULATED unplaced patients: {result['patients']['simulated_unplaced']}. "
               "No patient record was moved.")
    if not any(h["zone"] == "Palghar" for h in result["hospitals"]):
        st.caption("Palghar: no hospital recorded, so hypothetical beds cannot be invented.")
    st.subheader("Resource availability")
    st.dataframe(df([{"Hub": s["hub"], "Item": s["item"], "RECORDED qty": s["recorded_qty"],
                      "SIMULATED qty": s["simulated_qty"], "Label": s["text"]} for s in result["stock"]]),
                 use_container_width=True, hide_index=True)
    st.caption(f"SIMULATED supply calculation changed {result['supply']['changed']} route rows. "
               "No shipment was created and inventory was not reduced in the database.")
    for shortfall in result["supply"]["simulated_shortfalls"]:
        st.markdown(f"**SIMULATED shortfall.** {shortfall['zone']}: {shortfall['missing']} {shortfall['item']}. {shortfall['why']}")
    st.subheader("Zone severity")
    st.dataframe(df([{"Zone": s["zone"], "RECORDED probability": s["recorded_probability"],
                      "SIMULATED probability": s["simulated_probability"], "RECORDED score": s["recorded_score"],
                      "SIMULATED score": s["simulated_score"], "RECORDED level": s["recorded_level"],
                      "SIMULATED level": s["simulated_level"], "Higher priority": s["higher_priority"],
                      "Label": s["label"]} for s in result["severity"]]),
                 use_container_width=True, hide_index=True)
    st.dataframe(df([{"Road": r["id"], "Ends": f"{r['a']} — {r['b']}", "RECORDED": r["recorded_status"],
                      "SIMULATED": r["simulated_status"], "RECORDED minutes": r["recorded_minutes"],
                      "SIMULATED minutes": r["simulated_minutes"], "Meaning": r["meaning"]}
                     for r in result["roads"]]), use_container_width=True, hide_index=True)


def _whatif_controls(modifications):
    kind = st.selectbox("Add a hypothetical change", ["Road status", "Hospital free beds", "Hub stock", "Zone probability"])
    if kind == "Road status":
        roads = data.read("SELECT id, a, b, status FROM roads ORDER BY id")
        label = st.selectbox("Road", [f"{r['id']}: {r['a']} to {r['b']} (RECORDED {r['status']})" for r in roads])
        status = st.selectbox("SIMULATED status", ["BLOCKED", "SLOW", "OPEN"])
        if st.button("Add road change"):
            _whatif_add(modifications, {"kind": "road", "road_id": label.split(":")[0], "status": status})
    elif kind == "Hospital free beds":
        hospitals = data.read("SELECT id, name, total_beds, available_beds FROM hospitals ORDER BY id")
        label = st.selectbox("Hospital", [f"{h['id']}: {h['name']} (RECORDED {h['available_beds']} of {h['total_beds']} free)" for h in hospitals])
        chosen = next(h for h in hospitals if label.startswith(f"{h['id']}:"))
        beds = st.number_input("SIMULATED free beds", min_value=0, max_value=int(chosen["total_beds"]), value=int(chosen["available_beds"]), step=1)
        if st.button("Add hospital change"):
            _whatif_add(modifications, {"kind": "beds", "hospital_id": int(chosen["id"]), "available_beds": int(beds)})
    elif kind == "Hub stock":
        hubs = data.read("SELECT id, name FROM hubs ORDER BY id")
        hub_label = st.selectbox("Hub", [f"{h['id']}: {h['name']}" for h in hubs])
        hub_id = int(hub_label.split(":")[0])
        rows = data.read("SELECT item, qty FROM inventory WHERE hub_id=:h ORDER BY item", h=hub_id)
        if not rows:
            st.info("No stock row recorded at this hub.")
        else:
            item_label = st.selectbox("Item", [f"{r['item']} (RECORDED {r['qty']})" for r in rows])
            item = item_label.split(" (RECORDED ")[0]
            recorded_qty = int(next(r["qty"] for r in rows if r["item"] == item))
            qty = st.number_input("SIMULATED quantity", min_value=0, max_value=recorded_qty, value=recorded_qty, step=1)
            if st.button("Add stock change"):
                _whatif_add(modifications, {"kind": "stock", "hub_id": hub_id, "item": item, "qty": int(qty)})
    else:
        risks = data.read("""SELECT zone, probability FROM risk_logs
                              WHERE id IN (SELECT MAX(id) FROM risk_logs GROUP BY zone) ORDER BY zone""")
        if not risks:
            st.info("No risk reading is recorded.")
        else:
            label = st.selectbox("Zone", [f"{r['zone']} (RECORDED probability {float(r['probability']):.2f})" for r in risks])
            zone = label.split(" (RECORDED")[0]
            recorded = float(next(r["probability"] for r in risks if r["zone"] == zone))
            probability = st.number_input("SIMULATED probability", min_value=recorded, max_value=1.0, value=recorded, step=0.01, format="%.2f")
            if st.button("Add severity change"):
                _whatif_add(modifications, {"kind": "probability", "zone": zone, "probability": float(probability)})


def _whatif_add(modifications, modification):
    updated = list(modifications) + [modification]
    try:
        whatif_agent.compare(updated)
    except whatif_agent.ScenarioError as exc:
        st.error(str(exc))
        return
    st.session_state["whatif_mods"] = updated
    st.rerun()


def _whatif_maps(result):
    spots = data.places()
    left, right = st.columns(2)
    with left:
        st.markdown("**RECORDED roads**")
        _whatif_map(result, spots, "recorded_status", "whatif_recorded_map")
    with right:
        st.markdown("**SIMULATED roads**")
        _whatif_map(result, spots, "simulated_status", "whatif_simulated_map")


def _whatif_map(result, spots, status_field, key):
    lines = []
    for road in result["roads"]:
        start, end = spots.get(road["a"]), spots.get(road["b"])
        if not start or not end:
            continue
        status = road[status_field]
        lines.append({"path": [[start[1], start[0]], [end[1], end[0]]], "color": ROAD_RGB.get(status, [107, 114, 128]),
                      "width": 3 if status == "OPEN" else 6,
                      "label": f"{road['id']}: {road['a']} to {road['b']}, {status_field} {status}"})
    if not lines:
        st.info("No mapped roads.")
        return
    frame = df(lines)
    st.pydeck_chart(pdk.Deck(layers=[pdk.Layer("PathLayer", frame, get_path="path", get_color="color", get_width="width",
                                               width_units="pixels", pickable=True)],
                             initial_view_state=pdk.ViewState(latitude=18.2, longitude=73.4, zoom=6.3),
                             tooltip={"text": "{label}"}, map_style=None), height=360, key=key)


def sidebar_demos():  # demo buttons
    st.sidebar.divider()  # line
    st.sidebar.subheader("🎬 Demo controls")  # heading
    if st.sidebar.button("Cyclone intensifies"):  # prediction demo
        act(data.demo_cyclone, ok="Cyclone risk updated")
    for sid in "BCDE":  # quick scenario buttons
        if st.sidebar.button(f"{sid} · {data.SCENARIOS[sid][0]}"):  # pressed
            st.session_state["scenario_result"] = data.run_scenario(sid)  # run and remember
            st.rerun()  # redraw
    if st.sidebar.button("Tamper with a ledger record"):  # ledger demo
        act(data.demo_tamper, ok="A past record was edited")
    if st.sidebar.button("A · Reset to normal"):  # start over
        st.session_state["scenario_result"] = data.run_scenario("A")  # reset
        st.rerun()  # redraw


# ---------------------------------------------------------------------
# 5. MAIN: runs every time the page loads or something is clicked
# ---------------------------------------------------------------------
data.init_db()  # make sure the database exists
PAGES = {  # menu label -> page function
    "Overview": page_overview,
    "Early Warnings": page_alerts,
    "Severity & Vulnerability": page_severity,
    "Allocation (optimiser)": page_allocation,
    "Roads & Hospitals": page_network,
    "Supply Tracking": page_tracking,
    "Field Requests": page_requests,
    "Intake & Triage": page_intake,
    "Ripple": page_ripple,
    "What-If": page_whatif,
    "Scenario Tests": page_scenarios,
}
st.sidebar.title("🚨 Command Center")  # sidebar title
choice = st.sidebar.radio("Go to", list(PAGES.keys()))  # page menu
live = st.sidebar.toggle(f"Live mode (refresh every {REFRESH_SECONDS} s)", value=True)  # auto refresh on/off
sidebar_demos()  # demo buttons
st.fragment(PAGES[choice], run_every=REFRESH_SECONDS if live else None)()  # draw the page, re-reading data on a timer
