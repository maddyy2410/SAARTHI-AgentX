"""Plan Challenger: read-only review of a stored disaster-response plan.

Findings cite recorded rows. Recommendations are advice. This module does not
store a new plan or change the database.
"""

import copy

import data
import engine

DISCLAIMER = (
    "This review is advisory. It does not approve shipments, dispatch responders, "
    "or change the stored plan."
)


def review(plan=None, snapshot=None):
    """Review a plan against a snapshot. Both are copied before any checks."""
    recorded = load_snapshot() if snapshot is None else copy.deepcopy(snapshot)
    current = copy.deepcopy(data.get_plan()) if plan is None else copy.deepcopy(plan)
    findings = []
    _check_references(current, recorded, findings)
    supply, duplicates = _unique_supply(current.get("supply") or [], findings)
    _check_hospitals(current.get("patients") or [], current.get("unplaced") or [], recorded, findings)
    _check_stock(supply, recorded, findings)
    _check_routes(supply, current.get("patients") or [], recorded, findings)
    _check_priority(supply, current.get("shortfalls") or [], current.get("patients") or [],
                    current.get("unplaced") or [], recorded, findings)
    _check_demand(supply, current.get("shortfalls") or [], recorded, findings)
    return {"plan_version": current.get("version", recorded.get("plan_version")),
            "reviewed_at": data.now(), "disclaimer": DISCLAIMER, "findings": findings,
            "counts": {"confirmed": sum(1 for f in findings if f["status"] == "confirmed"),
                       "potential": sum(1 for f in findings if f["status"] == "potential"),
                       "missing": sum(1 for f in findings if f["status"] == "missing")}}


def load_snapshot():
    """Read the records a review needs. Nothing is written."""
    return {"zones": data.read("SELECT * FROM zones ORDER BY name"),
            "roads": data.read("SELECT * FROM roads"),
            "hospitals": data.read("SELECT * FROM hospitals ORDER BY id"),
            "hubs": data.read("SELECT * FROM hubs ORDER BY id"),
            "inventory": data.read("SELECT * FROM inventory"),
            "incidents": data.read("SELECT * FROM incidents"),
            "shipments": data.current_shipments(),
            "severity": data.severity(),
            "risks": data.read("""SELECT zone, disaster_type, probability FROM risk_logs
                                  WHERE id IN (SELECT MAX(id) FROM risk_logs GROUP BY zone)"""),
            "plan_version": data.latest_version()}


def _unique_supply(rows, findings):
    """Keep the first row for each zone, item and hub. Later copies are duplicates."""
    seen = {}
    unique = []
    for row in rows:
        identity = (row.get("zone"), row.get("item"), row.get("hub_id"), row.get("from_hub"))
        if identity in seen:
            findings.append(_finding(
                "confirmed", "high", "resources",
                "Duplicate supply allocation. Its quantity was not added again to the resource total.",
                "Keep one allocation for this zone, item and hub. Do not treat the copy as extra stock leaving the hub.",
                {"zone": row.get("zone"), "item": row.get("item"), "hub": row.get("from_hub"),
                 "hub_id": row.get("hub_id"), "plan_key": row.get("key"), "duplicate_of": seen[identity]}))
            continue
        seen[identity] = row.get("key")
        unique.append(row)
    return unique, seen


def _check_references(plan, snapshot, findings):
    zones = {z["name"] for z in snapshot["zones"]}
    hubs = {h["id"]: h for h in snapshot["hubs"]}
    hub_names = {h["name"] for h in snapshot["hubs"]}
    hospitals = {h["id"]: h for h in snapshot["hospitals"]}
    hospital_names = {h["name"]: h for h in snapshot["hospitals"]}
    roads = {r["id"] for r in snapshot["roads"]}
    incidents = {i["id"]: i for i in snapshot["incidents"]}
    for row in plan.get("supply") or []:
        if row.get("zone") not in zones:
            _missing(findings, "supply", f"Supply row names zone {row.get('zone')}, which is not a recorded zone.",
                     "Remove or correct this allocation. No zone was invented.",
                     {"zone": row.get("zone"), "plan_key": row.get("key"), "item": row.get("item")})
        if row.get("hub_id") is not None and row.get("hub_id") not in hubs:
            _missing(findings, "resources", f"Hub id {row.get('hub_id')} is not recorded.",
                     "Do not count stock from a hub that is not in the database.",
                     {"hub": row.get("from_hub"), "hub_id": row.get("hub_id"), "plan_key": row.get("key")})
        elif row.get("from_hub") not in hub_names:
            _missing(findings, "resources", f"Hub {row.get('from_hub')} is not recorded.",
                     "Do not count stock from a hub that is not in the database.",
                     {"hub": row.get("from_hub"), "plan_key": row.get("key")})
        _unknown_roads(findings, row.get("route"), roads, row.get("key"), row.get("zone"))
    for row in plan.get("patients") or []:
        if row.get("incident_id") not in incidents:
            _missing(findings, "hospital", f"Patient assignment cites incident {row.get('incident_id')}, which is not recorded.",
                     "Do not treat this assignment as safe. The incident record is unavailable.",
                     {"incident_id": row.get("incident_id"), "hospital": row.get("hospital"), "plan_key": row.get("key")})
        if row.get("zone") not in zones:
            _missing(findings, "hospital", f"Patient assignment names zone {row.get('zone')}, which is not recorded.",
                     "Correct the zone before accepting the assignment.",
                     {"zone": row.get("zone"), "incident_id": row.get("incident_id")})
        hospital_id = row.get("hospital_id")
        if hospital_id is not None and hospital_id not in hospitals:
            _missing(findings, "hospital", f"Hospital id {hospital_id} is not recorded.",
                     "Do not send patients to a hospital id that is not in the database.",
                     {"hospital_id": hospital_id, "hospital": row.get("hospital"), "incident_id": row.get("incident_id")})
        elif hospital_id is not None and hospitals[hospital_id]["name"] != row.get("hospital"):
            findings.append(_finding(
                "confirmed", "high", "hospital",
                f"Plan hospital id {hospital_id} is now {hospitals[hospital_id]['name']}, not {row.get('hospital')}.",
                "The stored plan refers to an outdated hospital name. Review the assignment before accepting it.",
                {"hospital_id": hospital_id, "hospital": row.get("hospital"), "incident_id": row.get("incident_id")}))
        elif row.get("hospital") not in hospital_names:
            _missing(findings, "hospital", f"Hospital {row.get('hospital')} is not recorded.",
                     "Do not assume that hospital has capacity.",
                     {"hospital": row.get("hospital"), "incident_id": row.get("incident_id")})
        _unknown_roads(findings, row.get("route"), roads, row.get("key"), row.get("zone"))
    for row in plan.get("unplaced") or []:
        if row.get("incident_id") not in incidents:
            _missing(findings, "hospital", f"Unplaced list cites incident {row.get('incident_id')}, which is not recorded.",
                     "Do not treat this unplaced row as a confirmed gap until the incident exists.",
                     {"incident_id": row.get("incident_id"), "zone": row.get("zone")})


def _unknown_roads(findings, route, roads, plan_key, zone):
    if route is None:
        _missing(findings, "roads", "A plan row has no route list.",
                 "Do not treat a missing route as an open road.",
                 {"zone": zone, "plan_key": plan_key})
        return
    for road_id in route:
        if road_id not in roads:
            _missing(findings, "roads", f"Route cites {road_id}, which is not a recorded road.",
                     "The plan is stale or the road id is wrong. Do not assume that road is open.",
                     {"road": road_id, "zone": zone, "plan_key": plan_key})


def _check_hospitals(patients, unplaced, snapshot, findings):
    hospitals = {h["name"]: h for h in snapshot["hospitals"]}
    incidents = {i["id"]: i for i in snapshot["incidents"]}
    totals = {}
    by_incident = {}
    missing_beds = set()
    for row in patients:
        name = row.get("hospital")
        count = row.get("patients")
        if count is None or name is None:
            _missing(findings, "hospital", "A patient assignment is missing the hospital or the patient count.",
                     "Do not assume this assignment fits.",
                     {"hospital": name, "incident_id": row.get("incident_id"), "plan_key": row.get("key")})
            continue
        if name not in hospitals:
            continue
        if hospitals[name].get("available_beds") is None:
            missing_beds.add(name)
            continue
        totals[name] = totals.get(name, 0) + int(count)
        by_incident.setdefault(row.get("incident_id"), 0)
        by_incident[row.get("incident_id")] += int(count)
    for name in sorted(missing_beds):
        _missing(findings, "hospital", f"{name} has no recorded free-bed count.",
                 "A missing bed count is not zero and is not spare capacity.",
                 {"hospital": name, "hospital_id": hospitals[name]["id"], "zone": hospitals[name]["zone"]})
    for name, count in totals.items():
        free = hospitals[name].get("available_beds")
        if int(count) > int(free):
            evidence = {"hospital": name, "hospital_id": hospitals[name]["id"], "zone": hospitals[name]["zone"]}
            _recorded_whole(evidence, "available_beds", free)
            _recorded_whole(evidence, "total_beds", hospitals[name].get("total_beds"))
            findings.append(_finding(
                "confirmed", "critical", "hospital",
                f"{name} is assigned {count} patients and has {free} free beds recorded.",
                "Reduce this assignment or choose a recorded hospital that still has free beds. This review did not move any patients.",
                evidence))
    for incident_id, count in by_incident.items():
        incident = incidents.get(incident_id)
        if incident is None:
            continue
        if incident.get("people") is None:
            _missing(findings, "resources", f"Incident {incident_id} has no recorded number of people.",
                     "Do not compare the assignment with zero.",
                     {"incident_id": incident_id, "zone": incident.get("zone")})
            continue
        if count > int(incident["people"]):
            findings.append(_finding(
                "confirmed", "high", "resources",
                f"Incident {incident_id} records {incident['people']} people, but the plan assigns {count}.",
                "These assignments conflict. Keep only assignments that fit the recorded incident.",
                {"incident_id": incident_id, "zone": incident.get("zone")}))
    for row in unplaced:
        if row.get("patients") is None:
            _missing(findings, "hospital", f"Unplaced patients for incident {row.get('incident_id')} have no count.",
                     "Do not treat the missing count as zero.",
                     {"incident_id": row.get("incident_id"), "zone": row.get("zone")})


def _check_stock(supply, snapshot, findings):
    stock = {}
    present = set()
    for row in snapshot["inventory"]:
        present.add((row["hub_id"], row["item"]))
        stock[(row["hub_id"], row["item"])] = row.get("qty")
    used = {}
    for row in supply:
        if row.get("qty") is None or row.get("item") is None or row.get("hub_id") is None:
            _missing(findings, "resources", "A supply row is missing a quantity, item or hub id.",
                     "Do not assume that row fits the recorded stock.",
                     {"hub": row.get("from_hub"), "hub_id": row.get("hub_id"), "item": row.get("item"),
                      "zone": row.get("zone"), "plan_key": row.get("key")})
            continue
        key = (row["hub_id"], row["item"])
        if key not in present or stock[key] is None:
            _missing(findings, "supply", f"No stock quantity is recorded for {row['item']} at hub {row['hub_id']}.",
                     "A missing stock row is not zero. Do not accept this allocation as covered.",
                     {"hub_id": row["hub_id"], "hub": row.get("from_hub"), "item": row["item"], "zone": row.get("zone")})
            continue
        used[key] = used.get(key, 0) + int(row["qty"])
    hubs = {h["id"]: h["name"] for h in snapshot["hubs"]}
    for key, qty in used.items():
        recorded = int(stock[key])
        if qty > recorded:
            evidence = {"hub_id": key[0], "hub": hubs.get(key[0]), "item": key[1]}
            _recorded_whole(evidence, "qty", stock[key])
            findings.append(_finding(
                "confirmed", "critical", "resources",
                f"The plan sends {qty} {key[1]} from {hubs.get(key[0], key[0])}, which records {recorded}.",
                "Lower the allocation or record more stock. This review did not change inventory.",
                evidence))


def _check_routes(supply, patients, snapshot, findings):
    roads = {r["id"]: r for r in snapshot["roads"]}
    zone_names = [z["name"] for z in snapshot["zones"]]
    seen_roads = set()
    for row in list(supply) + list(patients):
        route = row.get("route")
        if not isinstance(route, list):
            continue
        start = row.get("from_hub") or row.get("zone")
        goal = row.get("zone") if row.get("from_hub") else row.get("hospital")
        blocked = [road_id for road_id in route if road_id in roads and (roads[road_id].get("status") or "OPEN") == "BLOCKED"]
        slow = [road_id for road_id in route if road_id in roads and (roads[road_id].get("status") or "OPEN") == "SLOW"]
        for road_id in blocked:
            if ("blocked", road_id, start, goal) in seen_roads:
                continue
            seen_roads.add(("blocked", road_id, start, goal))
            alternate = _alternate(snapshot["roads"], start, goal, {road_id})
            evidence = {"road": road_id, "zone": row.get("zone"), "hub": row.get("from_hub"),
                        "hospital": row.get("hospital"), "plan_key": row.get("key")}
            if alternate:
                findings.append(_finding(
                    "confirmed", "moderate", "roads",
                    f"{road_id} is BLOCKED and is on the stored route from {start} to {goal}.",
                    "An open alternate is " + ", ".join(alternate) + ". Accepting it would be a separate decision.",
                    evidence))
            else:
                findings.append(_finding(
                    "confirmed", "critical", "roads",
                    f"{road_id} is BLOCKED and no open alternate reaches {goal} from {start}.",
                    "Do not treat this destination as reachable. This review did not change the road.",
                    evidence))
        for road_id in slow:
            if ("slow", road_id) in seen_roads:
                continue
            seen_roads.add(("slow", road_id))
            findings.append(_finding(
                "potential", "moderate", "roads",
                f"{road_id} is SLOW. Travel minutes on that road are doubled. It is not closed.",
                "A person can review the longer travel time. The road status was not changed.",
                {"road": road_id, "zone": row.get("zone"), "plan_key": row.get("key")}))
    for name in zone_names:
        if not _mentioned(name, supply, patients) and not _has_incident(snapshot, name):
            continue
        others = [zone for zone in zone_names if zone != name and zone in _reachable(snapshot["roads"], name)]
        if others:
            continue
        touching = [r["id"] for r in snapshot["roads"] if r["a"] == name or r["b"] == name]
        findings.append(_finding(
            "confirmed", "critical", "roads",
            f"{name} cannot reach another recorded zone on the current usable roads.",
            "Roads touching this zone: " + (", ".join(touching) or "none") + ". No road status was changed.",
            {"zone": name, "road": touching[0] if len(touching) == 1 else None}))


def _check_priority(supply, shortfalls, patients, unplaced, snapshot, findings):
    scores = _scores(snapshot, findings)
    for shortfall in shortfalls:
        if shortfall.get("zone") not in scores or shortfall.get("item") is None:
            _missing(findings, "priority", "A shortfall is missing its zone, item or a recorded severity score.",
                     "Do not rank this shortfall against other zones.",
                     {"zone": shortfall.get("zone"), "item": shortfall.get("item")})
            continue
        for row in supply:
            if row.get("zone") not in scores or row.get("item") != shortfall.get("item"):
                continue
            if scores[row["zone"]] >= scores[shortfall["zone"]]:
                continue
            leg = _travel(snapshot).get((row.get("from_hub"), shortfall["zone"]))
            if leg and leg.get("minutes") is not None:
                findings.append(_finding(
                    "confirmed", "high", "priority",
                    f"{row['zone']} (score {scores[row['zone']]}) receives {row['item']} while {shortfall['zone']} "
                    f"(score {scores[shortfall['zone']]}) is short and {row.get('from_hub')} can reach it.",
                    "Review sending this item to the higher-severity zone first. No allocation was changed.",
                    {"zone": shortfall["zone"], "item": shortfall["item"], "hub": row.get("from_hub"),
                     "hub_id": row.get("hub_id")}))
    hospitals = {h["name"]: h for h in snapshot["hospitals"]}
    assigned = {}
    for row in patients:
        if row.get("hospital") in hospitals and row.get("patients") is not None:
            assigned[row["hospital"]] = assigned.get(row["hospital"], 0) + int(row["patients"])
    for row in unplaced:
        zone = row.get("zone")
        if zone not in scores or row.get("patients") is None:
            continue
        spared = []
        for hospital in snapshot["hospitals"]:
            free = hospital.get("available_beds")
            if free is None:
                continue
            left = int(free) - assigned.get(hospital["name"], 0)
            leg = _travel(snapshot).get((zone, hospital["name"]))
            if left > 0 and leg and leg.get("minutes") is not None:
                spared.append(hospital["name"])
        if spared and _lower_zone_was_placed(patients, scores, zone):
            findings.append(_finding(
                "confirmed", "high", "priority",
                f"{row['patients']} patients from incident {row.get('incident_id')} in {zone} are unplaced while "
                f"{', '.join(spared)} still has free beds on an open route.",
                "Review those beds before accepting the plan. No patient was moved.",
                {"zone": zone, "incident_id": row.get("incident_id"), "hospital": spared[0]}))


def _check_demand(supply, shortfalls, snapshot, findings):
    known = {r["zone"] for r in snapshot["risks"] if r.get("probability") is not None}
    severity = [row for row in snapshot["severity"] if row["name"] in known and row.get("score") is not None]
    needs = engine.supply_needs(severity, snapshot["shipments"])
    planned = {}
    for row in supply:
        if row.get("qty") is None or row.get("zone") is None or row.get("item") is None:
            continue
        key = (row["zone"], row["item"])
        planned[key] = planned.get(key, 0) + int(row["qty"])
    recorded_gap = {(row.get("zone"), row.get("item")): row for row in shortfalls}
    for need in needs:
        key = (need["zone"], need["item"])
        sent = planned.get(key, 0)
        if sent < need["need"]:
            cited = recorded_gap.get(key)
            why = f"{need['zone']} still needs {need['need'] - sent} {need['item']} after this plan."
            if cited:
                why += f" The stored shortfall says {cited.get('missing')} because {cited.get('why')}."
            findings.append(_finding(
                "confirmed", "high", "supply", why,
                "This is the existing supply-need gap. No shipment was created.",
                {"zone": need["zone"], "item": need["item"]}))
        elif sent > need["need"]:
            findings.append(_finding(
                "potential", "moderate", "supply",
                f"The plan sends {sent} {need['item']} to {need['zone']} against a recorded remaining need of {need['need']}.",
                "Review whether the extra quantity is intended. Inventory was not changed.",
                {"zone": need["zone"], "item": need["item"]}))
    for key, row in recorded_gap.items():
        if row.get("why") == "Cut off: no open road from any hub":
            findings.append(_finding(
                "potential", "info", "supply",
                f"{row.get('zone')} is short of {row.get('missing')} {row.get('item')} and the plan says no hub can reach it.",
                "This cites the stored shortfall. It is not a new allocation.",
                {"zone": row.get("zone"), "item": row.get("item")}))


def _scores(snapshot, findings):
    known = {r["zone"] for r in snapshot["risks"] if r.get("probability") is not None}
    scores = {}
    for row in snapshot["severity"]:
        if row["name"] not in known or row.get("vulnerability") is None or row.get("score") is None:
            continue
        scores[row["name"]] = row["score"]
    for zone in {row.get("zone") for row in snapshot.get("incidents", [])}:
        if zone and zone not in scores and zone not in known:
            _missing(findings, "priority", f"No risk reading is recorded for {zone}.",
                     "This zone is not given a priority score of zero.",
                     {"zone": zone})
    return scores


def _lower_zone_was_placed(patients, scores, zone):
    here = scores.get(zone)
    if here is None:
        return False
    return any(scores.get(row.get("zone"), here) < here and row.get("patients") for row in patients)


def _mentioned(name, supply, patients):
    return any(row.get("zone") == name or row.get("from_hub") == name for row in supply) or any(row.get("zone") == name for row in patients)


def _has_incident(snapshot, name):
    return any(row.get("zone") == name and row.get("status") != "RESOLVED" for row in snapshot["incidents"])


def _travel(snapshot):
    zones = [z["name"] for z in snapshot["zones"]]
    sources = zones + [h["name"] for h in snapshot["hubs"]]
    targets = zones + [h["name"] for h in snapshot["hospitals"]]
    return engine.travel_table(snapshot["roads"], sources, targets)


def _alternate(roads, start, goal, skip_ids):
    if not start or not goal:
        return None
    usable = [r for r in roads if r["id"] not in skip_ids and (r.get("status") or "OPEN") != "BLOCKED"]
    best, how = engine.shortest_from(usable, start)
    if goal not in best or (goal != start and goal not in how):
        return None
    steps = []
    place = goal
    while place != start:
        place, road_id = how[place]
        steps.append(road_id)
    steps.reverse()
    return steps


def _reachable(roads, source):
    graph = {}
    for road in roads:
        if (road.get("status") or "OPEN") == "BLOCKED":
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


def _recorded_whole(evidence, key, value):
    """Keep a recorded whole number. A missing or non-whole value is left out."""
    if isinstance(value, bool) or not isinstance(value, int):
        return
    evidence[key] = value


def _missing(findings, area, why, recommendation, evidence):
    findings.append(_finding("missing", "high", area, why, recommendation, evidence))


def _finding(status, risk, area, why, recommendation, evidence):
    clean = {key: value for key, value in evidence.items() if value is not None}
    return {"status": status, "risk": risk, "area": area, "why": why,
            "recommendation": recommendation, "evidence": clean}
