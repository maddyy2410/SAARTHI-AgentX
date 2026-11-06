// =====================================================================
// Alerts.jsx : PAGE 2, early warnings (24-hour alerts)
// =====================================================================
import { useState } from "react"; // remembers form values
import { useApi, send } from "../api.js"; // data hook + send helper
import { Table, ErrorBox } from "../components/common.jsx"; // table and error box

function timeToImpact(a) { // "21h 30m" until impact, or "Impact window reached"
  const created = new Date(a.created_at.replace(" ", "T")); // text time -> real date
  const hoursLeft = (created.getTime() + a.eta_hours * 3600000 - Date.now()) / 3600000; // hours from now to impact
  if (hoursLeft <= 0) return "Impact window reached"; // already passed
  return `${Math.floor(hoursLeft)}h ${Math.floor((hoursLeft % 1) * 60)}m`; // hours and minutes
}

export default function Alerts({ version, refresh }) {
  const alerts = useApi("/api/alerts", version); // all alerts
  const zones = useApi("/api/severity", version); // used for the zone dropdown
  const logs = useApi("/api/risk-logs?limit=20", version); // newest readings
  const [form, setForm] = useState({ zone: "", disaster_type: "Cyclone", severity: "Red", eta_hours: 24, radius_km: 30 }); // form values
  const [msg, setMsg] = useState(""); // success or error message under the form

  const update = (field) => (e) => setForm({ ...form, [field]: e.target.value }); // makes a change-handler for one field

  const submit = async (e) => { // when "Queue alert" is pressed
    e.preventDefault(); // stop the browser reloading the page (default form behaviour)
    const zone = form.zone || (zones.data && zones.data[0]?.name); // chosen zone, or the first one
    try {
      await send("POST", "/api/alerts", { ...form, zone, eta_hours: Number(form.eta_hours), radius_km: Number(form.radius_km) }); // send it
      setMsg("✅ Alert queued for SMS."); // success message
      refresh(); // reload data
    } catch (err) { setMsg("❌ " + err.message); } // show the backend's reason
  };

  const rows = (alerts.data || []).map((a) => ({ ...a, time_to_impact: timeToImpact(a) })); // add the countdown column

  return (
    <div>
      <h1>⚠️ Early Warnings (24-hour alerts)</h1> {/* title */}
      <ErrorBox error={alerts.error} /> {/* backend down? */}
      <Table rows={rows} columns={["id", "created_at", "zone", "disaster_type", "severity", "time_to_impact", "radius_km", "sms_status"]} /> {/* alert table */}

      <h3>Issue a new alert</h3> {/* form title */}
      <p className="muted">Saving marks the alert QUEUED. The backend SMS sender sends it and changes the status to SENT.</p> {/* explanation */}
      <form className="card form" onSubmit={submit}> {/* the form */}
        <label>Zone {/* dropdown of zones */}
          <select value={form.zone} onChange={update("zone")}>
            {(zones.data || []).map((z) => <option key={z.name}>{z.name}</option>)}
          </select>
        </label>
        <label>Disaster type {/* dropdown of types */}
          <select value={form.disaster_type} onChange={update("disaster_type")}>
            {["Cyclone", "Flood", "Heatwave", "Landslide", "Earthquake"].map((t) => <option key={t}>{t}</option>)}
          </select>
        </label>
        <label>Severity {/* dropdown of severities */}
          <select value={form.severity} onChange={update("severity")}>
            {["Red", "Orange", "Yellow"].map((s) => <option key={s}>{s}</option>)}
          </select>
        </label>
        <label>Impact in (hours): {form.eta_hours} {/* slider 1-72 */}
          <input type="range" min="1" max="72" value={form.eta_hours} onChange={update("eta_hours")} />
        </label>
        <label>Alert radius (km): {form.radius_km} {/* slider 5-100 */}
          <input type="range" min="5" max="100" value={form.radius_km} onChange={update("radius_km")} />
        </label>
        <button className="primary" type="submit">Queue alert</button> {/* submit button */}
        {msg && <p>{msg}</p>} {/* result message */}
      </form>

      <h3>Latest risk readings from the scraper</h3> {/* readings title */}
      <Table rows={logs.data} columns={["timestamp", "zone", "disaster_type", "probability", "source"]} /> {/* readings table */}
    </div>
  );
}
