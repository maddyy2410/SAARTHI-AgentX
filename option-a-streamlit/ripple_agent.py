"""Ripple agent: read-only domino analysis on the stored coastal Maharashtra network.

This is a deterministic scenario reading. It is not a disaster prediction.
It does not change roads, hospitals, stock, incidents, shipments, or plans,
and it does not send alerts or dispatch responders.
"""

import data
import engine

DISCLAIMER = (
    "Heuristic attention score for this demo. It is not a validated emergency-priority model, "
    "not a prediction, and it does not dispatch resources."
)
NO_HOSPITAL = "No hospital recorded"
NO_STOCK_ROW = "No stock row recorded"
ZERO_STOCK = "Recorded stock: 0"


def analyse(origin, reports=None):
    """Read the current database and explain one origin zone. Nothing is written.

    reports, when passed, are attached to affected zones by zone name. They are
    not written, and they do not change the recorded risk reading or the score.
    """
    zones = data.read("SELECT * FROM zones ORDER BY name")
    known = {z["name"]: z for z in zones}
    if origin not in known:
        raise ValueError(f"No zone recorded with the name {origin}.")
    roads = data.read("SELECT * FROM roads")
    hospitals = data.read("SELECT * FROM hospitals")
    hubs = data.read("SELECT * FROM hubs ORDER BY id")
    inventory = data.read("SELECT * FROM inventory")
    incidents = data.read("SELECT * FROM incidents")
    risks = data.read("""SELECT zone, disaster_type, probability FROM risk_logs
                         WHERE id IN (SELECT MAX(id) FROM risk_logs GROUP BY zone)""")
    risk_by_zone = {r["zone"]: r for r in risks}
    severity = data.severity()
    needs = engine.supply_needs(severity, data.current_shipments())
    zone_names = [z["name"] for z in zones]
    rings = zone_rings(origin, roads, zone_names)
    bridges = bridge_report(roads, zone_names)
    affected = [origin] + rings["secondary"] + rings["extended"]
    recommendations = [
        score_zone(name, known[name], risk_by_zone.get(name), _hospital_for(hospitals, name),
                   _medical_people(incidents, name), roads, zone_names)
        for name in affected
    ]
    located = unlocated = None
    if reports is not None:
        located, unlocated = classify_reports(reports, zone_names)
    def impact(name, hop, role):
        zone = _impact_zone(name, hop, role, known, roads, hospitals, incidents, risk_by_zone)
        if located is not None:
            zone["reported"] = located.get(name, [])
        return zone
    result = {
        "origin": origin,
        "disclaimer": DISCLAIMER,
        "plan_version": data.latest_version(),
        "primary": impact(origin, 0, "primary"),
        "secondary": [impact(n, 1, "secondary") for n in rings["secondary"]],
        "extended": [impact(n, 2, "extended") for n in rings["extended"]],
        "bridges": bridges,
        "timeline": _timeline(origin, rings, roads, zone_names, hubs, risk_by_zone, hospitals, incidents),
        "hospitals": [_hospital_view(n, _hospital_for(hospitals, n)) for n in affected],
        "supply": [_supply_view(n, severity, needs, _usable_risk(risk_by_zone.get(n))) for n in affected],
        "reach": _reach(origin, roads, hubs, inventory),
        "stock": describe_stock(inventory, hubs, data.demo.ITEMS),
        "recommendations": _rank(recommendations),
    }
    if located is not None:
        affected_names = set(affected)
        result["unlocated_reports"] = unlocated
        result["reports_outside_ring"] = [
            fact for name, facts in located.items() if name not in affected_names for fact in facts]
    return result


def report_fact(row):
    """One stored report, kept separate from the recorded risk reading."""
    if not isinstance(row, dict):
        raise TypeError("A citizen report must be a dictionary.")
    zone = row.get("zone")
    if isinstance(zone, str):
        zone = zone.strip() or None
    else:
        zone = None
    return {
        "id": row.get("id"),
        "zone": zone,
        "classification": row.get("classification"),
        "severity": row.get("severity"),
        "people": row.get("people"),
        "urgency": row.get("urgency"),
        "priority": row.get("priority"),
        "validation_status": row.get("validation_status"),
        "created_at": row.get("created_at"),
        "basis": "reported",
    }


def classify_reports(reports, zone_names):
    """Match report zone names to recorded zones. An unresolved zone stays unlocated."""
    if isinstance(reports, (str, bytes)) or not hasattr(reports, "__iter__"):
        raise TypeError("Citizen reports must be a list of rows.")
    known = set(zone_names)
    located = {}
    unlocated = []
    for row in reports:
        fact = report_fact(row)
        if fact["zone"] in known:
            located.setdefault(fact["zone"], []).append(fact)
        else:
            unlocated.append(fact)
    return located, unlocated


def zone_rings(origin, roads, zone_names):
    """Zones one and two hops away on stored zone-to-zone roads, including blocked ones."""
    names = set(zone_names)
    neighbours = {name: set() for name in names}
    for road in roads:
        if road["a"] in names and road["b"] in names:
            neighbours[road["a"]].add(road["b"])
            neighbours[road["b"]].add(road["a"])
    secondary = sorted(neighbours.get(origin, set()))
    extended = set()
    for name in secondary:
        extended.update(neighbours.get(name, set()))
    extended.discard(origin)
    extended.difference_update(secondary)
    return {"secondary": secondary, "extended": sorted(extended), "neighbours": neighbours}


def bridge_report(roads, zone_names):
    """Full-network bridges and zone-only bridges. The road table is not changed."""
    full = full_network_bridges(roads)
    zone_only = zone_graph_bridges(roads, zone_names)
    full_ids = {b["id"] for b in full}
    comparisons = []
    for bridge in zone_only:
        road = next(r for r in roads if r["id"] == bridge["id"])
        path = alternate_path(roads, road["id"], road["a"], road["b"])
        comparisons.append({
            "id": road["id"],
            "a": road["a"],
            "b": road["b"],
            "full_network_bridge": road["id"] in full_ids,
            "zone_graph_bridge": True,
            "alternate": path,
            "note": _bridge_note(road, road["id"] in full_ids, path),
        })
    return {"full_network": full, "zone_graph": zone_only, "comparisons": comparisons}


def full_network_bridges(roads):
    """Roads whose in-memory removal disconnects stored endpoints. Blocked roads are already out."""
    usable = [r for r in roads if _status(r) != "BLOCKED"]
    found = []
    for road in usable:
        remaining = [r for r in usable if r["id"] != road["id"]]
        side_a = _reachable(remaining, road["a"])
        side_b = _reachable(remaining, road["b"])
        if side_a.isdisjoint(side_b):
            found.append({
                "id": road["id"],
                "a": road["a"],
                "b": road["b"],
                "kind": "full-network bridge",
                "unreachable_from_a": sorted(side_b),
                "unreachable_from_b": sorted(side_a),
                "reason": (
                    f"Removing {road['id']} ({road['a']} — {road['b']}) in memory makes "
                    f"{', '.join(sorted(side_b))} unreachable from {road['a']}. "
                    "The stored road status is not changed."
                ),
            })
    return sorted(found, key=lambda b: _road_number(b["id"]))


def zone_graph_bridges(roads, zone_names):
    """Same removal test, using only roads whose two ends are both zones."""
    names = set(zone_names)
    zone_roads = [r for r in roads if r["a"] in names and r["b"] in names and _status(r) != "BLOCKED"]
    found = []
    for road in zone_roads:
        remaining = [r for r in zone_roads if r["id"] != road["id"]]
        side_a = _reachable(remaining, road["a"]) & names
        side_b = _reachable(remaining, road["b"]) & names
        if side_a.isdisjoint(side_b):
            smaller = side_a if len(side_a) <= len(side_b) else side_b
            found.append({
                "id": road["id"],
                "a": road["a"],
                "b": road["b"],
                "kind": "zone-graph bridge",
                "unreachable_from_a": sorted(side_b),
                "unreachable_from_b": sorted(side_a),
                "isolated_side": sorted(smaller),
                "reason": (
                    f"On zone names only, removing {road['id']} separates {road['a']} from {road['b']}. "
                    "This does not by itself mean the full network, including hubs, is cut."
                ),
            })
    return sorted(found, key=lambda b: _road_number(b["id"]))


def alternate_path(roads, blocked_id, start, goal):
    """Fastest remaining route after marking one road blocked in a copy of the list."""
    copied = [dict(r) for r in roads]
    for road in copied:
        if road["id"] == blocked_id:
            road["status"] = "BLOCKED"
    best, how = engine.shortest_from(copied, start)
    if goal not in best or (goal != start and goal not in how):
        return None
    steps = []
    places = [goal]
    place = goal
    while place != start:
        previous, road_id = how[place]
        steps.append(road_id)
        places.append(previous)
        place = previous
    steps.reverse()
    places.reverse()
    return {"roads": steps, "places": places, "from": start, "to": goal}


def score_zone(name, zone, risk, hospital, medical_people, roads, zone_names):
    """One heuristic score. Any missing input leaves the zone unranked."""
    missing = []
    reasons = []
    severity = _severity_contribution(zone, risk, missing, reasons)
    hospital_points, hospital_text = _hospital_contribution(hospital, missing, reasons)
    medical = _medical_contribution(medical_people, missing, reasons)
    connectivity, connectivity_text = _connectivity_contribution(name, roads, zone_names, hospital, missing, reasons)
    complete = not missing
    total = None
    if complete:
        total = round(severity + hospital_points + medical + connectivity, 1)
        reasons.append(f"Heuristic attention score {total} = {severity} + {hospital_points} + {medical} + {connectivity}.")
    return {
        "zone": name,
        "score": total,
        "ranked": complete,
        "missing": missing,
        "severity": severity,
        "hospital": hospital_points,
        "hospital_text": hospital_text,
        "medical": medical,
        "connectivity": connectivity,
        "connectivity_text": connectivity_text,
        "reasons": reasons,
    }


def describe_stock(inventory_rows, hubs, items):
    """A missing inventory row is not the same as a stored quantity of zero."""
    found = {(row["hub_id"], row["item"]): row.get("qty") for row in inventory_rows}
    described = []
    for hub in hubs:
        for item in items:
            key = (hub["id"], item)
            if key not in found or found[key] is None:
                described.append({"hub": hub["name"], "hub_id": hub["id"], "item": item,
                                  "state": "missing", "qty": None, "text": NO_STOCK_ROW})
            elif int(found[key]) == 0:
                described.append({"hub": hub["name"], "hub_id": hub["id"], "item": item,
                                  "state": "zero", "qty": 0, "text": ZERO_STOCK})
            else:
                qty = int(found[key])
                described.append({"hub": hub["name"], "hub_id": hub["id"], "item": item,
                                  "state": "recorded", "qty": qty, "text": f"Recorded stock: {qty}"})
    return described


def _severity_contribution(zone, risk, missing, reasons):
    if risk is None or risk.get("probability") is None:
        missing.append("severity")
        reasons.append("Severity contribution unavailable: no risk reading with a probability is recorded.")
        return None
    if zone.get("vulnerability") is None:
        missing.append("severity")
        reasons.append("Severity contribution unavailable: vulnerability is not recorded.")
        return None
    base = min(100 * float(risk["probability"]) * float(zone["vulnerability"]), 100)
    points = round(0.40 * base, 1)
    reasons.append(
        f"Severity contribution {points}: 0.40 × min(100 × {float(risk['probability']):.2f} × "
        f"{float(zone['vulnerability']):.2f}, 100). Open SOS points are not included here."
    )
    return points


def _hospital_contribution(hospital, missing, reasons):
    if hospital is None:
        missing.append("hospital pressure")
        reasons.append(f"Hospital pressure unavailable: {NO_HOSPITAL}.")
        return None, NO_HOSPITAL
    if hospital.get("multiple"):
        missing.append("hospital pressure")
        reasons.append("Hospital pressure unavailable: more than one hospital is recorded for this zone.")
        return None, "More than one hospital recorded"
    total = hospital.get("total_beds")
    free = hospital.get("available_beds")
    if total is None or free is None:
        missing.append("hospital pressure")
        reasons.append(f"Hospital pressure unavailable: {hospital['name']} does not have both bed counts recorded.")
        return None, "Bed counts not recorded"
    if int(total) <= 0:
        missing.append("hospital pressure")
        reasons.append(
            f"Hospital pressure unavailable: {hospital['name']} has total beds recorded as {int(total)}. "
            "Occupancy cannot be calculated."
        )
        return None, "Total beds recorded as 0 — occupancy cannot be calculated"
    if int(free) > int(total):
        missing.append("hospital pressure")
        reasons.append(f"Hospital pressure unavailable: free beds at {hospital['name']} are greater than total beds.")
        return None, "Bed counts are inconsistent"
    occupancy = (int(total) - int(free)) / int(total)
    points = round(25 * occupancy, 1)
    reasons.append(
        f"Hospital pressure {points}: 25 × ({int(total)} − {int(free)}) / {int(total)} at {hospital['name']}. "
        "ICU and oxygen are shown separately and are not part of this score."
    )
    return points, f"{hospital['name']}: {free} of {total} beds free"


def _medical_contribution(medical_people, missing, reasons):
    if any(people is None for people in medical_people):
        missing.append("open medical patients")
        reasons.append("Open medical patient contribution unavailable: a Medical incident has no people count.")
        return None
    patients = sum(int(people) for people in medical_people)
    points = round(20 * min(patients, 100) / 100, 1)
    if patients == 0:
        reasons.append("Open medical patients 0: no unresolved Medical incident is recorded. This zero is a count, not missing data.")
    else:
        reasons.append(
            f"Open medical patients {points}: 20 × min({patients}, 100) / 100. "
            "The cap of 100 is a display scale, not a clinical threshold."
        )
    return points


def _connectivity_contribution(zone, roads, zone_names, hospital, missing, reasons):
    named = [r for r in roads if r["a"] == zone or r["b"] == zone]
    if not named:
        missing.append("road connectivity")
        reasons.append("Road connectivity unavailable: no stored road names this zone.")
        return None, "No road recorded for this zone"
    usable = [r for r in named if _status(r) != "BLOCKED"]
    if not usable:
        reasons.append("Road connectivity 15: every recorded road into this zone is BLOCKED.")
        return 15, "Every recorded road into this zone is BLOCKED"
    isolating = _road_that_isolates(zone, roads, zone_names)
    if isolating is not None:
        reasons.append(
            f"Road connectivity 15: removing {isolating['id']} ({isolating['a']} — {isolating['b']}) in memory "
            f"disconnects {zone} from every other zone. The stored status is not changed."
        )
        return 15, f"One road, {isolating['id']}, disconnects this zone from every other zone"
    hospital_name = hospital["name"] if hospital else None
    access = _hospital_access_bridge(zone, hospital_name, roads) if hospital_name else None
    if access is not None:
        reasons.append(
            f"Road connectivity 8: {zone} stays connected, but removing {access['id']} "
            f"({access['a']} — {access['b']}) in memory makes {hospital_name} unreachable. "
            "A zone-only bridge does not add points while the full network still connects the zone."
        )
        return 8, f"{access['id']} is the only stored access road to {hospital_name}"
    reasons.append(
        f"Road connectivity 0: no single stored road disconnects {zone} from the rest of the network. "
        "This zero is a calculated result."
    )
    return 0, "No single stored road disconnects this zone from the rest of the network"


def _road_that_isolates(zone, roads, zone_names):
    usable = [r for r in roads if _status(r) != "BLOCKED" and (r["a"] == zone or r["b"] == zone)]
    for road in usable:
        remaining = [r for r in roads if r["id"] != road["id"] and _status(r) != "BLOCKED"]
        reached = _reachable(remaining, zone)
        if not any(other != zone and other in reached for other in zone_names):
            return road
    return None


def _hospital_access_bridge(zone, hospital_name, roads):
    usable = [r for r in roads if _status(r) != "BLOCKED" and (r["a"] == hospital_name or r["b"] == hospital_name)]
    if len(usable) != 1:
        return None
    road = usable[0]
    remaining = [r for r in roads if r["id"] != road["id"] and _status(r) != "BLOCKED"]
    if zone not in _reachable(remaining, hospital_name):
        return road
    return None


def _rank(rows):
    ranked = sorted((r for r in rows if r["ranked"]), key=lambda r: (-r["score"], r["zone"]))
    unranked = [r for r in rows if not r["ranked"]]
    return {"ranked": ranked, "unranked": unranked}


def _impact_zone(name, hop, role, known, roads, hospitals, incidents, risk_by_zone):
    zone = known[name]
    risk = risk_by_zone.get(name)
    touching = [r for r in roads if r["a"] == name or r["b"] == name]
    return {
        "zone": name,
        "role": role,
        "hop": hop,
        "lat": zone["lat"],
        "lon": zone["lon"],
        "vulnerability": zone.get("vulnerability"),
        "population": zone.get("population"),
        "risk": None if risk is None else {"disaster_type": risk["disaster_type"], "probability": risk["probability"]},
        "open_incidents": [
            {"id": i["id"], "need_type": i["need_type"], "people": i["people"], "status": i["status"]}
            for i in incidents if i["zone"] == name and i["status"] != "RESOLVED"
        ],
        "hospital": _hospital_view(name, _hospital_for(hospitals, name)),
        "roads": [_road_fact(r) for r in sorted(touching, key=lambda r: _road_number(r["id"]))],
    }


def _road_fact(road):
    status = _status(road)
    if status == "BLOCKED":
        meaning = "Recorded status BLOCKED. This road is unusable. The analysis did not set this status."
    elif status == "SLOW":
        meaning = "Recorded status SLOW. Travel minutes are doubled. The road is still connected and is not described as closed."
    else:
        meaning = "Recorded status OPEN. This analysis does not treat it as disrupted."
    minutes = engine.road_minutes(road)
    return {"id": road["id"], "a": road["a"], "b": road["b"], "status": status,
            "stored_minutes": road["minutes"], "current_minutes": minutes, "meaning": meaning}


def _timeline(origin, rings, roads, zone_names, hubs, risk_by_zone, hospitals, incidents):
    risk = risk_by_zone.get(origin)
    primary_roads = [r for r in roads if r["a"] == origin or r["b"] == origin]
    immediate = []
    if risk is None or risk.get("probability") is None:
        immediate.append({"tag": "Recorded", "text": f"{origin}: no risk reading is recorded. None is invented."})
    else:
        immediate.append({"tag": "Recorded", "text": (
            f"{origin}: latest recorded reading is {risk['disaster_type']} at probability {float(risk['probability']):.2f}. "
            "This is a stored demo reading, not a forecast."
        )})
    hospital = _hospital_for(hospitals, origin)
    immediate.append({"tag": "Recorded", "text": _hospital_view(origin, hospital)["text"]})
    open_count = sum(1 for i in incidents if i["zone"] == origin and i["status"] != "RESOLVED")
    immediate.append({"tag": "Recorded", "text": f"{origin}: {open_count} unresolved SOS records."})
    for road in sorted(primary_roads, key=lambda r: _road_number(r["id"])):
        immediate.append({"tag": "Recorded", "text": f"{road['id']} {road['a']} — {road['b']}: {_road_fact(road)['meaning']}"})
    immediate.append({"tag": "Simulated", "text": "Immediate means the recorded facts at the origin. It is not a predicted arrival time."})

    short = []
    joins = []
    for name in rings["secondary"]:
        link = next(r for r in roads if {r["a"], r["b"]} == {origin, name})
        joins.append(link)
        short.append({"tag": "Simulated", "text": (
            f"{name} is one zone-hop from {origin} by {link['id']} ({link['a']} — {link['b']}), "
            f"recorded status {_status(link)}. A neighbour is not a confirmed impact."
        )})
    comparisons = {c["id"]: c for c in bridge_report(roads, zone_names)["comparisons"]}
    for link in joins:
        compared = comparisons.get(link["id"])
        if compared and compared["zone_graph_bridge"] and not compared["full_network_bridge"] and compared["alternate"]:
            via = ", ".join(compared["alternate"]["roads"])
            places = " — ".join(compared["alternate"].get("places") or [])
            short.append({"tag": "Simulated", "text": (
                f"{link['id']} is a zone-graph bridge, not a full-network bridge. "
                f"With {link['id']} removed only in memory, {compared['alternate']['to']} remains reachable from "
                f"{compared['alternate']['from']} by {via} ({places})."
            )})
        elif compared and compared["full_network_bridge"]:
            short.append({"tag": "Simulated", "text": (
                f"{link['id']} is a full-network bridge. Removing it in memory disconnects its two ends. "
                "The stored status is not changed."
            )})
    if not short:
        short.append({"tag": "Recorded", "text": f"No other zone shares a stored road with {origin}."})

    extended = []
    for name in rings["extended"]:
        extended.append({"tag": "Simulated", "text": (
            f"{name} is two zone-hops from {origin}. It is in the extended ring because of the stored zone roads, "
            "not because a disruption has been recorded there."
        )})
    corridor_ids = {r["id"] for r in joins if _status(r) != "BLOCKED"}
    dependent = _hubs_behind_corridor(origin, roads, hubs, corridor_ids)
    if dependent:
        for hub_name, lost in dependent:
            extended.append({"tag": "Simulated", "text": (
                f"{hub_name} has no current route from {origin} that avoids {', '.join(sorted(corridor_ids))}. "
                "Those roads were removed only in memory."
            )})
    else:
        extended.append({"tag": "Simulated", "text": (
            "No hub depends only on the one-hop zone roads. "
            "An alternate stored route remains after those roads are removed in memory."
        )})
    extended.append({"tag": "Simulated", "text": "Extended is a scenario stage, not a forecast of when an effect would arrive."})
    return [
        {"stage": "Immediate", "reason": "Recorded facts at the origin zone.", "lines": immediate},
        {"stage": "Short-term", "reason": "Zones one stored road-hop away, and whether that hop is a bridge.", "lines": short},
        {"stage": "Extended", "reason": "Zones two hops away, and hubs whose current routes all cross the one-hop roads.", "lines": extended},
    ]


def _hubs_behind_corridor(origin, roads, hubs, corridor_ids):
    before = _reachable([r for r in roads if _status(r) != "BLOCKED"], origin)
    remaining = [r for r in roads if r["id"] not in corridor_ids and _status(r) != "BLOCKED"]
    after = _reachable(remaining, origin)
    lost = []
    for hub in hubs:
        if hub["name"] in before and hub["name"] not in after:
            lost.append((hub["name"], sorted(before - after)))
    return lost


def _hospital_view(zone, hospital):
    if hospital is None:
        return {"zone": zone, "text": NO_HOSPITAL, "occupancy": None, "icu": "No hospital recorded",
                "oxygen": "No hospital recorded"}
    if hospital.get("multiple"):
        return {"zone": zone, "text": "More than one hospital recorded", "occupancy": None,
                "icu": "More than one hospital recorded", "oxygen": "More than one hospital recorded"}
    icu_total = hospital.get("icu_total")
    icu_free = hospital.get("icu_available")
    if icu_total is None or icu_free is None:
        icu = "ICU not recorded"
    elif int(icu_total) <= 0:
        icu = "ICU capacity recorded as 0 — ICU occupancy cannot be calculated"
    else:
        icu = f"ICU {icu_free} of {icu_total} free (shown only; not in the score)"
    oxygen = hospital.get("oxygen")
    if oxygen is None:
        oxygen_text = "Oxygen stock not recorded"
    elif int(oxygen) == 0:
        oxygen_text = "Recorded oxygen: 0"
    else:
        oxygen_text = f"Recorded oxygen cylinders: {int(oxygen)}"
    total = hospital.get("total_beds")
    free = hospital.get("available_beds")
    if total is None or free is None or int(total) <= 0 or int(free) > int(total):
        occupancy = None
        beds = hospital["name"]
    else:
        occupancy = round((int(total) - int(free)) / int(total), 3)
        beds = f"{hospital['name']}: {free} of {total} beds free"
    return {"zone": zone, "name": hospital["name"], "text": beds, "occupancy": occupancy,
            "icu": icu, "oxygen": oxygen_text}


def _supply_view(zone, severity_rows, needs, has_risk):
    if not has_risk:
        return {"zone": zone, "text": "No recorded risk reading, so the existing supply-need rule was not applied.",
                "items": []}
    row = next(item for item in severity_rows if item["name"] == zone)
    if row["level"] not in engine.PRIORITY_LEVELS:
        return {"zone": zone, "level": row["level"], "items": [], "text": (
            f"Recorded level is {row['level']}. The existing supply-need rule does not calculate a need "
            "for Moderate or Low zones. This is not a need of zero, and it is not an allocation."
        )}
    items = [{"item": n["item"], "need": n["need"]} for n in needs if n["zone"] == zone]
    if not items:
        return {"zone": zone, "level": row["level"], "items": [], "text": (
            "Existing rule: remaining need is 0 after committed shipments. This zero is calculated, not missing stock, "
            "and it is not an allocation."
        )}
    detail = ", ".join(f"{n['need']} {n['item']}" for n in items)
    return {"zone": zone, "level": row["level"], "items": items, "text": (
        f"Existing supply-need rule for this {row['level']} zone: {detail} still uncovered. "
        "No allocation was created and inventory was not changed."
    )}


def _reach(origin, roads, hubs, inventory):
    usable = [r for r in roads if _status(r) != "BLOCKED"]
    best, _how = engine.shortest_from(usable, origin)
    rows = []
    described = {(s["hub"], s["item"]): s for s in describe_stock(inventory, hubs, data.demo.ITEMS)}
    for hub in hubs:
        minutes = best.get(hub["name"])
        stock = [described[(hub["name"], item)] for item in data.demo.ITEMS]
        if minutes is None:
            route = f"No route from {origin} to {hub['name']} on the current usable roads."
        else:
            route = f"{hub['name']} is {engine.fmt_minutes(minutes)} from {origin} on the current usable roads."
        rows.append({"hub": hub["name"], "minutes": minutes, "route": route, "stock": stock})
    return rows


def _usable_risk(risk):
    return risk is not None and risk.get("probability") is not None


def _hospital_for(hospitals, zone):
    matches = [h for h in hospitals if h["zone"] == zone]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        return {"name": None, "multiple": True, "zone": zone}
    return None


def _medical_people(incidents, zone):
    return [i["people"] for i in incidents if i["zone"] == zone and i["need_type"] == "Medical" and i["status"] != "RESOLVED"]


def _bridge_note(road, is_full, path):
    if is_full:
        return f"{road['id']} is both a zone-graph bridge and a full-network bridge."
    if path:
        places = " — ".join(path.get("places") or [])
        return (
            f"{road['id']} is a zone-graph bridge only. An alternate stored route remains by "
            f"{', '.join(path['roads'])} ({places})."
        )
    return f"{road['id']} is a zone-graph bridge only, and no alternate route is stored."


def _reachable(roads, source):
    graph = {}
    for road in roads:
        if _status(road) == "BLOCKED":
            continue
        graph.setdefault(road["a"], []).append(road["b"])
        graph.setdefault(road["b"], []).append(road["a"])
    seen = set()
    stack = [source]
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        for nxt in graph.get(node, []):
            stack.append(nxt)
    return seen


def _status(road):
    return road.get("status") or "OPEN"


def _road_number(road_id):
    digits = "".join(ch for ch in str(road_id) if ch.isdigit())
    return int(digits or 0)
