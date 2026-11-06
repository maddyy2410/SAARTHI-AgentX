# Option A: Streamlit + SQLite (Python only)

The same command center as Option B, with the same engine (OR-Tools), the same eight pages and the same scenarios, but everything is Python and there is no separate web server. The main README has the scenarios, demo script and judge questions.

## 1. Setup (one time)

1. **Install Python 3.11 or newer** from python.org (Windows: tick **"Add Python to PATH"**).
2. **Install VS Code** with the Python extension.
3. Open this folder in VS Code, then a terminal (Terminal → New Terminal).
4. Create a private Python environment:

   | Windows | Mac |
   |---|---|
   | `python -m venv .venv` | `python3 -m venv .venv` |
   | `.venv\Scripts\activate` | `source .venv/bin/activate` |

5. `pip install -r requirements.txt` (includes OR-Tools)
6. `streamlit run app.py` → opens `http://localhost:8501`. Stop with **Ctrl + C**.

## 2. Files

| File | What it does | Will you edit it? |
|---|---|---|
| `app.py` | The eight screens. Commented line by line | **Yes, most of your time** |
| `data.py` | Saves changes to SQLite; every change is an event that re-runs the optimiser and stores a plan version | Rarely |
| `engine.py` | All decisions: road travel times, severity, OR-Tools supply and patient plans, checks, hash chain (same file as Option B) | Backend team |
| `demo_data.py` | The demo world: zones, hubs, hospitals, roads, stock (same file as Option B) | Yes, to change the region |

## 3. Pages

| Page | Shows | You can |
|---|---|---|
| Overview | 5 numbers, live map (zones, roads, hospitals, hubs, SOS, shipments), system events, Plotly risk chart | Switch map layers |
| Early Warnings | Alerts with countdown, scraper readings | Queue an alert for SMS |
| Severity & Vulnerability | Plotly ranking by severity | Read the formula |
| Allocation (optimiser) | OR-Tools supply moves and hospital assignments, each with its reason; what changed; history | Approve one or all, re-optimise now |
| Roads & Hospitals | Road conditions, hospital beds (Plotly), stock | Block or slow a road, change beds, add stock |
| Supply Tracking | Tamper check, shipment steps, ledger | Move a shipment to its next step |
| Field Requests | SOS map and table | Claim, resolve, log a new SOS |
| Scenario Tests | Scenarios A to E | Run one and see the automatic checks |

## 4. Streamlit in 60 seconds

Streamlit runs `app.py` top to bottom every time someone clicks. Each `st.` line draws something:

```python
st.title("Hello")                   # heading
st.metric("Open SOS", 12)           # number tile
if st.button("Approve"):            # True only right after a click
    act(data.approve, key)          # do the action, then redraw
```

Edits you can make yourself: colours in section 1 of `app.py`; places in `demo_data.py` (then click **A · Reset to normal**); a new page = a new `def page_...():` added to `PAGES` at the bottom.

## 5. The contract with the backend

The tables in `data.py` (`SCHEMA`) are the contract: `zones`, `risk_logs`, `alerts`, `incidents`, `hubs`, `inventory`, `hospitals`, `roads`, `supply_movement`, `events`, `plans`, `plan_runs`. They are the same tables as Option B. Probabilities are 0 to 1; road status is `OPEN`, `SLOW` or `BLOCKED`; times are `YYYY-MM-DD HH:MM:SS`.

## 6. Dashboard independence (requirement from our notes)

- **Algorithm changes:** all logic is in `engine.py`; `app.py` only shows results and reasons.
- **Data changes:** every event re-runs the optimiser; live mode also re-reads every 10 seconds, so changes by the scraper appear by themselves.
- **Must stay fixed:** table and column names, and the shape of what `engine.py` returns.
- **Limit:** Option A reads SQLite directly; moving to PostgreSQL needs code changes (Option B does not).

Demo: open Allocation, click **C · Major road blocked**, and watch the moves marked "Changed".

## 7. Troubleshooting

| Problem | Fix |
|---|---|
| Allocation says "Built-in rule" | `pip install ortools` in the venv, then restart |
| `streamlit` not recognised / `ModuleNotFoundError` | Activate the venv, then `pip install -r requirements.txt` |
| `module 'streamlit' has no attribute 'fragment'` | `pip install -U streamlit` |
| Map background blank | Needs internet; lines and dots still show |
| Data looks odd | Click **A · Reset to normal** |
| `database is locked` | Two writes at once; click again |
