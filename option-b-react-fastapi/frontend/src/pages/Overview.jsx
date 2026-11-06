// =====================================================================
// Overview.jsx : PAGE 1, the whole situation on one screen
// =====================================================================
import { useApi } from "../api.js"; // data hook (re-fetches when "version" changes)
import MapView from "../components/MapView.jsx"; // live map with layers
import Chart from "../components/Chart.jsx"; // Plotly chart
import { Metric, ErrorBox, Chip, eventKind } from "../components/common.jsx"; // helpers

const LINE_COLORS = ["#b91c1c", "#d9580d", "#1f5fd1", "#6b4fbb", "#0e7490", "#2f7d4f", "#6b7280"]; // one per zone

export default function Overview({ version }) {
  const o = useApi("/api/overview", version); // headline numbers
  const sev = useApi("/api/severity", version); // zones
  const roads = useApi("/api/roads", version); // roads
  const hospitals = useApi("/api/hospitals", version); // hospitals
  const hubs = useApi("/api/hubs", version); // hubs
  const inc = useApi("/api/incidents", version); // SOS requests
  const ships = useApi("/api/shipments", version); // shipments
  const events = useApi("/api/events?limit=14", version); // live feed
  const logs = useApi("/api/risk-logs", version); // readings
  const n = o.data || {}; // numbers (empty while loading)

  const byZone = {}; // zone -> {x: times, y: probabilities}, oldest first
  for (const r of [...(logs.data || [])].reverse()) { // API sends newest first
    byZone[r.zone] = byZone[r.zone] || { x: [], y: [] }; // create the series
    byZone[r.zone].x.push(r.timestamp); // time
    byZone[r.zone].y.push(r.probability); // chance
  }
  const traces = Object.entries(byZone).map(([zone, s], i) => ({ // one Plotly line per zone
    x: s.x, y: s.y, name: zone, type: "scatter", mode: "lines+markers",
    line: { color: LINE_COLORS[i % LINE_COLORS.length], width: 2 },
  }));

  return (
    <div>
      <h1>Situation overview</h1>
      <ErrorBox error={o.error} />
      <div className="metrics five">
        <Metric label="Critical zones" value={n.critical_zones} tone="bad" />
        <Metric label="Open SOS requests" value={n.open_requests} />
        <Metric label="Patients without a bed" value={n.patients_waiting} tone="bad" />
        <Metric label="Roads blocked" value={n.roads_blocked} tone="bad" />
        <Metric label="Shipments on the move" value={n.moving_shipments} />
      </div>
      <div className="two-col">
        <section className="card">
          <h3>Live map</h3>
          <MapView zones={sev.data || []} roads={roads.data || []} hospitals={hospitals.data || []} hubs={hubs.data || []}
            incidents={inc.data || []} shipments={ships.data || []} />
        </section>
        <section className="card">
          <h3>System events</h3>
          {(events.data || []).map((e) => (
            <div key={e.id} className="feed-line">
              <code>{e.timestamp.slice(11, 16)}</code>
              <Chip text={e.type.replaceAll("_", " ").toLowerCase()} kind={eventKind(e.type)} />
              <span>{e.message}</span>
            </div>
          ))}
        </section>
      </div>
      <section className="card">
        <h3>Risk probability over the last 24 hours</h3>
        <Chart data={traces} height={320} layout={{ yaxis: { tickformat: ".0%", range: [0, 1] } }} />
      </section>
    </div>
  );
}
