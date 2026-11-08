"""Tests for the What-If sandbox.

These use a temporary database. Hypothetical edits stay in memory.
"""

import os
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DISASTER_DB", "/tmp/saarthi-whatif-placeholder.db")

import data  # noqa: E402
import ripple_agent  # noqa: E402
import whatif_agent  # noqa: E402


def temp_world():
    fd, path = tempfile.mkstemp(prefix="saarthi-whatif-", suffix=".db")
    os.close(fd)
    os.remove(path)
    data.DB_PATH = path
    data.init_db()
    return path


def remove_world(path):
    if path and os.path.exists(path):
        os.remove(path)


def snapshot():
    return {
        "roads": data.read("SELECT id, status, minutes FROM roads ORDER BY id"),
        "hospitals": data.read("SELECT id, available_beds, icu_available, total_beds FROM hospitals ORDER BY id"),
        "inventory": data.read("SELECT hub_id, item, qty FROM inventory ORDER BY hub_id, item"),
        "risks": data.read("SELECT id, zone, probability FROM risk_logs ORDER BY id"),
        "incidents": data.read("SELECT id, zone, need_type, people, status FROM incidents ORDER BY id"),
        "shipments": data.read("SELECT entry_id, shipment_id, qty, status FROM supply_movement ORDER BY entry_id"),
        "plans": data.read("SELECT COUNT(*) AS n FROM plans")[0]["n"],
        "version": data.latest_version(),
    }


def road(result, road_id):
    return next(row for row in result["roads"] if row["id"] == road_id)


def test_sandbox_keeps_the_database_unchanged():
    path = temp_world()
    try:
        before = snapshot()
        saved_path = data.DB_PATH
        blocked = whatif_agent.compare([{"kind": "road", "road_id": "R6", "status": "BLOCKED"}])
        slow = whatif_agent.compare([{"kind": "road", "road_id": "R6", "status": "SLOW"}])
        ratnagiri = next(h for h in before["hospitals"] if h["id"] == 4)
        beds = whatif_agent.compare([{"kind": "beds", "hospital_id": 4, "available_beds": 0}])
        stock = whatif_agent.compare([{"kind": "stock", "hub_id": 4, "item": "Food kits", "qty": 0}])
        recorded_probability = data.read(
            "SELECT probability FROM risk_logs WHERE zone='Ratnagiri' ORDER BY id DESC LIMIT 1")[0]["probability"]
        raised = min(1.0, float(recorded_probability) + 0.2)
        severity = whatif_agent.compare([{"kind": "probability", "zone": "Ratnagiri", "probability": raised}])
        combined = whatif_agent.compare([
            {"kind": "road", "road_id": "R1", "status": "BLOCKED"},
            {"kind": "beds", "hospital_id": 4, "available_beds": 0},
            {"kind": "stock", "hub_id": 4, "item": "Food kits", "qty": 0},
            {"kind": "probability", "zone": "Ratnagiri", "probability": raised},
        ])
        again = whatif_agent.compare([{"kind": "road", "road_id": "R1", "status": "BLOCKED"}])
        reset = whatif_agent.compare(whatif_agent.reset_modifications())
        assert snapshot() == before
        assert data.DB_PATH == saved_path
        assert road(blocked, "R6")["simulated_status"] == "BLOCKED"
        assert road(blocked, "R6")["recorded_status"] == "OPEN"
        assert "not a recorded closure" in road(blocked, "R6")["meaning"]
        route = next(r for r in blocked["routes"] if r["from"] == "Raigad" and r["to"] == "Ratnagiri")
        assert route["recorded_roads"] == ["R6"]
        assert route["simulated_roads"] == ["R5", "R7", "R8"]
        assert "Ratnagiri" not in {row["place"] for row in blocked["isolation"] if row["kind"] == "zone" and row["label"] == "SIMULATED"}
        assert road(slow, "R6")["simulated_minutes"] == 420
        assert road(slow, "R6")["recorded_minutes"] == 210
        assert "not described as closed" in road(slow, "R6")["meaning"]
        hospital = next(h for h in beds["hospitals"] if h["id"] == 4)
        assert hospital["recorded_free"] == ratnagiri["available_beds"]
        assert hospital["simulated_free"] == 0
        assert hospital["simulated_occupancy"] == 1.0
        assert hospital["label"] == "SIMULATED"
        food = next(s for s in stock["stock"] if s["hub_id"] == 4 and s["item"] == "Food kits")
        assert food["text"] == "SIMULATED stock: 0"
        assert food["recorded_qty"] != 0
        severity_row = next(s for s in severity["severity"] if s["zone"] == "Ratnagiri")
        assert severity_row["simulated_probability"] == raised
        assert severity_row["recorded_probability"] == float(recorded_probability)
        assert severity_row["simulated_score"] > severity_row["recorded_score"]
        assert severity_row["label"] == "SIMULATED"
        assert len(combined["modifications"]) == 4
        assert all("SIMULATED" in note for note in combined["modifications"])
        assert "Palghar" in {row["place"] for row in again["isolation"] if row["label"] == "SIMULATED"}
        assert road(again, "R6")["simulated_status"] == "OPEN"
        assert road(blocked, "R1")["simulated_status"] == "OPEN"
        assert reset["modifications"] == []
        assert all(row["recorded_status"] == row["simulated_status"] for row in reset["roads"])
        assert not any(row["label"] == "SIMULATED" for row in reset["isolation"])
        assert "does not dispatch resources" in reset["disclaimer"]
        assert reset["recorded_plan_version"] == before["version"]
    finally:
        remove_world(path)


def test_invalid_inputs_and_independent_copies():
    path = temp_world()
    try:
        before = snapshot()
        original_write = data.write
        data.write = _refuse_write
        try:
            whatif_agent.compare([{"kind": "road", "road_id": "R6", "status": "BLOCKED"}])
        finally:
            data.write = original_write
        for beds in (-1, 181, True):
            try:
                whatif_agent.compare([{"kind": "beds", "hospital_id": 4, "available_beds": beds}])
            except whatif_agent.ScenarioError:
                pass
            else:
                raise AssertionError(f"bed value {beds} should be refused")
        try:
            whatif_agent.compare([{"kind": "stock", "hub_id": 4, "item": "Not a kit", "qty": 0}])
        except whatif_agent.ScenarioError as exc:
            assert "No stock row recorded" in str(exc)
        else:
            raise AssertionError("missing stock should be refused")
        crafted = whatif_agent.fresh_copy()
        crafted["risks"] = [row for row in crafted["risks"] if row["zone"] != "Pune"]
        pune = next(row for row in whatif_agent.severity_rows(crafted) if row["name"] == "Pune")
        assert pune["probability"] is None
        assert pune["score"] is None
        try:
            whatif_agent.apply_modifications(crafted, [{"kind": "probability", "zone": "Pune", "probability": 0.9}])
        except whatif_agent.ScenarioError as exc:
            assert "No risk reading" in str(exc)
        else:
            raise AssertionError("a missing risk reading should be refused")
        first = whatif_agent.fresh_copy()
        second = whatif_agent.fresh_copy()
        whatif_agent.apply_modifications(first, [{"kind": "road", "road_id": "R6", "status": "BLOCKED"}])
        assert next(r for r in second["roads"] if r["id"] == "R6")["status"] == "OPEN"
        assert next(r for r in first["roads"] if r["id"] == "R6")["status"] == "BLOCKED"
        assert snapshot() == before
        source = Path(whatif_agent.__file__).read_text()
        for forbidden in ("run_scenario", "reset_db", "replan", "DB_PATH", ".write(", "event("):
            assert forbidden not in source
    finally:
        remove_world(path)


def test_ripple_context_is_attached_without_changing_the_comparison():
    path = temp_world()
    try:
        before = snapshot()
        plain = whatif_agent.compare([])
        assert "ripple_context" not in plain
        calls = {"analyse": 0}
        original = ripple_agent.analyse

        def counting(origin, reports=None):
            calls["analyse"] += 1
            return original(origin, reports)

        ripple_agent.analyse = counting
        try:
            cascade = counting("Ratnagiri", reports=[
                {"id": 9, "zone": "Ratnagiri", "classification": "Flood", "severity": "High",
                 "people": 40, "urgency": "HIGH", "priority": "HIGH", "validation_status": "valid",
                 "created_at": "2026-11-07 12:00:00"},
            ])
        finally:
            ripple_agent.analyse = original
        blocked = whatif_agent.compare([{"kind": "road", "road_id": "R6", "status": "BLOCKED"}], ripple=cascade)
        same = whatif_agent.compare([{"kind": "road", "road_id": "R6", "status": "BLOCKED"}])
        invalid = whatif_agent.compare([], ripple="not-a-cascade")
        partial = whatif_agent.compare([], ripple={"origin": None, "primary": {}})
        assert calls["analyse"] == 1
        context = blocked["ripple_context"]
        assert context["status"] == "received"
        assert context["basis"] == "inferred"
        assert context["origin"] == "Ratnagiri"
        assert context["affected_zones"][0]["zone"] == "Ratnagiri"
        assert context["affected_zones"][0]["role"] == "primary"
        assert context["affected_zones"][0]["risk"] == cascade["primary"]["risk"]
        assert context["affected_zones"][0]["reported"][0]["severity"] == "High"
        assert context["affected_zones"][0]["reported"][0]["basis"] == "reported"
        assert context["recommendations"] == cascade["recommendations"]
        assert context["recommendations"]["ranked"][0]["score"] == cascade["recommendations"]["ranked"][0]["score"]
        names = [row["zone"] for row in context["affected_zones"]]
        assert names == [cascade["primary"]["zone"]] + [row["zone"] for row in cascade["secondary"]] + [
            row["zone"] for row in cascade["extended"]]
        for key in ("roads", "routes", "isolation", "hospitals", "patients", "stock", "supply", "needs", "severity", "modifications"):
            assert blocked[key] == same[key]
        road = next(row for row in blocked["roads"] if row["id"] == "R6")
        assert road["recorded_status"] == "OPEN"
        assert road["simulated_status"] == "BLOCKED"
        ratnagiri = next(row for row in blocked["severity"] if row["zone"] == "Ratnagiri")
        assert ratnagiri["recorded_probability"] == cascade["primary"]["risk"]["probability"]
        assert ratnagiri["simulated_probability"] == ratnagiri["recorded_probability"]
        assert "ripple_context" not in same
        assert invalid["ripple_context"] == {"status": "unavailable", "result": None, "basis": "inferred"}
        assert invalid["roads"] == plain["roads"]
        assert invalid["severity"] == plain["severity"]
        assert partial["ripple_context"]["status"] == "unavailable"
        assert partial["modifications"] == plain["modifications"]
        assert snapshot() == before
        cascade["primary"]["risk"]["probability"] = 0
        assert blocked["ripple_context"]["affected_zones"][0]["risk"]["probability"] != 0
    finally:
        ripple_agent.analyse = original
        remove_world(path)


def _refuse_write(*_args, **_kwargs):
    raise AssertionError("the sandbox tried to write")


def _run_all():
    failed = 0
    tests = [value for name, value in globals().items() if name.startswith("test_") and callable(value)]
    for test in tests:
        try:
            test()
        except Exception:
            failed += 1
            print(f"FAIL {test.__name__}")
            traceback.print_exc()
        else:
            print(f"PASS {test.__name__}")
    print(f"{len(tests) - failed} passed, {failed} failed")
    return failed


if __name__ == "__main__":
    raise SystemExit(_run_all())
