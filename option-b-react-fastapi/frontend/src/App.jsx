// =====================================================================
// App.jsx : the frame of the app (sidebar + chosen page) and the LIVE
// connection. When the backend says "something changed", every page
// re-reads its data by itself: the dashboard never needs a manual refresh.
// =====================================================================
import { useCallback, useEffect, useState } from "react"; // React hooks
import { API_URL, WS_URL, send } from "./api.js"; // backend address + "send a change" helper
import Overview from "./pages/Overview.jsx"; // 1
import Alerts from "./pages/Alerts.jsx"; // 2
import Severity from "./pages/Severity.jsx"; // 3
import Allocation from "./pages/Allocation.jsx"; // 4
import Network from "./pages/Network.jsx"; // 5
import Tracking from "./pages/Tracking.jsx"; // 6
import Requests from "./pages/Requests.jsx"; // 7
import Scenarios from "./pages/Scenarios.jsx"; // 8

const PAGES = [ // menu: label + component
  { label: "Overview", component: Overview },
  { label: "Early Warnings", component: Alerts },
  { label: "Severity & Vulnerability", component: Severity },
  { label: "Allocation (optimiser)", component: Allocation },
  { label: "Roads & Hospitals", component: Network },
  { label: "Supply Tracking", component: Tracking },
  { label: "Field Requests", component: Requests },
  { label: "Scenario Tests", component: Scenarios },
];

const DEMOS = [ // sidebar demo buttons: label + backend address to call
  { label: "Cyclone intensifies", path: "/api/demo/cyclone" },
  { label: "B · Hospital fills up", path: "/api/scenarios/B" },
  { label: "C · Major road blocked", path: "/api/scenarios/C" },
  { label: "D · New supplies arrive", path: "/api/scenarios/D" },
  { label: "E · Several changes at once", path: "/api/scenarios/E" },
  { label: "Tamper with a ledger record", path: "/api/demo/tamper" },
  { label: "A · Reset to normal", path: "/api/scenarios/A" },
];

const POLL_MS = 15000; // safety net: re-read data every 15 s even without a live message

export default function App() {
  const [page, setPage] = useState(0); // open page (index into PAGES)
  const [version, setVersion] = useState(0); // goes up whenever data should be re-read
  const [live, setLive] = useState(false); // is the WebSocket connected?
  const [toast, setToast] = useState(""); // pop-up message
  const [busy, setBusy] = useState(false); // true while a demo button is running

  const refresh = useCallback(() => setVersion((v) => v + 1), []); // pages call this after changing something

  useEffect(() => { // open the live connection once
    let ws, retry, stopped = false; // connection, reconnect timer, closing flag
    const connect = () => { // (re)connect
      ws = new WebSocket(WS_URL); // backend /ws
      ws.onopen = () => setLive(true); // green dot
      ws.onmessage = (m) => { const d = JSON.parse(m.data); setToast(d.event); refresh(); }; // change -> reload data
      ws.onclose = () => { setLive(false); if (!stopped) retry = setTimeout(connect, 3000); }; // retry in 3 s
    };
    connect(); // first connection
    const poll = setInterval(refresh, POLL_MS); // backup timer
    return () => { stopped = true; clearTimeout(retry); clearInterval(poll); ws && ws.close(); }; // clean up
  }, [refresh]);

  useEffect(() => { // hide the toast after 5 s
    if (!toast) return;
    const t = setTimeout(() => setToast(""), 5000);
    return () => clearTimeout(t);
  }, [toast]);

  const runDemo = async (d) => { // a demo button was clicked
    setBusy(true); // disable buttons while it runs
    try {
      const res = await send("POST", d.path); // ask the backend
      if (res.checks) { // a scenario: summarise its automatic checks
        const passed = res.checks.filter((c) => c.ok).length;
        setToast(`${res.title}: ${res.changed} allocations changed, ${passed}/${res.checks.length} checks passed`);
      }
      refresh(); // reload data
    } catch (e) { setToast("Error: " + e.message); } // show problems
    setBusy(false); // buttons back on
  };

  const Page = PAGES[page].component; // component of the open page

  return (
    <div className="layout">
      <aside className="sidebar">
        <h2>Command Center</h2>
        <div className="live"><span className={live ? "dot on" : "dot"} />{live ? "Live, auto-updating" : "Connecting…"}</div>
        <nav aria-label="Pages">
          {PAGES.map((p, i) => (
            <button key={p.label} className={i === page ? "nav active" : "nav"} aria-current={i === page ? "page" : undefined}
              onClick={() => setPage(i)}>{p.label}</button>
          ))}
        </nav>
        <h3>Demo controls</h3>
        {DEMOS.map((d) => <button key={d.path} className="demo" disabled={busy} onClick={() => runDemo(d)}>{d.label}</button>)}
        <p className="muted small">Backend: {API_URL}</p>
      </aside>
      <main className="main">
        <Page version={version} refresh={refresh} goTo={setPage} />
      </main>
      {toast && <div className="toast" role="status">{toast}</div>}
    </div>
  );
}
