# Option B: React + FastAPI + PostgreSQL + OR-Tools (the stack from our notes)

Two programs run at the same time:

```
 Browser                          Backend (Python)                        Storage
┌──────────────────┐  REST/JSON  ┌──────────────────────────────┐       ┌──────────────┐
│ React dashboard  │ ──────────▶ │ FastAPI (main.py)            │ ────▶ │ PostgreSQL   │
│ Leaflet map      │             │   event → update → optimise  │       │  or SQLite   │
│ Plotly charts    │ ◀────────── │   engine.py (OR-Tools)       │       └──────────────┘
│                  │  WebSocket  │   save plan → push "refresh" │
└──────────────────┘             └──────────────────────────────┘
```

The React app **never touches the database and never makes decisions**. It shows what the API returns, including the reason for every allocation. The main README has the scenarios, demo script and judge questions.

## 1. Install the tools (one time)

1. **Python 3.11+** from python.org (Windows: tick "Add Python to PATH").
2. **Node.js LTS** from nodejs.org (includes `npm`).
3. **VS Code** with the Python extension.
4. **PostgreSQL is optional.** Without it the backend uses SQLite automatically (section 5).

## 2. Start the backend (terminal 1)

In the `backend` folder:

| Windows | Mac |
|---|---|
| `python -m venv .venv` | `python3 -m venv .venv` |
| `.venv\Scripts\activate` | `source .venv/bin/activate` |

```
pip install -r requirements.txt
uvicorn main:app --reload
```

Check: open **http://localhost:8000/docs** to see and try every endpoint. Leave it running.

## 3. Start the frontend (terminal 2)

In the `frontend` folder:

```
npm install
npm run dev
```

Open **http://localhost:5173**. The green "Live" dot means the WebSocket is connected.

## 4. Files

```
backend/
  main.py        API endpoints, events, scenarios, WebSocket
  db.py          database tables, demo data, re-optimisation and plan versions
  engine.py      decision logic: roads, severity, OR-Tools plans, checks, hash chain (same file as Option A)
  demo_data.py   the demo world (same file as Option A)
frontend/src/
  api.js                     the only file that knows the backend address
  App.jsx                    sidebar, live connection, demo buttons
  components/MapView.jsx     Leaflet map with layer switches (zones, roads, hospitals, hubs, SOS, shipments)
  components/Chart.jsx       one Plotly wrapper used by every chart
  components/common.jsx      table, tiles, chips, colours
  pages/Overview.jsx         numbers, live map, system events, risk chart
  pages/Alerts.jsx           24-hour warnings and SMS queue
  pages/Severity.jsx         vulnerability-weighted severity ranking
  pages/Allocation.jsx       OR-Tools supply and patient plans, reasons, what changed
  pages/Network.jsx          road conditions, hospital capacity, stock arrivals (each one is an event)
  pages/Tracking.jsx         shipment timeline and tamper-evident ledger
  pages/Requests.jsx         SOS map; claim, resolve, new SOS
  pages/Scenarios.jsx        scenarios A to E with automatic checks
  styles.css                 colours and layout
```

### React in 60 seconds

```jsx
export default function Hello({ version }) {            // a component: a function that returns screen parts
  const [count, setCount] = useState(0);                 // a remembered value; setCount changes it and redraws
  const { data } = useApi("/api/plan", version);         // our hook: fetches data, re-fetches when version changes
  return <button onClick={() => setCount(count + 1)}>Plan v{data?.version}, clicked {count}</button>;
}
```

Live updates: `App.jsx` holds a `version` number. When the WebSocket says "refresh" (or every 15 seconds), it goes up by one, and every page re-reads its data.

## 5. PostgreSQL instead of SQLite

Create a database called `disaster`, then before `uvicorn`:

| Windows (PowerShell) | Mac |
|---|---|
| `$env:DATABASE_URL="postgresql+psycopg://postgres:PASSWORD@localhost:5432/disaster"` | `export DATABASE_URL="postgresql+psycopg://postgres:PASSWORD@localhost:5432/disaster"` |

Tables and demo data are created automatically. Nothing else changes.

## 6. The API (the contract)

| Method | Address | Used by | Does |
|---|---|---|---|
| GET | `/api/overview` | Dashboard | Headline numbers |
| GET | `/api/severity` | Dashboard | Zones ranked by severity |
| GET / **POST** | `/api/risk-logs` | Dashboard / **scraper** | Readings / add `{zone, disaster_type, probability, source}` → `RISK_UPDATED` |
| GET / POST | `/api/alerts` | Dashboard | List / queue an alert |
| **PATCH** | `/api/alerts/{id}` | **SMS sender** | `{sms_status: "SENT" or "FAILED"}` |
| GET / **POST** | `/api/incidents` | Dashboard / **citizen app** | List / new SOS `{lat, lon, zone, need_type, people}` → `NEW_PATIENTS` for Medical |
| PATCH | `/api/incidents/{id}/claim`, `/resolve` | NGOs | Claim or close |
| GET / PATCH | `/api/roads`, `/api/roads/{id}` | Dashboard | Roads with map positions / `{status: OPEN, SLOW or BLOCKED}` → `ROAD_*` |
| GET / PATCH | `/api/hospitals`, `/api/hospitals/{id}` | Dashboard | Capacity / `{available_beds, icu_available}` → `HOSPITAL_CAPACITY_CHANGED` |
| GET / POST | `/api/inventory`, `/api/inventory/receive` | Dashboard | Stock / `{hub_id, item, qty}` → `RESOURCE_RECEIVED` |
| GET | `/api/plan` | Dashboard | Current plan: supply moves and patient assignments with reasons and "changed" flags |
| GET / POST | `/api/plan/history`, `/api/plan/run` | Dashboard | Past re-optimisations / re-optimise now |
| POST | `/api/shipments` `{plan_key}`, `/api/shipments/approve-all` | Dashboard | Approve moves → tracked shipments |
| GET / POST | `/api/shipments`, `/api/shipments/{id}/advance` | Dashboard | Shipments (with map position) / next step |
| GET | `/api/ledger`, `/api/ledger/verify` | Dashboard | Ledger / tamper check |
| GET | `/api/events`, `/api/checks` | Dashboard | System events / checks on the current plan |
| GET / POST | `/api/scenarios`, `/api/scenarios/{A-E}` | Dashboard | List / run a scenario and get its checks |
| POST | `/api/demo/cyclone`, `/api/demo/tamper` | Demo buttons | Prediction demo / tamper demo |
| WS | `/ws` | Dashboard | Sends `{"type": "refresh"}` after every change |

Bad data is refused before it reaches the database: probability outside 0 to 1, an SOS without a location, unknown zones or roads, claiming twice, more free beds than total beds, approving a move that is no longer in the plan.

## 7. Dashboard independence (requirement from our notes)

*"The optimization engine should be independent of the UI, so the algorithm can change without rebuilding the dashboard."*

- **Algorithm changes:** logic lives only in `backend/engine.py`; the React app just shows its results and reasons.
- **Data changes:** every event re-runs OR-Tools, saves a new plan version and pushes a WebSocket refresh; open dashboards update instantly.
- **Database changes:** SQLite or PostgreSQL by one setting.
- **Backend changes:** only `src/api.js` knows the backend address.
- **Must stay fixed:** the API addresses and field names in section 6.

## 8. Troubleshooting

| Problem | Fix |
|---|---|
| Red box "Cannot reach the backend" | Terminal 1 is not running; check http://localhost:8000/docs |
| Allocation page says "Built-in rule" | OR-Tools is not installed: `pip install ortools` in the backend venv, then restart |
| `npm` or `uvicorn` not recognised | Install Node.js / activate the venv and reinstall requirements |
| Charts blank or `createPlotlyComponent is not a function` | Run `npm install` again; `Chart.jsx` already handles both import styles |
| PostgreSQL "password authentication failed" | Wrong password in `DATABASE_URL` |
| Map tiles missing | Needs internet; circles, lines and dots still show |
| Data looks odd | Click **A · Reset to normal** |
