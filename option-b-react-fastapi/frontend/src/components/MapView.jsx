// =====================================================================
// MapView.jsx : the live map (Leaflet + OpenStreetMap), with layers the
// operator can switch on and off: zones, roads, hospitals, hubs, SOS, shipments
// =====================================================================
import { useState } from "react"; // remembers which layers are switched on
import { MapContainer, TileLayer, Circle, CircleMarker, Polyline, Tooltip } from "react-leaflet"; // map pieces
import { LEVEL_COLORS, REQUEST_COLORS, ROAD_COLORS } from "./common.jsx"; // colour tables

const LAYERS = [ // every layer: key, label shown on the checkbox
  { key: "zones", label: "Risk zones" },
  { key: "roads", label: "Roads" },
  { key: "hospitals", label: "Hospitals" },
  { key: "hubs", label: "Supply hubs" },
  { key: "sos", label: "SOS requests" },
  { key: "shipments", label: "Shipments on the move" },
];

export default function MapView({ zones = [], roads = [], hospitals = [], hubs = [], incidents = [], shipments = [],
                                  height = 470, initial = ["zones", "roads", "hospitals", "hubs", "sos", "shipments"] }) {
  const [on, setOn] = useState(new Set(initial)); // switched-on layers
  const toggle = (k) => { const next = new Set(on); next.has(k) ? next.delete(k) : next.add(k); setOn(next); }; // flip one

  return (
    <div className="map-wrap">
      <div className="layer-row" role="group" aria-label="Map layers"> {/* layer switches */}
        {LAYERS.map((l) => (
          <label key={l.key} className="layer-toggle">
            <input type="checkbox" id={`layer-${l.key}`} checked={on.has(l.key)} onChange={() => toggle(l.key)} /> {l.label}
          </label>
        ))}
      </div>
      <MapContainer center={[18.2, 73.4]} zoom={7} style={{ height, width: "100%", borderRadius: 8 }}> {/* the map */}
        <TileLayer url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png" attribution="&copy; OpenStreetMap contributors" />
        {on.has("zones") && zones.map((z) => ( // big circles for risk zones
          <Circle key={`z-${z.name}`} center={[z.lat, z.lon]} radius={8000 + z.score * 500}
            pathOptions={{ color: LEVEL_COLORS[z.level], fillOpacity: 0.25 }}>
            <Tooltip>{z.name}: {z.level}, score {z.score}, {z.disaster_type}</Tooltip>
          </Circle>
        ))}
        {on.has("roads") && roads.filter((r) => r.a_lat && r.b_lat).map((r) => ( // one line per road
          <Polyline key={`r-${r.id}`} positions={[[r.a_lat, r.a_lon], [r.b_lat, r.b_lon]]}
            pathOptions={{ color: ROAD_COLORS[r.status], weight: r.status === "OPEN" ? 3 : 5,
                           dashArray: r.status === "OPEN" ? null : "8 6", opacity: 0.85 }}>
            <Tooltip>{r.id}: {r.a} to {r.b}, {r.status}{r.current_minutes ? `, ${Math.round(r.current_minutes)} min` : ""}</Tooltip>
          </Polyline>
        ))}
        {on.has("hospitals") && hospitals.map((h) => ( // purple dots for hospitals
          <CircleMarker key={`h-${h.id}`} center={[h.lat, h.lon]} radius={9}
            pathOptions={{ color: "#ffffff", weight: 2, fillColor: h.available_beds > 0 ? "#6b4fbb" : "#9ca3af", fillOpacity: 1 }}>
            <Tooltip>{h.name}: {h.available_beds}/{h.total_beds} beds free, ICU {h.icu_available}/{h.icu_total}</Tooltip>
          </CircleMarker>
        ))}
        {on.has("hubs") && hubs.map((h) => ( // blue dots for hubs
          <CircleMarker key={`hub-${h.id}`} center={[h.lat, h.lon]} radius={9}
            pathOptions={{ color: "#ffffff", weight: 2, fillColor: "#1f5fd1", fillOpacity: 1 }}>
            <Tooltip>{h.name} (supply hub)</Tooltip>
          </CircleMarker>
        ))}
        {on.has("sos") && incidents.filter((i) => i.status !== "RESOLVED").map((i) => ( // small SOS dots
          <CircleMarker key={`sos-${i.id}`} center={[i.lat, i.lon]} radius={5}
            pathOptions={{ color: "#ffffff", weight: 1, fillColor: REQUEST_COLORS[i.status], fillOpacity: 1 }}>
            <Tooltip>SOS #{i.id} ({i.need_type}): {i.people} people, {i.status}</Tooltip>
          </CircleMarker>
        ))}
        {on.has("shipments") && shipments.filter((s) => s.lat).map((s) => ( // shipments halfway along their route
          <CircleMarker key={`s-${s.shipment_id}`} center={[s.lat, s.lon]} radius={7}
            pathOptions={{ color: "#111827", weight: 2, fillColor: "#f2c14e", fillOpacity: 1 }}>
            <Tooltip>{s.shipment_id}: {s.qty} {s.item}, {s.source_hub} to {s.destination} ({s.status})</Tooltip>
          </CircleMarker>
        ))}
      </MapContainer>
      <p className="muted small">Zones: red critical, orange high, yellow moderate, green low. Roads: grey open, orange dashed slow, red dashed blocked. Purple: hospitals (grey when full). Blue: hubs. Yellow: shipments on the move.</p>
    </div>
  );
}
