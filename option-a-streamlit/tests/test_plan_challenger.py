"""Tests for the read-only Plan Challenger.

Injected plans stay in memory. The temporary database is compared before and after.
"""

import copy
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DISASTER_DB", "/tmp/saarthi-plan-challenger-placeholder.db")

import data  # noqa: E402
import plan_challenger_agent  # noqa: E402


def temp_world():
    fd, path = tempfile.mkstemp(prefix="saarthi-plan-", suffix=".db")
    os.close(fd)
    os.remove(path)
    data.DB_PATH = path
    data.init_db()
    return path


def remove_world(path):
    if path and os.path.exists(path):
        os.remove(path)


def fingerprint():
    return {
        "roads": data.read("SELECT id, status, minutes FROM roads ORDER BY id"),
        "hospitals": data.read("SELECT id, name, available_beds, icu_available FROM hospitals ORDER BY id"),
        "inventory": data.read("SELECT hub_id, item, qty FROM inventory ORDER BY hub_id, item"),
        "risks": data.read("SELECT id, zone, probability FROM risk_logs ORDER BY id"),
        "incidents": data.read("SELECT id, zone, need_type, people, status FROM incidents ORDER BY id"),
        "shipments": data.read("SELECT entry_id, shipment_id, item, qty, status FROM supply_movement ORDER BY entry_id"),
        "plans": data.read("SELECT COUNT(*) AS n FROM plans")[0]["n"],
        "version": data.latest_version(),
        "path": data.DB_PATH,
    }


def base():
    return plan_challenger_agent.load_snapshot(), copy.deepcopy(data.get_plan())


def supply_row(zone, item, qty, hub_id, hub, route, key=None):
    return {"key": key or f"supply|{zone}|{item}|{hub}", "zone": zone, "level": "High", "item": item,
            "qty": qty, "from_hub": hub, "hub_id": hub_id, "minutes": 10, "route": route, "reason": "test"}


def patient_row(incident_id, zone, patients, hospital, hospital_id, route):
    return {"key": f"patient|{incident_id}|{hospital}", "incident_id": incident_id, "zone": zone,
            "patients": patients, "hospital": hospital, "hospital_id": hospital_id,
            "minutes": 10, "route": route, "reason": "test"}


def has(findings, **expected):
    for row in findings:
        if all(row.get(key) == value for key, value in expected.items() if key != "evidence"):
            evidence = expected.get("evidence", {})
            if all(row["evidence"].get(key) == value for key, value in evidence.items()):
                return row
    return None


def test_source_does_not_change_records():
    text = (ROOT / "plan_challenger_agent.py").read_text()
    for word in ("run_scenario", "reset_db", "replan", "DB_PATH", ".write(", "event(", "approve_all", "approve("):
        assert word not in text


def test_seeded_plan_has_no_confirmed_critical_and_keeps_the_database():
    path = temp_world()
    try:
        before = fingerprint()
        result = plan_challenger_agent.review()
        after = fingerprint()
        assert before == after
        assert result["plan_version"] == before["version"]
        assert result["reviewed_at"]
        assert "advisory" in result["disclaimer"].lower()
        assert not [row for row in result["findings"] if row["status"] == "confirmed" and row["risk"] == "critical"]
    finally:
        remove_world(path)


def test_hospital_overload_is_confirmed():
    path = temp_world()
    try:
        snapshot, plan = base()
        incident = next(row for row in snapshot["incidents"] if row["need_type"] == "Medical" and row["people"])
        plan["patients"] = [patient_row(incident["id"], incident["zone"], 10000, "Raigad District Hospital", 3, ["R17"])]
        plan["supply"] = []
        plan["shortfalls"] = []
        plan["unplaced"] = []
        result = plan_challenger_agent.review(plan, snapshot)
        found = has(result["findings"], status="confirmed", risk="critical", area="hospital",
                    evidence={"hospital_id": 3})
        assert found
        assert "10000" in found["why"]
        assert fingerprint()["version"] == plan["version"]
    finally:
        remove_world(path)


def test_insufficient_stock_is_confirmed_and_a_missing_row_is_not_zero():
    path = temp_world()
    try:
        snapshot, plan = base()
        plan["patients"] = []
        plan["unplaced"] = []
        plan["shortfalls"] = []
        plan["supply"] = [supply_row("Raigad", "Food kits", 50, 1, "Pune Central Warehouse", ["R5"])]
        for row in snapshot["inventory"]:
            if row["hub_id"] == 1 and row["item"] == "Food kits":
                row["qty"] = 10
        result = plan_challenger_agent.review(plan, snapshot)
        assert has(result["findings"], status="confirmed", risk="critical", area="resources",
                   evidence={"hub_id": 1, "item": "Food kits"})
        snapshot["inventory"] = [row for row in snapshot["inventory"] if not (row["hub_id"] == 1 and row["item"] == "Food kits")]
        result = plan_challenger_agent.review(plan, snapshot)
        assert has(result["findings"], status="missing", area="supply", evidence={"hub_id": 1, "item": "Food kits"})
        assert not has(result["findings"], status="confirmed", risk="critical", area="resources",
                       evidence={"hub_id": 1, "item": "Food kits"})
    finally:
        remove_world(path)


def test_blocked_road_with_and_without_an_alternate():
    path = temp_world()
    try:
        snapshot, plan = base()
        plan["patients"] = []
        plan["unplaced"] = []
        plan["shortfalls"] = []
        plan["supply"] = [supply_row("Ratnagiri", "Food kits", 1, 4, "Kolhapur Depot", ["R6"])]
        for road in snapshot["roads"]:
            if road["id"] == "R6":
                road["status"] = "BLOCKED"
        result = plan_challenger_agent.review(plan, snapshot)
        found = has(result["findings"], status="confirmed", risk="moderate", area="roads", evidence={"road": "R6"})
        assert found and "R8" in found["recommendation"]
        plan["supply"] = [supply_row("Palghar", "Food kits", 1, 2, "Thane Relief Hub", ["R1"])]
        for road in snapshot["roads"]:
            if road["id"] == "R1":
                road["status"] = "BLOCKED"
        result = plan_challenger_agent.review(plan, snapshot)
        assert has(result["findings"], status="confirmed", risk="critical", area="roads",
                   evidence={"road": "R1", "zone": "Palghar"})
    finally:
        remove_world(path)


def test_isolated_palghar_and_a_slow_road():
    path = temp_world()
    try:
        snapshot, plan = base()
        for road in snapshot["roads"]:
            if road["id"] == "R1":
                road["status"] = "BLOCKED"
        result = plan_challenger_agent.review(plan, snapshot)
        assert has(result["findings"], status="confirmed", risk="critical", area="roads", evidence={"zone": "Palghar"})
        snapshot, plan = base()
        for road in snapshot["roads"]:
            if road["id"] == "R5":
                road["status"] = "SLOW"
        plan["patients"] = []
        plan["unplaced"] = []
        plan["shortfalls"] = []
        plan["supply"] = [supply_row("Raigad", "Food kits", 1, 1, "Pune Central Warehouse", ["R5"])]
        result = plan_challenger_agent.review(plan, snapshot)
        found = has(result["findings"], status="potential", risk="moderate", area="roads", evidence={"road": "R5"})
        assert found and "not closed" in found["why"]
    finally:
        remove_world(path)


def test_priority_inversion_and_conflicting_patient_counts():
    path = temp_world()
    try:
        snapshot, plan = base()
        plan["patients"] = []
        plan["unplaced"] = []
        plan["supply"] = [supply_row("Pune", "Food kits", 5, 1, "Pune Central Warehouse", ["R14"])]
        plan["shortfalls"] = [{"zone": "Ratnagiri", "item": "Food kits", "missing": 10, "why": "Not enough stock in reachable hubs"}]
        result = plan_challenger_agent.review(plan, snapshot)
        assert has(result["findings"], status="confirmed", risk="high", area="priority",
                   evidence={"zone": "Ratnagiri", "hub_id": 1})
        incident = next(row for row in snapshot["incidents"] if row["need_type"] == "Medical" and row["people"])
        plan["shortfalls"] = []
        plan["supply"] = []
        plan["patients"] = [patient_row(incident["id"], incident["zone"], int(incident["people"]) + 5,
                                        "Pune Trauma Centre", 6, ["R16"])]
        result = plan_challenger_agent.review(plan, snapshot)
        assert has(result["findings"], status="confirmed", risk="high", area="resources",
                   evidence={"incident_id": incident["id"]})
    finally:
        remove_world(path)


def test_duplicate_supply_is_not_counted_twice():
    path = temp_world()
    try:
        snapshot, plan = base()
        plan["patients"] = []
        plan["unplaced"] = []
        plan["shortfalls"] = []
        first = supply_row("Raigad", "Food kits", 40, 1, "Pune Central Warehouse", ["R5"], key="supply|a")
        second = supply_row("Raigad", "Food kits", 40, 1, "Pune Central Warehouse", ["R5"], key="supply|b")
        plan["supply"] = [first, second]
        for row in snapshot["inventory"]:
            if row["hub_id"] == 1 and row["item"] == "Food kits":
                row["qty"] = 50
        result = plan_challenger_agent.review(plan, snapshot)
        duplicate = has(result["findings"], status="confirmed", area="resources", evidence={"plan_key": "supply|b"})
        assert duplicate and "not added again" in duplicate["why"]
        assert not has(result["findings"], status="confirmed", risk="critical", area="resources",
                       evidence={"hub_id": 1, "item": "Food kits"})
    finally:
        remove_world(path)


def test_missing_beds_and_stale_references():
    path = temp_world()
    try:
        snapshot, plan = base()
        incident = next(row for row in snapshot["incidents"] if row["need_type"] == "Medical" and row["people"])
        plan["supply"] = []
        plan["shortfalls"] = []
        plan["unplaced"] = []
        plan["patients"] = [patient_row(incident["id"], incident["zone"], 1, "Raigad District Hospital", 3, ["R17"])]
        for hospital in snapshot["hospitals"]:
            if hospital["id"] == 3:
                hospital["available_beds"] = None
        result = plan_challenger_agent.review(plan, snapshot)
        assert has(result["findings"], status="missing", area="hospital", evidence={"hospital_id": 3})
        assert not has(result["findings"], status="confirmed", risk="critical", area="hospital",
                       evidence={"hospital_id": 3})
        snapshot, plan = base()
        plan["patients"] = [patient_row(999999, "Raigad", 1, "Missing Hospital", 999, ["R99"])]
        plan["supply"] = [supply_row("Nowhere", "Food kits", 1, 999, "Missing Hub", ["R99"])]
        plan["shortfalls"] = []
        plan["unplaced"] = []
        result = plan_challenger_agent.review(plan, snapshot)
        assert has(result["findings"], status="missing", evidence={"incident_id": 999999})
        assert has(result["findings"], status="missing", evidence={"hospital_id": 999})
        assert has(result["findings"], status="missing", evidence={"road": "R99"})
        assert has(result["findings"], status="missing", evidence={"hub_id": 999})
        assert has(result["findings"], status="missing", evidence={"zone": "Nowhere"})
        for hospital in snapshot["hospitals"]:
            if hospital["id"] == 3:
                hospital["name"] = "Renamed Hospital"
        plan["patients"] = [patient_row(incident["id"], incident["zone"], 1, "Raigad District Hospital", 3, ["R17"])]
        plan["supply"] = []
        result = plan_challenger_agent.review(plan, snapshot)
        assert has(result["findings"], status="confirmed", area="hospital", evidence={"hospital_id": 3})
    finally:
        remove_world(path)


def test_combined_findings_do_not_change_the_database_or_share_copies():
    path = temp_world()
    try:
        before = fingerprint()
        snapshot, plan = base()
        incident = next(row for row in snapshot["incidents"] if row["need_type"] == "Medical" and row["people"])
        plan["patients"] = [patient_row(incident["id"], incident["zone"], 500, "Raigad District Hospital", 3, ["R1"])]
        plan["unplaced"] = []
        plan["shortfalls"] = []
        plan["supply"] = [supply_row("Palghar", "Food kits", 99999, 2, "Thane Relief Hub", ["R1"])]
        for road in snapshot["roads"]:
            if road["id"] == "R1":
                road["status"] = "BLOCKED"
        original = copy.deepcopy(snapshot)
        original_write = data.write

        def explode(*_args, **_kwargs):
            raise AssertionError("review tried to write")

        data.write = explode
        try:
            result = plan_challenger_agent.review(plan, snapshot)
            again = plan_challenger_agent.review(plan, original)
        finally:
            data.write = original_write
        assert has(result["findings"], status="confirmed", risk="critical", area="hospital")
        assert has(result["findings"], status="confirmed", risk="critical", area="resources")
        assert has(result["findings"], status="confirmed", risk="critical", area="roads")
        assert snapshot == original
        assert again["plan_version"] == result["plan_version"]
        assert fingerprint() == before
    finally:
        remove_world(path)


def test_confirmed_hospital_evidence_uses_recorded_bed_counts():
    path = temp_world()
    try:
        snapshot, plan = base()
        hospital = next(row for row in snapshot["hospitals"] if row["id"] == 3)
        recorded_free = hospital["available_beds"]
        recorded_total = hospital["total_beds"]
        incident = next(row for row in snapshot["incidents"] if row["need_type"] == "Medical" and row["people"])
        plan["patients"] = [patient_row(incident["id"], incident["zone"], int(recorded_free) + 1,
                                        hospital["name"], 3, ["R17"])]
        plan["supply"] = []
        plan["shortfalls"] = []
        plan["unplaced"] = []
        result = plan_challenger_agent.review(plan, snapshot)
        found = has(result["findings"], status="confirmed", risk="critical", area="hospital",
                    evidence={"hospital_id": 3, "available_beds": recorded_free, "total_beds": recorded_total})
        assert found
        assert found["recommendation"] == (
            "Reduce this assignment or choose a recorded hospital that still has free beds. "
            "This review did not move any patients.")
        hospital["total_beds"] = None
        result = plan_challenger_agent.review(plan, snapshot)
        found = has(result["findings"], status="confirmed", risk="critical", area="hospital",
                    evidence={"hospital_id": 3, "available_beds": recorded_free})
        assert found and "total_beds" not in found["evidence"]
    finally:
        remove_world(path)


def test_confirmed_stock_evidence_uses_the_recorded_quantity():
    path = temp_world()
    try:
        snapshot, plan = base()
        plan["patients"] = []
        plan["unplaced"] = []
        plan["shortfalls"] = []
        plan["supply"] = [supply_row("Raigad", "Food kits", 50, 1, "Pune Central Warehouse", ["R5"])]
        for row in snapshot["inventory"]:
            if row["hub_id"] == 1 and row["item"] == "Food kits":
                row["qty"] = 10
        result = plan_challenger_agent.review(plan, snapshot)
        found = has(result["findings"], status="confirmed", risk="critical", area="resources",
                    evidence={"hub_id": 1, "item": "Food kits", "qty": 10})
        assert found
        assert "records 10" in found["why"]
        assert found["recommendation"] == "Lower the allocation or record more stock. This review did not change inventory."
    finally:
        remove_world(path)


def test_missing_quantities_and_identifiers_are_not_filled_in():
    path = temp_world()
    try:
        snapshot, plan = base()
        incident = next(row for row in snapshot["incidents"] if row["need_type"] == "Medical" and row["people"])
        plan["supply"] = []
        plan["shortfalls"] = []
        plan["unplaced"] = []
        plan["patients"] = [patient_row(incident["id"], incident["zone"], 1, "Raigad District Hospital", 3, ["R17"])]
        for hospital in snapshot["hospitals"]:
            if hospital["id"] == 3:
                hospital["available_beds"] = None
        result = plan_challenger_agent.review(plan, snapshot)
        missing_beds = has(result["findings"], status="missing", area="hospital", evidence={"hospital_id": 3})
        assert missing_beds
        assert "available_beds" not in missing_beds["evidence"]
        assert "total_beds" not in missing_beds["evidence"]
        assert missing_beds["evidence"].get("available_beds") != 0
        for row in snapshot["inventory"]:
            if row["hub_id"] == 1 and row["item"] == "Food kits":
                row["qty"] = None
        plan["patients"] = []
        plan["supply"] = [supply_row("Raigad", "Food kits", 50, 1, "Pune Central Warehouse", ["R5"])]
        result = plan_challenger_agent.review(plan, snapshot)
        missing_stock = has(result["findings"], status="missing", area="supply",
                            evidence={"hub_id": 1, "item": "Food kits"})
        assert missing_stock and "qty" not in missing_stock["evidence"]
        assert not has(result["findings"], status="confirmed", risk="critical", area="resources",
                       evidence={"hub_id": 1, "item": "Food kits"})
        plan["patients"] = [patient_row(999999, "Raigad", 1, "Missing Hospital", 999, ["R99"])]
        plan["supply"] = [supply_row("Nowhere", "Food kits", 1, 999, "Missing Hub", ["R99"])]
        result = plan_challenger_agent.review(plan, snapshot)
        missing_hospital = has(result["findings"], status="missing", evidence={"hospital_id": 999})
        missing_hub = has(result["findings"], status="missing", evidence={"hub_id": 999})
        assert missing_hospital and "available_beds" not in missing_hospital["evidence"]
        assert missing_hub and "qty" not in missing_hub["evidence"]
    finally:
        remove_world(path)
