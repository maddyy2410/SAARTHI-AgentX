// =====================================================================
// Allocation.jsx : PAGE 4, the optimiser's decisions WITH their reasons
//   - supply plan: which hub sends what to which zone (OR-Tools)
//   - patient plan: which hospital takes which patients (OR-Tools)
//   - what changed since the last plan, and why it re-ran
// =====================================================================
import { useState } from "react"; // feedback message
import { useApi, send } from "../api.js"; // data hook + send helper
import { ErrorBox, Loading, Chip, fmtMinutes, Table } from "../components/common.jsx"; // helpers

export default function Allocation({ version, refresh }) {
  const plan = useApi("/api/plan", version); // current plan
  const history = useApi("/api/plan/history", version); // recent re-optimisations
  const [msg, setMsg] = useState(""); // feedback text
  const p = plan.data; // shorthand

  const act = async (method, path, body, ok) => { // run an action and show the result
    try { await send(method, path, body); setMsg(ok); refresh(); } // success
    catch (e) { setMsg("❌ " + e.message); } // the backend's reason
  };

  return (
    <div>
      <h1>Allocation</h1>
      <ErrorBox error={plan.error} />
      {!p && <Loading what="the plan" />}
      {p && (
        <section className="card plan-head">
          <div>
            <div className="eyebrow">Plan v{p.version} · {p.created_at?.slice(11, 16)}</div>
            <div className="plan-title">{p.changed} allocations changed after <b>{p.trigger_type?.replaceAll("_", " ").toLowerCase()}</b></div>
            <div className="muted small">{p.trigger} · Solver: {p.solver}</div>
          </div>
          <div className="btn-row">
            <button onClick={() => act("POST", "/api/plan/run", null, "✅ Plan recalculated")}>Re-optimise now</button>
            <button className="primary" disabled={!p.supply.length}
              onClick={() => act("POST", "/api/shipments/approve-all", null, "✅ Every supply move approved and tracked")}>Approve all moves</button>
          </div>
        </section>
      )}
      {msg && <p>{msg}</p>}

      {p && (
        <>
          <h3>Supply plan</h3>
          {p.supply.length === 0 && <div className="banner ok">No moves needed right now. Every high-risk zone is covered.</div>}
          {p.supply.map((r) => ( // one card per planned move
            <article key={r.key} className="card rec">
              <div className="rec-main">
                <div className="rec-top">
                  <Chip text={r.level} kind={r.level === "Critical" ? "bad" : "warn"} />
                  {r.changed && <Chip text="Changed" kind="info" />}
                  <b>Move {r.qty} {r.item}</b> <span>{r.from_hub} → <b>{r.zone}</b></span>
                </div>
                <div className="muted small">{r.reason}</div>
              </div>
              <div className="rec-side">
                <span className="mono">{fmtMinutes(r.minutes)}</span>
                <button className="primary" onClick={() => act("POST", "/api/shipments", { plan_key: r.key }, `✅ ${r.qty} ${r.item} approved`)}>Approve</button>
              </div>
            </article>
          ))}
          {p.shortfalls.length > 0 && (
            <div className="banner error">
              <b>Could not cover:</b> {p.shortfalls.map((s) => `${s.missing} ${s.item} for ${s.zone} (${s.why})`).join(" · ")}
            </div>
          )}

          <h3>Patient plan (hospitals)</h3>
          {p.patients.length === 0 && <p className="muted">No medical requests waiting for a bed.</p>}
          {p.patients.map((a) => ( // one card per hospital assignment
            <article key={a.key} className="card rec">
              <div className="rec-main">
                <div className="rec-top">
                  {a.changed && <Chip text="Changed" kind="info" />}
                  <b>{a.patients} patients</b> <span>from SOS #{a.incident_id} ({a.zone}) → <b>{a.hospital}</b></span>
                </div>
                <div className="muted small">{a.reason}</div>
              </div>
              <div className="rec-side"><span className="mono">{fmtMinutes(a.minutes)}</span></div>
            </article>
          ))}
          {p.unplaced.length > 0 && (
            <div className="banner error"><b>No bed found:</b> {p.unplaced.map((u) => `${u.patients} patients from SOS #${u.incident_id} (${u.zone})`).join(" · ")}</div>
          )}
        </>
      )}

      <h3>Recent re-optimisations</h3>
      <Table rows={history.data} columns={["version", "created_at", "trigger_type", "trigger_text", "changed"]} />
    </div>
  );
}
