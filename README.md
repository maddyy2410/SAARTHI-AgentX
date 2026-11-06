# Disaster Response Command Center

Two complete versions of the same command center, built to the technical roadmap in our notes:

| Folder | Stack | Best when |
|---|---|---|
| `option-b-react-fastapi/` | **The stack from our notes:** React + Leaflet + Plotly (frontend), Python + FastAPI + SQLAlchemy + Pydantic (backend), PostgreSQL (or SQLite), Google OR-Tools, WebSockets, SHA-256 hash chain | The team follows the roadmap; someone is comfortable with JavaScript |
| `option-a-streamlit/` | Python only: Streamlit + Plotly + pydeck + SQLite, same OR-Tools engine | Time is tight or the whole team knows only Python (our skeleton drawing) |

Both have the same eight pages, the same demo data and scenarios, and use **the same `engine.py` and `demo_data.py`**. Each folder has its own README with setup steps.

---

## What is covered from our notes

| Roadmap item | Where |
|---|---|
| Client–server architecture, modular backend (no microservices) | Option B: `backend/` API + `frontend/` React |
| Data layer: PostgreSQL + SQLAlchemy + Pydantic | `backend/db.py`, `backend/main.py` (tested on PostgreSQL 16 and SQLite) |
| Capacity and availability change while running | Roads & Hospitals page: road condition, free beds, new stock |
| REST API; the frontend never touches the database | `backend/main.py`; full list in the Option B README |
| Optimisation engine: **Google OR-Tools** | `engine.py`: `plan_supplies()` and `plan_patients()` (GLOP linear programming) |
| Engine independent of the UI | `engine.py` has no screen or database code |
| **Dynamic re-optimisation** on events | `ROAD_BLOCKED`, `ROAD_SLOW`, `ROAD_REOPENED`, `HOSPITAL_CAPACITY_CHANGED`, `NEW_PATIENTS`, `RESOURCE_RECEIVED`, `RESOURCE_DEPLETED` and more: each one triggers update → optimise → save → dashboard refresh |
| Road network with OPEN / SLOW / BLOCKED converted to travel time | `engine.travel_table()` (shortest routes; slow = double time; blocked = unusable) |
| Simulated disaster on a real map | Leaflet + OpenStreetMap (B), pydeck (A) |
| Tracking with IDs and statuses ALLOCATED → DISPATCHED → IN_TRANSIT → RECEIVED → DISTRIBUTED | Supply Tracking page |
| Tamper-evident hash chain instead of blockchain | `engine.make_hash()` / `verify_chain()` |
| Dashboard: live map (hospitals, relief hubs, roads, resources) | Overview map with layer switches |
| Dashboard: allocation panel **with reasons** (capacity, travel time) | Allocation page |
| Dashboard: resource tracking timeline | Supply Tracking page |
| Dashboard: system events feed ("Road R8 blocked", "Re-optimisation: 21 allocations changed") | Overview → System events |
| Real-time updates over WebSockets | Option B `/ws`; Option A live mode |
| Charts with Plotly | Both options |
| Testing scenarios A–E with automatic checks | Scenario Tests page |
| ML prediction (optional, only after everything works) | **Not built**, as the roadmap advises. The design keeps prediction separate from decisions, so a model can feed risk readings later |
| Kafka / RabbitMQ | **Not needed**; mention as the scalable future design |
| Real routing (OSRM / GraphHopper) | **Simulated** road network with real map positions, as the roadmap recommends for a hackathon |

Our skeleton scope (risk zones, supply hubs, SMS warnings, SOS map) and the roadmap scope (hospitals, patients, roads) are now **both** in the app: medical SOS requests become patients that the optimiser assigns to hospitals.

---

## Requirement: the dashboard must be independent of changes

**From our meeting notes:** *"The optimization engine should be independent of the UI. That allows you to change the algorithm later without rebuilding the dashboard."* The notes also ask for **dynamic re-optimisation**: when something happens, the system recalculates and the dashboard updates by itself.

**Status: met in both options.** Three rules make it work:

1. **Logic is separate from the screen.** All decisions live in `engine.py`. Screens only display what it returns, including the reason text.
2. **Screens use an agreed contract only.** Option A: the database tables. Option B: the API. The screen never depends on how the backend works inside.
3. **The screen refreshes itself.** Every event re-runs the optimiser, saves a new plan version, and the dashboard reloads without anyone clicking.

| Kind of change | Option A (Streamlit) | Option B (React + FastAPI) |
|---|---|---|
| **The algorithm changes** (new formula, a different OR-Tools model, an ML prediction) | ✅ Independent: edit `engine.py`, screens unchanged | ✅ Independent: same `engine.py`, inside the backend |
| **The data changes** (road blocked, hospital fills, new patients, stock arrives) | ✅ Re-optimises on the event; live mode re-reads every 10 s | ✅ Re-optimises on the event; WebSocket pushes the update instantly |
| **The database changes** (SQLite to PostgreSQL) | ⚠️ Needs code changes | ✅ One setting (`DATABASE_URL`), tested on both |
| **The backend is rewritten or moved** | ⚠️ Screens call `data.py` directly | ✅ Only `src/api.js` knows the backend address |
| **Someone renames a table, column, API address or field** | ❌ Breaks | ❌ Breaks |

**Keep it independent:** agree the contract in the first hour, and keep `engine.py` functions taking and returning the same shape of data.

### Show it to judges

1. Open **Allocation**. Note the moves from Kolhapur Depot to Ratnagiri and Sindhudurg (roads R8 and R9).
2. Click **C · Major road blocked** in the sidebar.
3. Without touching the screen, the event feed shows `road blocked` then `reoptimized` with how many allocations changed, the map shows the roads in red, and the Allocation page marks every changed move.
4. Open **Scenario Tests**: every check is green (capacity respected, blocked roads avoided, priorities kept, ledger intact).

---

## Scenarios (from our notes)

| Scenario | What happens | Expected result |
|---|---|---|
| A · Normal conditions | Reset to the starting data | Baseline plan |
| B · Hospital capacity decreases | Ratnagiri Civil Hospital fills up | Its patients move to the next nearest hospital with beds |
| C · Major road blocked | Roads R8 and R9 out of Kolhapur Depot cut | Supplies re-route over longer roads or from other hubs |
| D · New relief resources arrive | Extra food and medical kits at Kolhapur Depot | Plan uses the closer stock; long trips drop |
| E · Several changes at once | Cyclone worsens, road R6 cut, Thane Civil fills, 40 new patients in Raigad | Plan adapts to all of them; cut-off zones are reported clearly |

Each scenario runs these automatic checks: allocation changed, hospital capacity respected, blocked roads avoided, stock limits respected, high-priority zones served first, ledger intact.

**OR-Tools matters:** with OR-Tools installed (it is in both `requirements.txt` files) all checks pass in every scenario. Without it, a simpler built-in rule runs; it is safe but not optimal, and in scenario D it may pick the same hubs as before, so "allocation changed" shows as not met.

---

## What we tested

| Check | Result |
|---|---|
| OR-Tools models (supply and patients) | ✅ Solved and checked with a stand-in solver; all scenarios pass |
| Fallback rule without OR-Tools | ✅ Safe in every scenario (see note on D above) |
| Option B: every endpoint, bad-data rejection, WebSocket push | ✅ on **PostgreSQL 16** and SQLite, identical results |
| Option A: all eight pages and every button | ✅ on a stand-in Streamlit |
| React code: syntax, types, every API call matches a backend route | ✅ |

**Note:** Streamlit, FastAPI, SQLAlchemy, OR-Tools and the React libraries could not be installed where this was built, so the OR-Tools model was solved by a stand-in that uses the same equations, and the screens have not been seen running yet. Run each option once on your laptop early and report anything odd.

---

## Demo script (about 3 minutes)

1. **Problem:** "When a cyclone or flood approaches, teams learn late, can't see who is most at risk, and lose track of supplies and patients. We built one command center that fixes all of it."
2. **Overview:** "Red circles are zones in danger, purple are hospitals, blue are supply hubs, lines are roads."
3. **Prediction and warning:** click **Cyclone intensifies**. "The scraper picked up the change, severity re-ranked, and an SMS warning is queued for the area."
4. **Allocation:** "Google OR-Tools decides which hub sends what, and which hospital takes which patients. Every decision shows its reason: beds free, travel time, route."
5. **Re-optimisation:** click **C · Major road blocked**. "A road is cut. The plan re-routes itself and tells us exactly what changed."
6. **Scenario Tests:** "Automatic checks prove the new plan respects capacity, avoids blocked roads and keeps priorities."
7. **Tracking:** approve a move, advance it, then **Tamper with a ledger record**. "Any secret edit breaks the chain and we see it."
8. **Close:** "Predict, prioritise, re-plan, and prove where every supply went."

---

## Questions to ask your team

- Option A or B? (Option B matches the roadmap.) Who decides, and by when?
- Can we freeze the contract (tables or API fields) in the first hour?
- Which website or API will the scraper use? Does it need a key? What if it is down during the demo?
- How is "probability" calculated? One sentence I can repeat to judges.
- Where do vulnerability values, hospital capacity and road times come from?
- Is the SMS real (Twilio) or simulated? Trial accounts usually only send to verified numbers, and commercial SMS in India needs registration.
- Will the scraper and SMS sender call the API (instant updates) or write to the database directly?
- Who presents, who clicks, and what does the submission need (video, slides, repo, live link)?

## Questions judges may ask

| Question | How to answer |
|---|---|
| How do you decide allocations? | Google OR-Tools solves a linear programme: meet every zone's need and every patient's bed, never exceed stock or beds, minimise total travel time, and leave shortages only where the zone is less severe or cut off |
| What happens when conditions change? | Every event (road, capacity, patients, stock) triggers a re-optimisation; the plan version and what changed are logged and shown live |
| How do you know the plan is safe? | Automatic checks after each scenario: capacity, blocked roads, stock, priority, ledger |
| Where does your data come from? | The scraper pulls official feeds (name your team's source). The demo uses simulated data so the story is repeatable |
| How accurate is the prediction? | Prediction and decision are separate. Today a rule; a trained model can feed the same risk readings later |
| Why not blockchain? | A hash chain gives the same tamper proof without the setup |
| How does it scale? | Option B already separates screen, API, optimiser and database on PostgreSQL. Next: cloud hosting, a message queue such as Kafka, real routing |
| Privacy? | We store location, need and headcount, not names. Production adds login and roles |

**Golden rule:** if you don't know, say "In the prototype we did X, and the next step would be Y." Never invent facts.

---

## Words you will hear

| Word | Meaning |
|---|---|
| Frontend / Backend | What the user sees / the code behind the scenes |
| API, REST, endpoint | Programs asking each other for data using web addresses; each address is an endpoint |
| React, component, hook | JavaScript library for screens; a reusable piece of screen; a function (`useState`) that gives it memory |
| Leaflet | The map library; OpenStreetMap provides the map pictures |
| Plotly | The chart library |
| FastAPI, uvicorn | Python library for APIs; the program that runs it |
| SQLAlchemy, Pydantic | Talks to any database; checks incoming data |
| OR-Tools, linear programming | Google's optimisation library; a way to find the best plan that obeys every limit |
| Re-optimisation | Recalculating the plan after something changes |
| WebSocket | A live connection so the server can push updates |
| Hash / SHA-256 | A fingerprint of data; change one letter and it changes completely |
