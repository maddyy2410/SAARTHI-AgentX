"""What-If agent: counterfactual scenarios on an in-memory copy.

Every call reads the database and copies the rows. Hypothetical edits touch
only that copy. Nothing here writes to SQLite, changes the database path, or
sends an alert.
"""

import copy

import data
import engine

DISCLAIMER = (
    "This simulation does not dispatch resources or change live operations. "
    "Hypothetical values are SIMULATED. Values read from the database are RECORDED."
)
ROAD_STATUSES = ("OPEN", "SLOW", "BLOCKED")


class ScenarioError(Exception):
    """A hypothetical change does not fit the recorded data."""


def compare(modifications):
    """Read a fresh baseline, copy it, apply the hypothetical list, and compare.

    A later call reads the database again. It does not reuse this copy.
    """
    recorded = load_baseline()
    simulated = fresh_copy(recorded)
    notes = apply_modifications(simulated, modifications)
    return _comparison(recorded, simulated, notes)


def load_baseline():
    """Recorded rows only. Callers must not treat this object as writable state."""
    zones = data.read("SELECT * FROM zones ORDER BY name")
    return {
        "zones": zones,
        "roads": data.read("SELECT * FROM roads"),
        "hospitals": data.read("SELECT * FROM hospitals ORDER BY id"),
        "hubs": data.read("SELECT * FROM hubs ORDER BY id"),
        "inventory": data.read("SELECT * FROM inventory ORDER BY hub_id, item"),
        "incidents": data.read("SELECT * FROM incidents ORDER BY id"),
        "risks": data.read("""SELECT zone, disaster_type, probability FROM risk_logs
                              WHERE id IN (SELECT MAX(id) FROM risk_logs GROUP BY zone)"""),
        "shipments": data.current_shipments(),
        "plan_version": data.latest_version(),
    }


def fresh_copy(baseline=None):
    """A new in-memory copy. The source object is left as it was."""
    source = load_baseline() if baseline is None else baseline
    return copy.deepcopy(source)


def apply_modifications(state, modifications):
    """Apply hypothetical edits to this copy. Limits use the copy's starting values."""
    original = copy.deepcopy(state)
    notes = []
    for modification in modifications:
        notes.append(_apply_one(state, modification, original))
    return notes


def reset_modifications():
    """An empty hypothetical list. This does not touch the database."""
    return []


def severity_rows(state):
    """Severity from recorded inputs. A missing risk reading stays missing."""
    risks = {row["zone"]: row for row in state["risks"]}
    counts = {}
    for incident in state["incidents"]:
        if incident.get("status") == "OPEN":
            counts[incident["zone"]] = counts.get(incident["zone"], 0) + 1
    rows = []
    for zone in state["zones"]:
        risk = risks.get(zone["name"])
        probability = None if risk is None else risk.get("probability")
        vulnerability = zone.get("vulnerability")
        if probability is None or vulnerability is None:
            rows.append({"name": zone["name"], "probability": None, "vulnerability": vulnerability,
                         "disaster_type": None if risk is None else risk.get("disaster_type"),
                         "open_requests": counts.get(zone["name"], 0), "score": None, "level": None,
                         "missing": True})
            continue
        score = round(min(100 * float(probability) * float(vulnerability) + min(2 * counts.get(zone["name"], 0), 10), 100), 1)
        rows.append({"name": zone["name"], "probability": float(probability), "vulnerability": float(vulnerability),
                     "disaster_type": risk.get("disaster_type"), "open_requests": counts.get(zone["name"], 0),
                     "score": score, "level": engine.level_for(score), "missing": False})
    return rows


def _apply_one(state, modification, original):
    kind = modification.get("kind")
    if kind == "road":
        return _apply_road(state, modification, original)
    if kind == "beds":
        return _apply_beds(state, modification, original)
    if kind == "stock":
        return _apply_stock(state, modification, original)
    if kind == "probability":
        return _apply_probability(state, modification, original)
    raise ScenarioError("Unknown hypothetical change.")


def _apply_road(state, modification, original):
    road_id = modification.get("road_id")
    status = modification.get("status")
    if status not in ROAD_STATUSES:
        raise ScenarioError("Road status must be OPEN, SLOW or BLOCKED.")
    recorded = _find(original["roads"], "id", road_id, f"No road recorded with id {road_id}.")
    current = _find(state["roads"], "id", road_id, f"No road recorded with id {road_id}.")
    current["status"] = status
    return (f"SIMULATED: road {road_id} ({recorded['a']} — {recorded['b']}) set to {status}. "
            f"RECORDED status stays {recorded.get('status') or 'OPEN'}.")


def _apply_beds(state, modification, original):
    hospital_id = modification.get("hospital_id")
    beds = modification.get("available_beds")
    if isinstance(beds, bool) or not isinstance(beds, int):
        raise ScenarioError("Free beds must be a whole number.")
    recorded = _find(original["hospitals"], "id", hospital_id, f"No hospital recorded with id {hospital_id}.")
    total = recorded.get("total_beds")
    if total is None or int(total) < 0:
        raise ScenarioError(f"{recorded['name']} has no usable bed total, so occupancy cannot be calculated.")
    if beds < 0 or beds > int(total):
        raise ScenarioError(f"Free beds must be from 0 through the recorded total of {int(total)} at {recorded['name']}.")
    current = _find(state["hospitals"], "id", hospital_id, f"No hospital recorded with id {hospital_id}.")
    current["available_beds"] = beds
    return (f"SIMULATED: {recorded['name']} free beds set to {beds}. "
            f"RECORDED free beds stay {recorded.get('available_beds')}.")


def _apply_stock(state, modification, original):
    hub_id = modification.get("hub_id")
    item = modification.get("item")
    qty = modification.get("qty")
    if isinstance(qty, bool) or not isinstance(qty, int):
        raise ScenarioError("Stock quantity must be a whole number.")
    hub = _find(original["hubs"], "id", hub_id, f"No hub recorded with id {hub_id}.")
    recorded = _inventory_row(original["inventory"], hub_id, item)
    if recorded is None:
        raise ScenarioError("No stock row recorded.")
    if qty < 0 or qty > int(recorded["qty"]):
        raise ScenarioError(
            f"SIMULATED quantity must be from 0 through the RECORDED quantity of {int(recorded['qty'])} {item}."
        )
    current = _inventory_row(state["inventory"], hub_id, item)
    current["qty"] = qty
    label = "SIMULATED stock: 0" if qty == 0 else f"SIMULATED stock: {qty}"
    return f"{label} {item} at {hub['name']}. RECORDED stock stays {int(recorded['qty'])}."


def _apply_probability(state, modification, original):
    zone = modification.get("zone")
    probability = modification.get("probability")
    if isinstance(probability, bool) or not isinstance(probability, (int, float)):
        raise ScenarioError("Probability must be a number from the recorded value up to 1.")
    probability = float(probability)
    recorded = next((row for row in original["risks"] if row["zone"] == zone), None)
    if recorded is None or recorded.get("probability") is None:
        raise ScenarioError(f"No risk reading is recorded for {zone}. None is invented.")
    recorded_probability = float(recorded["probability"])
    if probability < recorded_probability - 1e-9 or probability > 1:
        raise ScenarioError(
            f"Hypothetical probability for {zone} must be from the recorded {recorded_probability:.2f} up to 1."
        )
    if not any(row["name"] == zone for row in original["zones"]):
        raise ScenarioError(f"No zone recorded with the name {zone}.")
    current = next(row for row in state["risks"] if row["zone"] == zone)
    current["probability"] = probability
    return (f"SIMULATED: {zone} probability set to {probability:.2f}. "
            f"RECORDED probability stays {recorded_probability:.2f}.")


def _comparison(recorded, simulated, notes):
    recorded_severity = severity_rows(recorded)
    simulated_severity = severity_rows(simulated)
    recorded_travel = _travel(recorded)
    simulated_travel = _travel(simulated)
    recorded_needs = engine.supply_needs(_engine_rows(recorded_severity), recorded["shipments"])
    simulated_needs = engine.supply_needs(_engine_rows(simulated_severity), recorded["shipments"])
    recorded_supply = engine.plan_supplies(
        _engine_rows(recorded_severity), recorded["hubs"], recorded["inventory"], recorded["shipments"], recorded_travel)
    simulated_supply = engine.plan_supplies(
        _engine_rows(simulated_severity), simulated["hubs"], simulated["inventory"], recorded["shipments"], simulated_travel)
    groups = _medical_groups(recorded)
    recorded_scores = {row["name"]: row["score"] or 0 for row in recorded_severity}
    simulated_scores = {row["name"]: row["score"] or 0 for row in simulated_severity}
    recorded_patients = engine.plan_patients(groups, recorded["hospitals"], recorded_travel, recorded_scores)
    simulated_patients = engine.plan_patients(groups, simulated["hospitals"], simulated_travel, simulated_scores)
    return {
        "disclaimer": DISCLAIMER,
        "recorded_plan_version": recorded["plan_version"],
        "modifications": notes,
        "roads": _road_rows(recorded, simulated),
        "routes": _route_rows(recorded_travel, simulated_travel),
        "isolation": _isolation(recorded, simulated),
        "hospitals": _hospital_rows(recorded, simulated),
        "patients": _patient_change(recorded_patients, simulated_patients),
        "stock": _stock_rows(recorded, simulated),
        "supply": _supply_change(recorded_supply, simulated_supply),
        "needs": _need_rows(recorded_needs, simulated_needs),
        "severity": _severity_change(recorded_severity, simulated_severity),
    }


def _road_rows(recorded, simulated):
    simulated_by_id = {road["id"]: road for road in simulated["roads"]}
    rows = []
    for road in sorted(recorded["roads"], key=lambda item: _road_number(item["id"])):
        other = simulated_by_id[road["id"]]
        recorded_status = road.get("status") or "OPEN"
        simulated_status = other.get("status") or "OPEN"
        recorded_minutes = engine.road_minutes(road)
        simulated_minutes = engine.road_minutes(other)
        rows.append({"id": road["id"], "a": road["a"], "b": road["b"],
                     "recorded_status": recorded_status, "simulated_status": simulated_status,
                     "recorded_minutes": recorded_minutes, "simulated_minutes": simulated_minutes,
                     "meaning": _road_meaning(recorded_status, simulated_status)})
    return rows


def _road_meaning(recorded_status, simulated_status):
    if simulated_status == recorded_status:
        return f"RECORDED status {recorded_status}. No hypothetical change."
    if simulated_status == "SLOW":
        return ("SIMULATED status SLOW. Travel minutes are doubled. The road is not described as closed. "
                f"RECORDED status stays {recorded_status}.")
    if simulated_status == "BLOCKED":
        return (f"SIMULATED status BLOCKED. This is not a recorded closure. RECORDED status stays {recorded_status}.")
    return f"SIMULATED status {simulated_status}. RECORDED status stays {recorded_status}."


def _route_rows(before, after):
    rows = []
    for key in sorted(before):
        recorded = before[key]
        simulated = after.get(key, {"minutes": None, "roads": []})
        changed = recorded["minutes"] != simulated["minutes"] or recorded["roads"] != simulated["roads"]
        if not changed:
            continue
        rows.append({"from": key[0], "to": key[1], "recorded_minutes": recorded["minutes"],
                     "simulated_minutes": simulated["minutes"], "recorded_roads": list(recorded["roads"]),
                     "simulated_roads": list(simulated["roads"]), "label": "SIMULATED"})
    return rows


def _isolation(recorded, simulated):
    zone_names = [zone["name"] for zone in recorded["zones"]]
    found = []
    for name in zone_names:
        before = _other_zones(_reachable(recorded["roads"], name), zone_names, name)
        after = _other_zones(_reachable(simulated["roads"], name), zone_names, name)
        if before and not after:
            found.append({"place": name, "kind": "zone", "label": "SIMULATED",
                          "text": (f"SIMULATED: {name} cannot reach any other zone. "
                                   "The RECORDED network still connects it.")})
        elif not before and not after:
            found.append({"place": name, "kind": "zone", "label": "RECORDED",
                          "text": f"RECORDED: {name} is already unable to reach another zone. This scenario did not cause it."})
    for hospital in recorded["hospitals"]:
        _facility(found, recorded, simulated, hospital["zone"], hospital["name"], "hospital")
    for hub in recorded["hubs"]:
        for zone in zone_names:
            _facility(found, recorded, simulated, zone, hub["name"], "hub")
    return found


def _facility(found, recorded, simulated, origin, place, kind):
    before = _reachable(recorded["roads"], origin)
    after = _reachable(simulated["roads"], origin)
    if place in before and place not in after:
        found.append({"place": place, "kind": kind, "from": origin, "label": "SIMULATED",
                      "text": (f"SIMULATED: {place} is unreachable from {origin}. "
                               "The RECORDED network still reaches it.")})


def _hospital_rows(recorded, simulated):
    simulated_by_id = {hospital["id"]: hospital for hospital in simulated["hospitals"]}
    rows = []
    for hospital in recorded["hospitals"]:
        other = simulated_by_id[hospital["id"]]
        rows.append({"id": hospital["id"], "name": hospital["name"], "zone": hospital["zone"],
                     "total_beds": hospital.get("total_beds"),
                     "recorded_free": hospital.get("available_beds"),
                     "simulated_free": other.get("available_beds"),
                     "recorded_occupancy": _occupancy(hospital),
                     "simulated_occupancy": _occupancy(other),
                     "icu": _icu_text(hospital),
                     "label": "RECORDED" if hospital.get("available_beds") == other.get("available_beds") else "SIMULATED"})
    return rows


def _patient_change(recorded, simulated):
    recorded_assignments, recorded_unplaced, recorded_solver = recorded
    simulated_assignments, simulated_unplaced, simulated_solver = simulated
    return {"recorded_unplaced": sum(row["patients"] for row in recorded_unplaced),
            "simulated_unplaced": sum(row["patients"] for row in simulated_unplaced),
            "recorded_solver": recorded_solver, "simulated_solver": simulated_solver,
            "recorded_assignments": recorded_assignments, "simulated_assignments": simulated_assignments,
            "label": "SIMULATED"}


def _stock_rows(recorded, simulated):
    hubs = {hub["id"]: hub["name"] for hub in recorded["hubs"]}
    simulated_qty = {(row["hub_id"], row["item"]): row.get("qty") for row in simulated["inventory"]}
    rows = []
    for row in recorded["inventory"]:
        key = (row["hub_id"], row["item"])
        recorded_qty = None if row.get("qty") is None else int(row["qty"])
        other = simulated_qty.get(key)
        simulated_value = None if other is None else int(other)
        if simulated_value == 0 and recorded_qty != 0:
            text = "SIMULATED stock: 0"
        elif simulated_value == recorded_qty:
            text = f"RECORDED stock: {recorded_qty}"
        else:
            text = f"SIMULATED stock: {simulated_value}"
        rows.append({"hub": hubs.get(row["hub_id"], str(row["hub_id"])), "hub_id": row["hub_id"],
                     "item": row["item"], "recorded_qty": recorded_qty, "simulated_qty": simulated_value,
                     "text": text})
    return rows


def _supply_change(recorded, simulated):
    recorded_recs, recorded_short, recorded_solver = recorded
    simulated_recs, simulated_short, simulated_solver = simulated
    diff = engine.diff_plans(engine.plan_rows(recorded_recs, []), engine.plan_rows(simulated_recs, []))
    return {"recorded": recorded_recs, "simulated": simulated_recs, "recorded_shortfalls": recorded_short,
            "simulated_shortfalls": simulated_short, "changed": diff["count"],
            "recorded_solver": recorded_solver, "simulated_solver": simulated_solver, "label": "SIMULATED"}


def _need_rows(recorded, simulated):
    before = {(row["zone"], row["item"]): row["need"] for row in recorded}
    after = {(row["zone"], row["item"]): row["need"] for row in simulated}
    rows = []
    for key in sorted(set(before) | set(after)):
        rows.append({"zone": key[0], "item": key[1], "recorded_need": before.get(key),
                     "simulated_need": after.get(key), "label": "SIMULATED" if before.get(key) != after.get(key) else "RECORDED"})
    return rows


def _severity_change(recorded, simulated):
    simulated_by_name = {row["name"]: row for row in simulated}
    rows = []
    for row in recorded:
        other = simulated_by_name[row["name"]]
        changed = row["score"] != other["score"] or row["level"] != other["level"] or row["probability"] != other["probability"]
        rows.append({"zone": row["name"], "recorded_probability": row["probability"],
                     "simulated_probability": other["probability"], "recorded_score": row["score"],
                     "simulated_score": other["score"], "recorded_level": row["level"],
                     "simulated_level": other["level"], "missing": row["missing"] or other["missing"],
                     "higher_priority": _higher(row["level"], other["level"]),
                     "label": "SIMULATED" if changed else "RECORDED"})
    return rows


def _higher(recorded_level, simulated_level):
    order = {"Low": 0, "Moderate": 1, "High": 2, "Critical": 3}
    if recorded_level not in order or simulated_level not in order:
        return False
    return order[simulated_level] > order[recorded_level]


def _travel(state):
    zones = [zone["name"] for zone in state["zones"]]
    sources = zones + [hub["name"] for hub in state["hubs"]]
    targets = zones + [hospital["name"] for hospital in state["hospitals"]]
    return engine.travel_table(state["roads"], sources, targets)


def _engine_rows(rows):
    usable = []
    for row in rows:
        if row["score"] is None:
            continue
        usable.append({"name": row["name"], "score": row["score"], "level": row["level"],
                       "disaster_type": row["disaster_type"], "probability": row["probability"],
                       "vulnerability": row["vulnerability"]})
    return usable


def _medical_groups(state):
    groups = []
    for incident in state["incidents"]:
        if incident.get("need_type") == "Medical" and incident.get("status") != "RESOLVED" and incident.get("people") is not None:
            groups.append({"id": incident["id"], "zone": incident["zone"], "people": int(incident["people"])})
    return groups


def _occupancy(hospital):
    total = hospital.get("total_beds")
    free = hospital.get("available_beds")
    if total is None or free is None or int(total) <= 0 or int(free) > int(total):
        return None
    return round((int(total) - int(free)) / int(total), 3)


def _icu_text(hospital):
    total = hospital.get("icu_total")
    free = hospital.get("icu_available")
    if total is None or free is None:
        return "ICU not recorded"
    if int(total) <= 0:
        return "ICU capacity recorded as 0 — ICU occupancy cannot be calculated"
    return f"RECORDED ICU {free} of {total} free. ICU is not part of this hypothetical."


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
            if nxt not in seen:
                stack.append(nxt)
    return seen


def _other_zones(reached, zone_names, origin):
    return [name for name in zone_names if name != origin and name in reached]


def _inventory_row(rows, hub_id, item):
    return next((row for row in rows if row["hub_id"] == hub_id and row["item"] == item), None)


def _find(rows, field, value, message):
    found = next((row for row in rows if row.get(field) == value), None)
    if found is None:
        raise ScenarioError(message)
    return found


def _road_number(road_id):
    digits = "".join(ch for ch in str(road_id) if ch.isdigit())
    return int(digits or 0)
