// =====================================================================
// Severity.jsx : PAGE 3, which area is in the worst situation (vulnerability index)
// =====================================================================
import { useApi } from "../api.js"; // data hook
import Chart from "../components/Chart.jsx"; // Plotly chart
import { Table, ErrorBox, LEVEL_COLORS } from "../components/common.jsx"; // helpers

export default function Severity({ version }) {
  const sev = useApi("/api/severity", version); // zones, most severe first
  const rows = sev.data || []; // empty while loading
  const ordered = [...rows].reverse(); // Plotly draws bottom-up, so put the worst zone at the top

  return (
    <div>
      <h1>Severity and vulnerability</h1>
      <ErrorBox error={sev.error} />
      <section className="card">
        <Chart height={340} data={[{ // one horizontal bar per zone, coloured by level
          type: "bar", orientation: "h", x: ordered.map((z) => z.score), y: ordered.map((z) => z.name),
          marker: { color: ordered.map((z) => LEVEL_COLORS[z.level]) },
          text: ordered.map((z) => `${z.level} · ${z.score}`), textposition: "outside",
          hovertemplate: "%{y}: %{x}<extra></extra>",
        }]} layout={{ xaxis: { range: [0, 100], title: "Severity score (0 to 100)" }, margin: { l: 100, r: 60, t: 10, b: 44 } }} />
      </section>
      <Table rows={rows} columns={["name", "level", "score", "disaster_type", "probability", "vulnerability", "population", "open_requests"]} />
      <details className="card">
        <summary>How is the score calculated?</summary>
        <p><b>Severity = risk probability × vulnerability index × 100</b>, plus up to 10 points for open SOS requests.</p>
        <p>A likely disaster in a fragile area ranks above a likely disaster in a strong one. Critical 50+, High 30+, Moderate 15+, otherwise Low.</p>
        <p>The formula lives in the backend's <code>engine.py</code>, so it can change without touching this screen.</p>
      </details>
    </div>
  );
}
