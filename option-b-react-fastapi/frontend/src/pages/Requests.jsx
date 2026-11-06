// =====================================================================
// Requests.jsx : PAGE 6, SOS requests from the field (supply map)
// =====================================================================
import { useState } from "react"; // form values
import { useApi, send } from "../api.js"; // data hook + send helper
import MapView from "../components/MapView.jsx"; // the map
import { Table, ErrorBox } from "../components/common.jsx"; // helpers

export default function Requests({ version, refresh }) {
  const incidents = useApi("/api/incidents", version); // all SOS requests
  const hubs = useApi("/api/hubs", version); // hubs for the map
  const hospitals = useApi("/api/hospitals", version); // hospitals for the map (medical SOS go there)
  const zones = useApi("/api/severity", version); // zones (for the new-SOS form)
  const [claimId, setClaimId] = useState(""); // chosen request to claim
  const [team, setTeam] = useState("Red Cross Team 3"); // team name
  const [resolveId, setResolveId] = useState(""); // chosen request to close
  const [sos, setSos] = useState({ zone: "", need_type: "Medical", people: 10 }); // new-SOS form
  const [msg, setMsg] = useState(""); // feedback

  const all = incidents.data || []; // empty while loading
  const open = all.filter((i) => i.status === "OPEN"); // nobody has taken these
  const moving = all.filter((i) => i.status === "IN_TRANSIT"); // being handled

  const act = async (fn) => { // run an action, then reset the choices and reload
    try { await fn(); setMsg(""); setClaimId(""); setResolveId(""); refresh(); } // success: clear old choices so the first item is picked next
    catch (e) { setMsg("❌ " + e.message); } // show the backend's reason
  };

  const claim = () => act(() => send("PATCH", `/api/incidents/${claimId || open[0]?.id}/claim`, { claimed_by: team })); // claim
  const resolve = () => act(() => send("PATCH", `/api/incidents/${resolveId || moving[0]?.id}/resolve`)); // close
  const submitSos = () => act(() => { // new SOS near the chosen zone's centre
    const z = (zones.data || []).find((x) => x.name === sos.zone) || (zones.data || [])[0]; // chosen or first zone
    return send("POST", "/api/incidents", { // send it
      lat: z.lat + (Math.random() - 0.5) * 0.1, lon: z.lon + (Math.random() - 0.5) * 0.1, // random spot near the centre
      zone: z.name, need_type: sos.need_type, people: Number(sos.people), // details
    });
  });

  return (
    <div>
      <h1>🆘 Field Requests (supply map)</h1> {/* title */}
      <ErrorBox error={incidents.error} /> {/* backend down? */}
      <MapView incidents={all} hubs={hubs.data || []} hospitals={hospitals.data || []} height={400}
        initial={["hospitals", "hubs", "sos"]} /> {/* SOS, hospitals and hubs switched on */}
      <Table rows={all} columns={["id", "reported_at", "zone", "need_type", "people", "status", "claimed_by"]} /> {/* table */}
      {msg && <p>{msg}</p>} {/* feedback */}
      <div className="two-col equal"> {/* claim | close */}
        <div className="card form">
          <h3>Claim a request</h3>
          <select value={claimId} onChange={(e) => setClaimId(e.target.value)}> {/* open requests */}
            {open.map((i) => <option key={i.id} value={i.id}>#{i.id} {i.zone} ({i.need_type})</option>)}
          </select>
          <input value={team} onChange={(e) => setTeam(e.target.value)} placeholder="Team / NGO name" /> {/* team */}
          <button className="primary" disabled={!open.length} onClick={claim}>Claim (mark in transit)</button> {/* claim */}
        </div>
        <div className="card form">
          <h3>Close a request</h3>
          <select value={resolveId} onChange={(e) => setResolveId(e.target.value)}> {/* in-transit requests */}
            {moving.map((i) => <option key={i.id} value={i.id}>#{i.id} {i.zone} ({i.claimed_by})</option>)}
          </select>
          <button disabled={!moving.length} onClick={resolve}>Mark resolved</button> {/* close */}
        </div>
      </div>
      <details className="card form"> {/* click to open */}
        <summary>Log a new SOS manually (citizens would normally submit via the app/API)</summary>
        <select value={sos.zone} onChange={(e) => setSos({ ...sos, zone: e.target.value })}> {/* zone */}
          {(zones.data || []).map((z) => <option key={z.name}>{z.name}</option>)}
        </select>
        <select value={sos.need_type} onChange={(e) => setSos({ ...sos, need_type: e.target.value })}> {/* need */}
          {["Medical", "Food", "Water", "Shelter", "Rescue"].map((n) => <option key={n}>{n}</option>)}
        </select>
        <input type="number" min="1" value={sos.people} onChange={(e) => setSos({ ...sos, people: e.target.value })} /> {/* people */}
        <button onClick={submitSos}>Submit SOS</button> {/* send */}
      </details>
    </div>
  );
}
