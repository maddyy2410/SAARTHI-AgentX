"""Regression tests for the audited command-centre bugs.

Run with pytest from the project folder. The file also runs under plain
Python so the checks can be executed when pytest is not installed.
OR-Tools is not required: plan_patients uses the built-in rule here.
"""

import os
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DISASTER_DB", "/tmp/saarthi-import-placeholder.db")

import data  # noqa: E402
import engine  # noqa: E402


def temp_world():
    fd, path = tempfile.mkstemp(prefix="saarthi-test-", suffix=".db")
    os.close(fd)
    os.remove(path)
    data.DB_PATH = path
    data.init_db()
    return path


def remove_world(path):
    if path and os.path.exists(path):
        os.remove(path)


def assert_checks(result):
    failed = [c for c in result["checks"] if not c["ok"]]
    assert not failed, failed


def test_distributed_shipments_are_not_ordered_again():
    zone = {"name": "Ratnagiri", "level": "Critical", "score": 63.5, "disaster_type": "Cyclone",
            "probability": 0.7, "vulnerability": 0.85, "lat": 1, "lon": 1, "population": 1, "open_requests": 0}
    covered = [{"destination": "Ratnagiri", "item": "Food kits", "qty": 1270, "status": "DISTRIBUTED"}]
    needs = engine.supply_needs([zone], covered)
    assert all(row["item"] != "Food kits" for row in needs)

    path = temp_world()
    try:
        data.run_scenario("A")
        plan = data.get_plan()
        moves = [m for m in plan["supply"] if m["zone"] == "Ratnagiri" and m["item"] == "Food kits"]
        assert moves, "baseline should request food for Ratnagiri"
        original = sum(m["qty"] for m in moves)
        chosen = moves[0]
        data.approve(chosen["key"])
        shipment = data.read("SELECT shipment_id, status FROM supply_movement ORDER BY entry_id DESC LIMIT 1")[0]
        assert shipment["status"] == "ALLOCATED"
        for _ in range(len(engine.STATUS_FLOW)):
            latest = data.read("SELECT status FROM supply_movement WHERE shipment_id=:s ORDER BY entry_id DESC LIMIT 1",
                               s=shipment["shipment_id"])[0]["status"]
            if latest == "DISTRIBUTED":
                break
            data.advance_shipment(shipment["shipment_id"])
        again = sum(m["qty"] for m in data.get_plan()["supply"]
                    if m["zone"] == "Ratnagiri" and m["item"] == "Food kits")
        assert again == original - chosen["qty"]
    finally:
        remove_world(path)


def test_incident_choices_are_empty_without_matching_rows():
    rows = [{"id": 1, "status": "OPEN"}, {"id": 2, "status": "IN_TRANSIT"}]
    assert data.incident_ids(rows, "OPEN") == [1]
    assert data.incident_ids(rows, "IN_TRANSIT") == [2]
    assert data.incident_ids(rows, "RESOLVED") == []
    assert data.incident_ids([], "OPEN") == []


def test_scenario_e_fills_thane_civil():
    path = temp_world()
    try:
        data.run_scenario("A")
        result = data.run_scenario("E")
        assert_checks(result)
        hospital = data.read("SELECT name, available_beds, icu_available, total_beds FROM hospitals WHERE id=2")[0]
        assert hospital["name"] == "Thane Civil Hospital"
        assert hospital["available_beds"] == 0
        assert hospital["icu_available"] == 0
        assert all(row["hospital"] != "Thane Civil Hospital" for row in data.get_plan()["patients"])
    finally:
        remove_world(path)


def test_scenario_d_passes_after_reset_and_after_c():
    path = temp_world()
    try:
        data.run_scenario("A")
        fresh = data.run_scenario("D")
        assert_checks(fresh)
        assert fresh["changed"] > 0
        data.run_scenario("A")
        data.run_scenario("C")
        stacked = data.run_scenario("D")
        assert_checks(stacked)
    finally:
        remove_world(path)


def test_stacked_scenarios_a_to_e_pass():
    path = temp_world()
    try:
        for sid in "ABCDE":
            assert_checks(data.run_scenario(sid))
    finally:
        remove_world(path)


def test_open_request_cannot_be_resolved():
    path = temp_world()
    try:
        data.run_scenario("A")
        row = data.read("SELECT id FROM incidents WHERE status='OPEN' ORDER BY id LIMIT 1")[0]
        try:
            data.resolve_incident(row["id"])
        except data.ActionError as exc:
            assert "Claim" in str(exc)
        else:
            raise AssertionError("resolving an open request should be refused")
        status = data.read("SELECT status FROM incidents WHERE id=:i", i=row["id"])[0]["status"]
        assert status == "OPEN"
        data.claim_incident(row["id"], "Red Cross Team 3")
        data.resolve_incident(row["id"])
        status = data.read("SELECT status FROM incidents WHERE id=:i", i=row["id"])[0]["status"]
        assert status == "RESOLVED"
    finally:
        remove_world(path)


def test_failed_approval_rolls_back_every_shipment():
    path = temp_world()
    try:
        data.run_scenario("A")
        data.write("UPDATE inventory SET qty=1 WHERE hub_id=4 AND item='Food kits'")
        inventory = data.read("SELECT hub_id, item, qty FROM inventory ORDER BY hub_id, item")
        shipments = data.read("SELECT COUNT(*) AS n FROM supply_movement")[0]["n"]
        version = data.latest_version()
        try:
            data.approve_all()
        except data.ActionError:
            pass
        else:
            raise AssertionError("approval should stop when a hub does not have the planned stock")
        assert data.read("SELECT hub_id, item, qty FROM inventory ORDER BY hub_id, item") == inventory
        assert data.read("SELECT COUNT(*) AS n FROM supply_movement")[0]["n"] == shipments
        assert data.latest_version() == version
        assert data.verify_ledger()[0] is True
    finally:
        remove_world(path)


def test_patient_counts_are_whole_and_inside_capacity():
    groups = [{"id": 7, "zone": "Raigad", "people": 10}]
    hospitals = [
        {"id": 1, "name": "Near", "available_beds": 4, "total_beds": 10, "icu_available": 1, "icu_total": 2},
        {"id": 2, "name": "Far", "available_beds": 20, "total_beds": 20, "icu_available": 2, "icu_total": 2},
    ]
    travel = {
        ("Raigad", "Near"): {"minutes": 10, "roads": ["R1"]},
        ("Raigad", "Far"): {"minutes": 40, "roads": ["R2"]},
    }
    assignments, unplaced, solver = engine.plan_patients(groups, hospitals, travel, {"Raigad": 80})
    assert solver.startswith("Built-in rule")
    assert unplaced == []
    by_name = {row["hospital"]: row["patients"] for row in assignments}
    assert by_name == {"Near": 4, "Far": 6}
    assert all(isinstance(row["patients"], int) for row in assignments)
    far = next(row for row in assignments if row["hospital"] == "Far")
    assert "no beds left" not in far["reason"]
    assert "all 4 of its free beds are allocated" in far["reason"]

    arcs = {(0, 0): 10.0, (0, 1): 30.0, (1, 0): 12.0}
    overflow = engine.finalize_patient_plan(
        {(0, 0): 10.6, (1, 0): 10.6},
        [{"id": 1, "people": 11}, {"id": 2, "people": 11}],
        [{"available_beds": 15}, {"available_beds": 10}],
        arcs,
    )
    assert all(isinstance(qty, int) for qty in overflow.values())
    assert sum(qty for (g_i, h_i), qty in overflow.items() if h_i == 0) <= 15
    assert sum(qty for (g_i, h_i), qty in overflow.items() if g_i == 0) <= 11
    assert sum(qty for (g_i, h_i), qty in overflow.items() if g_i == 1) <= 11

    moved = engine.finalize_patient_plan(
        {(0, 1): 6},
        [{"id": 1, "people": 6}],
        [{"available_beds": 10}, {"available_beds": 10}],
        {(0, 0): 10.0, (0, 1): 40.0},
    )
    assert moved == {(0, 0): 6}
    reason = engine.patient_reason(
        {"name": "Far", "available_beds": 10, "total_beds": 10, "icu_available": 0, "icu_total": 0},
        {"minutes": 40, "roads": []},
        0,
        [{"name": "Near", "available_beds": 10}, {"name": "Far", "available_beds": 10}],
        {(0, 0): 10.0, (0, 1): 40.0},
        {(0, 1): 4},
    )
    assert "still has 10 beds free" in reason
    assert "no beds left" not in reason


def test_page_alerts_does_not_shadow_data_or_df():
    """Local `data = ...` / `df = ...` in page_alerts makes Python treat those names as
    locals for the whole function, so early df(data.read(...)) raises UnboundLocalError."""
    import ast

    source = (ROOT / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    fn = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "page_alerts")

    def bindings(nodes):
        found = []
        for node in nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store) and node.id in ("data", "df"):
                found.append((node.id, node.lineno))
            if isinstance(node, ast.arg) and node.arg in ("data", "df"):
                found.append((node.arg, getattr(node, "lineno", 0)))
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    name = alias.asname or alias.name.split(".")[0]
                    if name in ("data", "df"):
                        found.append((name, node.lineno))
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    continue
                found.extend(bindings([child]))
        return found

    shadowed = bindings(fn.body)
    assert not shadowed, f"page_alerts must not bind local data/df (shadows module helpers): {shadowed}"


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
