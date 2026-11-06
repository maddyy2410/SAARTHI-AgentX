// =====================================================================
// Network.jsx : PAGE 5, roads, hospitals and stock. Every change here is
// an EVENT (ROAD_BLOCKED, HOSPITAL_CAPACITY_CHANGED, RESOURCE_RECEIVED)
// that makes the backend re-optimise straight away.
// =====================================================================
import { useState } from "react"; // form values
import { useApi, send } from "../api.js"; // data hook + send helper
import Chart from "../components/Chart.jsx"; // Plotly chart
import { ErrorBox, fmtMinutes, ROAD_COLORS } from "../components/common.jsx"; // helpers

export default function Network({ version, refresh }) {
  const roads = useApi("/api/roads", version); // roads
  const hospitals = useApi("/api/hospitals", version); // hospitals
  const inventory = useApi("/api/inventory", version); // stock
  const hubs = useApi("/api/hubs", version); // hubs (for the receive form)
  const [beds, setBeds] = useState({}); // edited bed numbers: hospital id -> {available_beds, icu_available}
  const [stock, setStock] = useState({ hub_id: 1, item: "Food kits", qty: 500 }); // receive-stock form
  const [msg, setMsg] = useState(""); // feedback

  const act = async (method, path, body, ok) => { // send a change, show the result
    try { await send(method, path, body); setMsg(ok); refresh(); }
    catch (e) { setMsg("❌ " + e.message); }
  };

  const hs = hospitals.data || []; // hospitals list
  const inv = inventory.data || []; // stock rows
  const hubNames = [...new Set(inv.map((r) => r.hub))]; // table rows
  const items = [...new Set(inv.map((r) => r.item))]; // table columns
  const qty = (hub, item) => inv.find((r) => r.hub === hub && r.item === item)?.qty ?? 0; // one number
  const edit = (h, field) => beds[h.id]?.[field] ?? h[field]; // edited value, or the saved one

  return (
    <div>
      <h1>Roads and hospitals</h1>
      <ErrorBox error={roads.error} />
      {msg && <p>{msg}</p>}

      <h3>Road network</h3>
      <p className="muted">Change a road's condition. The optimiser re-plans routes immediately.</p>
      <div className="table-wrap">
        <table>
          <thead><tr><th>Road</th><th>From</th><th>To</th><th>Normal time</th><th>Now</th><th>Condition</th></tr></thead>
          <tbody>
            {(roads.data || []).map((r) => (
              <tr key={r.id}>
                <td className="mono">{r.id}</td><td>{r.a}</td><td>{r.b}</td>
                <td>{fmtMinutes(r.minutes)}</td>
                <td style={{ color: ROAD_COLORS[r.status] }}>{r.status === "BLOCKED" ? "closed" : fmtMinutes(r.current_minutes)}</td>
                <td>
                  <select id={`road-${r.id}`} aria-label={`Condition of road ${r.id}`} value={r.status}
                    onChange={(e) => act("PATCH", `/api/roads/${r.id}`, { status: e.target.value }, `✅ Road ${r.id} is now ${e.target.value}`)}>
                    <option value="OPEN">Open</option><option value="SLOW">Slow</option><option value="BLOCKED">Blocked</option>
                  </select>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <h3>Hospital capacity</h3>
      <section className="card">
        <Chart height={260} data={[ // stacked bars: occupied vs free beds
          { type: "bar", name: "Occupied beds", x: hs.map((h) => h.name.replace(" Hospital", "")), y: hs.map((h) => h.total_beds - h.available_beds), marker: { color: "#9ca3af" } },
          { type: "bar", name: "Free beds", x: hs.map((h) => h.name.replace(" Hospital", "")), y: hs.map((h) => h.available_beds), marker: { color: "#6b4fbb" } },
        ]} layout={{ barmode: "stack", yaxis: { title: "Beds" } }} />
      </section>
      <div className="table-wrap">
        <table>
          <thead><tr><th>Hospital</th><th>Zone</th><th>Free beds</th><th>Free ICU</th><th>Oxygen</th><th></th></tr></thead>
          <tbody>
            {hs.map((h) => (
              <tr key={h.id}>
                <td>{h.name}</td><td>{h.zone}</td>
                <td><input id={`beds-${h.id}`} type="number" min="0" max={h.total_beds} value={edit(h, "available_beds")} aria-label={`Free beds at ${h.name}`}
                  onChange={(e) => setBeds({ ...beds, [h.id]: { ...beds[h.id], available_beds: e.target.value } })} /> / {h.total_beds}</td>
                <td><input id={`icu-${h.id}`} type="number" min="0" max={h.icu_total} value={edit(h, "icu_available")} aria-label={`Free ICU beds at ${h.name}`}
                  onChange={(e) => setBeds({ ...beds, [h.id]: { ...beds[h.id], icu_available: e.target.value } })} /> / {h.icu_total}</td>
                <td>{h.oxygen}</td>
                <td><button onClick={() => act("PATCH", `/api/hospitals/${h.id}`,
                  { available_beds: Number(edit(h, "available_beds")), icu_available: Number(edit(h, "icu_available")) },
                  `✅ ${h.name} capacity updated`)}>Save</button></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <h3>Stock by hub</h3>
      <div className="table-wrap">
        <table>
          <thead><tr><th>Hub</th>{items.map((i) => <th key={i}>{i}</th>)}</tr></thead>
          <tbody>{hubNames.map((h) => <tr key={h}><td>{h}</td>{items.map((i) => <td key={i} className="mono">{qty(h, i)}</td>)}</tr>)}</tbody>
        </table>
      </div>
      <form className="card form" onSubmit={(e) => { e.preventDefault(); act("POST", "/api/inventory/receive", { ...stock, hub_id: Number(stock.hub_id), qty: Number(stock.qty) }, "✅ Stock received"); }}>
        <strong>Record new supplies arriving</strong>
        <label>Hub
          <select id="recv-hub" value={stock.hub_id} onChange={(e) => setStock({ ...stock, hub_id: e.target.value })}>
            {(hubs.data || []).map((h) => <option key={h.id} value={h.id}>{h.name}</option>)}
          </select>
        </label>
        <label>Item
          <select id="recv-item" value={stock.item} onChange={(e) => setStock({ ...stock, item: e.target.value })}>
            {["Food kits", "Medical kits", "Water packs", "Tarpaulins", "Oxygen cylinders"].map((i) => <option key={i}>{i}</option>)}
          </select>
        </label>
        <label>Quantity
          <input id="recv-qty" type="number" min="1" value={stock.qty} onChange={(e) => setStock({ ...stock, qty: e.target.value })} />
        </label>
        <button className="primary" type="submit">Add stock</button>
      </form>
    </div>
  );
}
