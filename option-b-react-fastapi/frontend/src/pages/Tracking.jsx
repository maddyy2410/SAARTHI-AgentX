// =====================================================================
// Tracking.jsx : PAGE 5, follow every shipment + tamper check
// =====================================================================
import { useState } from "react"; // remembers the chosen shipment
import { useApi, send } from "../api.js"; // data hook + send helper
import { Table, ErrorBox } from "../components/common.jsx"; // helpers

const FLOW = ["ALLOCATED", "DISPATCHED", "IN_TRANSIT", "RECEIVED", "DISTRIBUTED"]; // steps, in order (same as engine.py)
const nice = (s) => s.replace("_", " ").toLowerCase().replace(/\b\w/g, (c) => c.toUpperCase()); // "IN_TRANSIT" -> "In Transit"

export default function Tracking({ version, refresh }) {
  const verify = useApi("/api/ledger/verify", version); // {ok, first_bad_entry, total}
  const ships = useApi("/api/shipments", version); // latest step per shipment
  const ledger = useApi("/api/ledger", version); // every ledger row
  const [chosen, setChosen] = useState(""); // chosen shipment id
  const [msg, setMsg] = useState(""); // feedback

  const list = ships.data || []; // empty while loading
  const current = list.find((s) => s.shipment_id === chosen) || list[0]; // chosen shipment, or the first one
  const pos = current ? FLOW.indexOf(current.status) : -1; // its step number

  const advance = async () => { // move to the next step
    try { await send("POST", `/api/shipments/${current.shipment_id}/advance`); setMsg(""); refresh(); } // ask the backend
    catch (e) { setMsg("❌ " + e.message); } // show error
  };

  const shortLedger = (ledger.data || []).map((r) => ({ ...r, prev_hash: r.prev_hash.slice(0, 12) + "…", hash: r.hash.slice(0, 12) + "…" })); // shorten fingerprints

  return (
    <div>
      <h1>🚚 Supply Tracking (tamper-evident)</h1> {/* title */}
      <ErrorBox error={verify.error} /> {/* backend down? */}
      {verify.data && (verify.data.ok // green or red banner
        ? <div className="banner ok">Ledger verified: all {verify.data.total} records intact. No record has been altered.</div>
        : <div className="banner error">Tampering detected at ledger record #{verify.data.first_bad_entry}. The hash chain no longer matches.</div>)}

      <Table rows={list} columns={["shipment_id", "item", "qty", "source_hub", "destination", "status", "timestamp"]} /> {/* shipments */}

      {current && ( // only if there is at least one shipment
        <div className="card">
          <label>Open a shipment{" "} {/* dropdown */}
            <select value={current.shipment_id} onChange={(e) => setChosen(e.target.value)}>
              {list.map((s) => <option key={s.shipment_id}>{s.shipment_id}</option>)}
            </select>
          </label>
          <div className="stepper"> {/* progress steps */}
            {FLOW.map((step, i) => <span key={step} className={i <= pos ? "step done" : "step"}>{i <= pos ? "✅" : "⬜"} {nice(step)}</span>)}
          </div>
          {pos < FLOW.length - 1 && <button className="primary" onClick={advance}>Mark as {nice(FLOW[pos + 1])}</button>} {/* next-step button */}
          {msg && <p>{msg}</p>} {/* feedback */}
        </div>
      )}

      <details className="card"> {/* click to open */}
        <summary>Full ledger (every status change, chained by SHA-256 hash)</summary>
        <Table rows={shortLedger} /> {/* all rows */}
      </details>
    </div>
  );
}
