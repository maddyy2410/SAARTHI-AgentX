// =====================================================================
// common.jsx : small reusable pieces used by several pages
// =====================================================================
import { API_URL } from "../api.js"; // backend address (shown in the error box)

export const LEVEL_COLORS = { Critical: "#b91c1c", High: "#d9580d", Moderate: "#c99a06", Low: "#2f7d4f" }; // danger levels
export const REQUEST_COLORS = { OPEN: "#b91c1c", IN_TRANSIT: "#d9580d", RESOLVED: "#2f7d4f" }; // SOS dots
export const ROAD_COLORS = { OPEN: "#6b7280", SLOW: "#d9580d", BLOCKED: "#b91c1c" }; // road lines

export function fmtMinutes(m) { // 135 -> "2h 15m"
  if (m === null || m === undefined) return "no route"; // unreachable
  const h = Math.floor(m / 60); // whole hours
  const mm = Math.round(m % 60); // leftover minutes
  return h ? `${h}h ${String(mm).padStart(2, "0")}m` : `${mm} min`; // e.g. "2h 05m" or "45 min"
}

export function Table({ rows, columns }) { // simple table from a list of objects
  if (!rows || rows.length === 0) return <p className="muted">Nothing to show yet.</p>; // empty message
  const cols = columns || Object.keys(rows[0]); // chosen columns, or every key of the first row
  // (No comments inside <table>: extra spaces there make React print warnings.)
  return (
    <div className="table-wrap">
      <table>
        <thead><tr>{cols.map((c) => <th key={c}>{c.replaceAll("_", " ")}</th>)}</tr></thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={r.id ?? r.entry_id ?? r.key ?? r.shipment_id ?? r.name ?? i}>
              {cols.map((c) => <td key={c}>{String(r[c] ?? "")}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function Metric({ label, value, tone }) { // number tile; tone "bad" makes the number red
  return (
    <div className="metric">
      <div className={tone === "bad" && value ? "metric-value bad" : "metric-value"}>{value ?? "…"}</div> {/* the number */}
      <div className="metric-label">{label}</div> {/* its label */}
    </div>
  );
}

export function Chip({ text, kind }) { // small coloured label (kind picks the colour)
  return <span className={`chip ${kind || ""}`}>{text}</span>; // colours come from styles.css
}

export function eventKind(type) { // colour group for an event type in the feed
  if (!type) return ""; // no type
  if (type.includes("BLOCKED") || type.includes("DEPLETED") || type === "TAMPER") return "bad"; // red
  if (type === "REOPTIMIZED") return "info"; // blue
  if (type.includes("CAPACITY") || type.includes("SLOW") || type.includes("PATIENTS") || type.includes("RISK")) return "warn"; // orange
  if (type.includes("RECEIVED") || type.includes("REOPENED") || type.includes("RESOLVED")) return "good"; // green
  return ""; // grey
}

export function ErrorBox({ error }) { // shown when the backend cannot be reached
  if (!error) return null; // no error, no box
  return <div className="banner error">Cannot reach the backend at {API_URL}. Is it running? ({error})</div>;
}

export function Loading({ what }) { // placeholder while loading
  return <p className="muted">Loading {what}…</p>;
}
