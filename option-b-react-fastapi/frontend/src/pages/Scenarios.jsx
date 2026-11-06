// =====================================================================
// Scenarios.jsx : PAGE 8, the automated test scenarios from our notes.
// Each button changes the world, the optimiser re-plans, and the backend
// checks the new plan (capacity, blocked roads, priority, ledger...).
// =====================================================================
import { useState } from "react"; // remembers the last result
import { useApi, send } from "../api.js"; // data hook + send helper
import { ErrorBox } from "../components/common.jsx"; // helpers

export default function Scenarios({ version, refresh, goTo }) {
  const list = useApi("/api/scenarios", version); // A to E
  const [result, setResult] = useState(null); // last scenario result
  const [busy, setBusy] = useState(""); // scenario currently running
  const [msg, setMsg] = useState(""); // errors

  const run = async (id) => { // run one scenario
    setBusy(id); setMsg(""); // show it is running
    try { setResult(await send("POST", `/api/scenarios/${id}`)); refresh(); } // run and keep the result
    catch (e) { setMsg("❌ " + e.message); } // show error
    setBusy(""); // done
  };

  return (
    <div>
      <h1>Scenario tests</h1>
      <p className="muted">Run a scenario and the system re-plans on its own. The checks below confirm the new plan is safe. Scenarios add up: run A to start fresh.</p>
      <ErrorBox error={list.error} />
      <div className="scenario-grid">
        {(list.data || []).map((s) => ( // one card per scenario
          <article key={s.id} className="card scenario">
            <div className="eyebrow">Scenario {s.id}</div>
            <strong>{s.title}</strong>
            <span className="muted small">{s.description}</span>
            <button className="primary" disabled={!!busy} onClick={() => run(s.id)}>{busy === s.id ? "Running…" : "Run"}</button>
          </article>
        ))}
      </div>
      {msg && <p>{msg}</p>}
      {result && (
        <section className="card">
          <h3>Scenario {result.scenario}: {result.title}</h3>
          <p>Plan v{result.version}: <b>{result.changed} allocations changed.</b> <button className="linkish" onClick={() => goTo(3)}>See the new plan</button></p>
          <ul className="checks">
            {result.checks.map((c) => ( // one line per check
              <li key={c.check} className={c.ok ? "pass" : "fail"}>
                <span aria-hidden="true">{c.ok ? "✓" : "✗"}</span>
                <b>{c.check}</b> <span className="muted small">{c.detail}</span>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}
