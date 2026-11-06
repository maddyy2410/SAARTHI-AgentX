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
              "RISK_UPDATED": "🌀", "TAMPER": "🚨"}  # small symbol per event type in the feed

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
    open_ids = data.incident_ids(rows, "OPEN")  # empty on a fresh database must not reach selectbox
    transit_ids = data.incident_ids(rows, "IN_TRANSIT")
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
    "Scenario Tests": page_scenarios,
}
st.sidebar.title("🚨 Command Center")  # sidebar title
choice = st.sidebar.radio("Go to", list(PAGES.keys()))  # page menu
live = st.sidebar.toggle(f"Live mode (refresh every {REFRESH_SECONDS} s)", value=True)  # auto refresh on/off
sidebar_demos()  # demo buttons
st.fragment(PAGES[choice], run_every=REFRESH_SECONDS if live else None)()  # draw the page, re-reading data on a timer
