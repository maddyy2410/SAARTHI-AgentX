"""Tests for the read-only Ripple agent.

Road copies in these tests are in-memory lists. They do not change the database.
"""

import os
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DISASTER_DB", "/tmp/saarthi-ripple-placeholder.db")

import data  # noqa: E402
import engine  # noqa: E402
import ripple_agent  # noqa: E402


def temp_world():
    fd, path = tempfile.mkstemp(prefix="saarthi-ripple-", suffix=".db")
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
        "incidents": data.read("SELECT COUNT(*) AS n FROM incidents")[0]["n"],
        "inventory": data.read("SELECT hub_id, item, qty FROM inventory ORDER BY hub_id, item"),
        "roads": data.read("SELECT id, status FROM roads ORDER BY id"),
        "hospitals": data.read("SELECT id, available_beds, icu_available, oxygen FROM hospitals ORDER BY id"),
        "shipments": data.read("SELECT COUNT(*) AS n FROM supply_movement")[0]["n"],
        "plans": data.read("SELECT COUNT(*) AS n FROM plans")[0]["n"],
        "version": data.latest_version(),
    }


def copied_roads():
    return [dict(row) for row in data.read("SELECT * FROM roads")]


def test_full_network_bridges_differ_from_the_zone_graph():
    path = temp_world()
    try:
        roads = copied_roads()
        zones = [z["name"] for z in data.read("SELECT name FROM zones")]
        report = ripple_agent.bridge_report(roads, zones)
        full_ids = {b["id"] for b in report["full_network"]}
        zone_ids = {b["id"] for b in report["zone_graph"]}
        assert "R6" in zone_ids
        assert "R10" in zone_ids
        assert "R6" not in full_ids
        assert "R10" not in full_ids
        assert "R1" in full_ids and "R1" in zone_ids
        for spur in ("R13", "R14", "R15", "R16", "R17", "R18", "R19", "R20"):
            assert spur in full_ids
            assert spur not in zone_ids
        r6 = next(c for c in report["comparisons"] if c["id"] == "R6")
        assert r6["full_network_bridge"] is False
        assert r6["zone_graph_bridge"] is True
        assert r6["alternate"]["roads"] == ["R5", "R7", "R8"]
        assert "Kolhapur Depot" in r6["note"]
        r1 = next(b for b in report["full_network"] if b["id"] == "R1")
        smaller = r1["unreachable_from_a"] if len(r1["unreachable_from_a"]) < len(r1["unreachable_from_b"]) else r1["unreachable_from_b"]
        assert smaller == ["Palghar"]
        assert "not changed" in r1["reason"]
    finally:
        remove_world(path)


def test_blocked_and_slow_roads_and_hospital_access():
    path = temp_world()
    try:
        slow = copied_roads()
        r6 = next(r for r in slow if r["id"] == "R6")
        r6["status"] = "SLOW"
        assert "R6" not in {b["id"] for b in ripple_agent.full_network_bridges(slow)}
        assert engine.road_minutes(r6) == 420
        assert ripple_agent._road_fact(r6)["meaning"].startswith("Recorded status SLOW")

        blocked = copied_roads()
        for road in blocked:
            if road["id"] in ("R8", "R9"):
                road["status"] = "BLOCKED"
        r6_bridge = next(b for b in ripple_agent.full_network_bridges(blocked) if b["id"] == "R6")
        lost = r6_bridge["unreachable_from_a"]
        assert "Ratnagiri" in lost
        assert "Sindhudurg" in lost
        assert "Ratnagiri Civil Hospital" in lost
        assert "Sindhudurg District Hospital" in lost
        assert "Kolhapur Depot" not in lost
        assert "Pune" not in lost

        open_roads = copied_roads()
        hospital = next(b for b in ripple_agent.full_network_bridges(open_roads) if b["id"] == "R18")
        smaller = hospital["unreachable_from_a"] if len(hospital["unreachable_from_a"]) < len(hospital["unreachable_from_b"]) else hospital["unreachable_from_b"]
        assert smaller == ["Ratnagiri Civil Hospital"]
        assert "Ratnagiri" not in smaller
    finally:
        remove_world(path)


def test_palghar_stock_recommendations_and_database_are_unchanged():
    path = temp_world()
    try:
        before = snapshot()
        result = ripple_agent.analyse("Ratnagiri")
        palghar = ripple_agent.analyse("Palghar")
        assert snapshot() == before
        assert result["plan_version"] == before["version"]
        assert result["primary"]["zone"] == "Ratnagiri"
        assert [z["zone"] for z in result["secondary"]] == ["Raigad", "Sindhudurg"]
        assert [z["zone"] for z in result["extended"]] == ["Mumbai", "Pune"]
        assert [stage["stage"] for stage in result["timeline"]] == ["Immediate", "Short-term", "Extended"]
        assert any("R5" in line["text"] and "R8" in line["text"] for stage in result["timeline"] if stage["stage"] == "Short-term" for line in stage["lines"])
        assert any("OPEN" in line["text"] and line["tag"] == "Recorded" for line in result["timeline"][0]["lines"])

        palghar_hospital = next(h for h in palghar["hospitals"] if h["zone"] == "Palghar")
        assert palghar_hospital["text"] == "No hospital recorded"
        unranked = next(r for r in palghar["recommendations"]["unranked"] if r["zone"] == "Palghar")
        assert "hospital pressure" in unranked["missing"]
        assert unranked["score"] is None
        assert unranked["connectivity"] == 15
        assert unranked["hospital"] is None

        zone = next(z for z in data.read("SELECT * FROM zones") if z["name"] == "Ratnagiri")
        risk = next(r for r in data.read("""SELECT zone, disaster_type, probability FROM risk_logs
                                            WHERE id IN (SELECT MAX(id) FROM risk_logs GROUP BY zone)""")
                    if r["zone"] == "Ratnagiri")
        patients = sum(int(i["people"]) for i in data.read(
            "SELECT people FROM incidents WHERE zone='Ratnagiri' AND need_type='Medical' AND status != 'RESOLVED'"))
        severity = round(0.40 * min(100 * float(risk["probability"]) * float(zone["vulnerability"]), 100), 1)
        hospital = round(25 * (180 - 12) / 180, 1)
        medical = round(20 * min(patients, 100) / 100, 1)
        ranked = next(r for r in result["recommendations"]["ranked"] if r["zone"] == "Ratnagiri")
        assert ranked["severity"] == severity
        assert ranked["hospital"] == hospital
        assert ranked["medical"] == medical
        assert ranked["connectivity"] == 8
        assert ranked["score"] == round(severity + hospital + medical + 8, 1)
        assert "validated emergency-priority model" in result["disclaimer"]

        needs = engine.supply_needs(data.severity(), data.current_shipments())
        shown = next(s for s in result["supply"] if s["zone"] == "Ratnagiri")
        assert shown["items"] == [{"item": n["item"], "need": n["need"]} for n in needs if n["zone"] == "Ratnagiri"]
        assert "No allocation was created" in shown["text"]
        quiet = next(z for z in data.severity() if z["level"] not in ("Critical", "High"))
        quiet_text = next(s for s in ripple_agent.analyse(quiet["name"])["supply"] if s["zone"] == quiet["name"])["text"]
        assert "does not calculate a need" in quiet_text
        assert "not a need of zero" in quiet_text

        stock = ripple_agent.describe_stock(
            [{"hub_id": 1, "item": "Food kits", "qty": 0}],
            [{"id": 1, "name": "Pune Central Warehouse"}],
            ["Food kits", "Water packs"])
        assert stock[0]["text"] == "Recorded stock: 0"
        assert stock[0]["state"] == "zero"
        assert stock[1]["text"] == "No stock row recorded"
        assert stock[1]["state"] == "missing"
        assert all(row["state"] == "recorded" and row["qty"] > 0 for row in result["stock"])

        roads = copied_roads()
        names = [z["name"] for z in data.read("SELECT name FROM zones")]
        hospital_row = {"name": "Ratnagiri Civil Hospital", "zone": "Ratnagiri", "total_beds": 180, "available_beds": 12}
        missing_severity = ripple_agent.score_zone(
            "Ratnagiri", {"vulnerability": None}, {"disaster_type": "Cyclone", "probability": 0.7},
            hospital_row, [22], roads, names)
        assert missing_severity["ranked"] is False
        assert missing_severity["score"] is None
        assert "severity" in missing_severity["missing"]
        missing_patients = ripple_agent.score_zone(
            "Ratnagiri", zone, risk, hospital_row, [None], roads, names)
        assert "open medical patients" in missing_patients["missing"]
        assert missing_patients["medical"] is None
        zero_beds = ripple_agent.score_zone(
            "Ratnagiri", zone, risk, {"name": "Ratnagiri Civil Hospital", "total_beds": 0, "available_beds": 0},
            [22], roads, names)
        assert zero_beds["hospital"] is None
        assert "cannot be calculated" in " ".join(zero_beds["reasons"])
        try:
            ripple_agent.analyse("Atlantis")
        except ValueError as exc:
            assert "Atlantis" in str(exc)
        else:
            raise AssertionError("an unknown zone should be refused")
        assert snapshot() == before
    finally:
        remove_world(path)


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
