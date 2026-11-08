"""Tests for the read-only Coordinator brief.

The temporary database is compared before and after. Write attempts must fail the test.
"""

import copy
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DISASTER_DB", "/tmp/saarthi-coordinator-placeholder.db")

import data  # noqa: E402
import coordinator_agent  # noqa: E402
import intake_agent  # noqa: E402
import plan_challenger_agent  # noqa: E402
import ripple_agent  # noqa: E402
import whatif_agent  # noqa: E402


def temp_world():
    fd, path = tempfile.mkstemp(prefix="saarthi-coordinator-", suffix=".db")
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
        "reports": data.read("SELECT COUNT(*) AS n FROM citizen_reports")[0]["n"],
        "events": data.read("SELECT COUNT(*) AS n FROM events")[0]["n"],
        "path": data.DB_PATH,
    }


def flood():
    return {"zone": "Raigad", "lat": 18.6414, "lon": 72.8722, "disaster_type": "Flood", "people": 40,
            "urgency": "HIGH", "description": "Flood water has entered houses in Raigad."}


def sources(result):
    return {row["source"] for row in result["findings"]}


def test_source_does_not_change_records():
    text = (ROOT / "coordinator_agent.py").read_text()
    for word in ("run_scenario", "reset_db", "replan", "DB_PATH", ".write(", "event(", "approve_all", "approve("):
        assert word not in text


def test_combined_brief_keeps_sources_and_the_database():
    path = temp_world()
    try:
        before = fingerprint()
        original_write = data.write

        def explode(*_args, **_kwargs):
            raise AssertionError("brief tried to write")

        data.write = explode
        try:
            result = coordinator_agent.brief(flood())
        finally:
            data.write = original_write
        assert fingerprint() == before
        assert result["plan_version"] == before["version"]
        assert result["stored_plan"]["version"] == before["version"]
        assert result["records"]["situation"]["severity"]
        for name in ("intake", "ripple", "plan_challenger"):
            assert result["agents"][name]["source"] == name
            assert result["agents"][name]["status"] == "ok"
            assert result["agents"][name]["result"]
        assert result["agents"]["ripple"]["result"]["origin"] == "Raigad"
        assert all(row["source"] in coordinator_agent.SOURCES for row in result["findings"])
        direct_zones = data.read("SELECT name, lat, lon, vulnerability FROM zones")
        direct_existing = data.read("SELECT * FROM citizen_reports ORDER BY priority_score DESC, id DESC")
        assert result["agents"]["intake"]["result"] == intake_agent.evaluate(flood(), direct_zones, direct_existing)
        challenger = result["agents"]["plan_challenger"]["result"]
        again = plan_challenger_agent.review()
        assert challenger["findings"] == again["findings"]
        assert challenger["plan_version"] == again["plan_version"]
        ripple = ripple_agent.analyse("Raigad")
        assert result["agents"]["ripple"]["result"]["origin"] == ripple["origin"]
        assert result["agents"]["ripple"]["result"]["recommendations"] == ripple["recommendations"]
    finally:
        remove_world(path)


def test_missing_zone_is_not_a_zero_score():
    path = temp_world()
    try:
        before = fingerprint()
        report = {"description": "Water is rising near the road.", "people": 12, "urgency": "HIGH"}
        result = coordinator_agent.brief(report)
        assert result["agents"]["intake"]["status"] == "ok"
        assert result["agents"]["intake"]["result"]["zone"] is None
        assert "zone" in result["agents"]["intake"]["missing"]
        assert result["agents"]["ripple"]["status"] == "missing"
        assert result["agents"]["ripple"]["result"] is None
        assert result["agents"]["plan_challenger"]["status"] == "ok"
        ripple_findings = [row for row in result["findings"] if row["source"] == "ripple"]
        assert ripple_findings
        assert all(row["kind"] == "missing" for row in ripple_findings)
        assert all("not a score of zero" in row["detail"] or "not zero" in row["detail"] for row in ripple_findings)
        assert fingerprint() == before
    finally:
        remove_world(path)


def test_one_agent_can_fail_without_dropping_the_others():
    path = temp_world()
    try:
        before = fingerprint()
        unknown = coordinator_agent.brief(flood(), origin="Atlantis")
        assert unknown["agents"]["intake"]["status"] == "ok"
        assert unknown["agents"]["intake"]["result"]["zone"] == "Raigad"
        assert unknown["agents"]["ripple"]["status"] == "failed"
        assert "Atlantis" in unknown["agents"]["ripple"]["error"]
        assert unknown["agents"]["plan_challenger"]["status"] == "ok"
        assert any(row["source"] == "ripple" and row["kind"] == "agent_failure" for row in unknown["findings"])
        original_review = plan_challenger_agent.review
        original_evaluate = intake_agent.evaluate

        def bad_review(*_args, **_kwargs):
            raise RuntimeError("challenger unavailable")

        def bad_evaluate(*_args, **_kwargs):
            raise RuntimeError("intake unavailable")

        plan_challenger_agent.review = bad_review
        try:
            failed_review = coordinator_agent.brief(flood())
        finally:
            plan_challenger_agent.review = original_review
        assert failed_review["agents"]["plan_challenger"]["status"] == "failed"
        assert "challenger unavailable" in failed_review["agents"]["plan_challenger"]["error"]
        assert failed_review["agents"]["intake"]["status"] == "ok"
        assert failed_review["agents"]["ripple"]["status"] == "ok"
        intake_agent.evaluate = bad_evaluate
        try:
            failed_intake = coordinator_agent.brief(flood(), origin="Palghar")
        finally:
            intake_agent.evaluate = original_evaluate
        assert failed_intake["agents"]["intake"]["status"] == "failed"
        assert failed_intake["agents"]["ripple"]["status"] == "ok"
        assert failed_intake["agents"]["ripple"]["result"]["origin"] == "Palghar"
        assert failed_intake["agents"]["plan_challenger"]["status"] == "ok"
        assert fingerprint() == before
    finally:
        remove_world(path)


def test_missing_attention_score_stays_missing_and_sources_stay_attached():
    path = temp_world()
    try:
        result = coordinator_agent.brief(origin="Palghar")
        original = result["agents"]["ripple"]["result"]
        palghar = next(row for row in original["recommendations"]["unranked"] if row["zone"] == "Palghar")
        assert palghar["score"] is None
        assert palghar["ranked"] is False
        found = next(row for row in result["findings"]
                     if row["source"] == "ripple" and row["evidence"].get("zone") == "Palghar")
        assert found["kind"] == "missing"
        assert "not zero" in found["detail"]
        assert result["agents"]["intake"]["status"] == "not_requested"
        for row in result["findings"]:
            assert row["source"] in coordinator_agent.SOURCES
            assert row["detail"]
        challenger = result["agents"]["plan_challenger"]["result"]["findings"]
        challenger_findings = [row for row in result["findings"] if row["source"] == "plan_challenger"]
        assert [row["detail"] for row in challenger_findings] == [row["why"] for row in challenger]
    finally:
        remove_world(path)


def _finding(status, area, why, evidence, risk="critical"):
    return {"status": status, "risk": risk, "area": area, "why": why,
            "recommendation": "Advice only.", "evidence": evidence}


def _with_review(findings):
    def review(*_args, **_kwargs):
        return {"plan_version": data.latest_version(), "reviewed_at": "test", "disclaimer": "test",
                "findings": findings, "counts": {}}
    return review


def _road(result, road_id):
    return next(row for row in result["roads"] if row["id"] == road_id)


def test_confirmed_findings_become_separate_hypothetical_results():
    path = temp_world()
    try:
        before = fingerprint()
        original = plan_challenger_agent.review
        plan_challenger_agent.review = _with_review([
            _finding("confirmed", "roads", "R1 is BLOCKED on the stored route.", {"road": "R1", "zone": "Palghar"}),
            _finding("confirmed", "hospital", "Raigad District Hospital is over its recorded beds.",
                     {"hospital_id": 3, "hospital": "Raigad District Hospital", "available_beds": 5}),
            _finding("confirmed", "resources", "The plan sends more Food kits than hub 1 records.",
                     {"hub_id": 1, "hub": "Pune Central Warehouse", "item": "Food kits", "qty": 10}),
        ])
        try:
            result = coordinator_agent.brief(origin="Raigad")
        finally:
            plan_challenger_agent.review = original
        whatif = result["agents"]["whatif"]
        assert whatif["status"] == "ok"
        assert whatif["hypothetical"] is True
        assert whatif["label"] == "hypothetical"
        assert whatif["modifications"] == [
            {"kind": "road", "road_id": "R1", "status": "BLOCKED"},
            {"kind": "beds", "hospital_id": 3, "available_beds": 5},
            {"kind": "stock", "hub_id": 1, "item": "Food kits", "qty": 10},
        ]
        simulated = whatif["result"]
        assert simulated["recorded_plan_version"] == before["version"]
        road = _road(simulated, "R1")
        assert road["recorded_status"] == "OPEN"
        assert road["simulated_status"] == "BLOCKED"
        assert "SIMULATED" in road["meaning"] and "RECORDED" in road["meaning"]
        hospital = next(row for row in simulated["hospitals"] if row["id"] == 3)
        assert hospital["recorded_free"] == 25
        assert hospital["simulated_free"] == 5
        stock = next(row for row in simulated["stock"] if row["hub_id"] == 1 and row["item"] == "Food kits")
        assert stock["recorded_qty"] == 656
        assert stock["simulated_qty"] == 10
        assert result["agents"]["intake"]["status"] == "not_requested"
        for name in ("ripple", "plan_challenger"):
            assert result["agents"][name]["status"] == "ok"
            assert result["agents"][name]["result"]
        assert fingerprint() == before
    finally:
        remove_world(path)


def test_missing_unsupported_and_unbounded_values_are_not_simulated():
    path = temp_world()
    try:
        before = fingerprint()
        original_review = plan_challenger_agent.review
        original_compare = whatif_agent.compare

        def refuse_compare(_modifications):
            raise AssertionError("compare was called without a safe modification")

        whatif_agent.compare = refuse_compare
        cases = [
            [_finding("confirmed", "roads", "A route is blocked.", {"zone": "Palghar"})],
            [_finding("confirmed", "hospital", "A hospital is over capacity.", {"hospital_id": 3})],
            [_finding("confirmed", "resources", "Stock is exceeded.", {"hub_id": 1, "item": "Food kits"})],
            [_finding("confirmed", "priority", "A lower zone received kits.", {"zone": "Pune", "item": "Food kits"})],
        ]
        try:
            for findings in cases:
                plan_challenger_agent.review = _with_review(findings)
                result = coordinator_agent.brief(origin="Raigad")
                whatif = result["agents"]["whatif"]
                assert whatif["result"] is None
                assert whatif["modifications"] == []
                assert whatif["skipped"]
                assert result["agents"]["plan_challenger"]["result"]["findings"] == findings
                assert any(row["source"] == "whatif" and row["kind"] == "not_simulated" for row in result["findings"])
            plan_challenger_agent.review = _with_review([
                _finding("potential", "roads", "R5 is SLOW.", {"road": "R5"}),
            ])
            potential = coordinator_agent.brief(origin="Raigad")
            assert potential["agents"]["whatif"]["status"] == "not_requested"
            assert potential["agents"]["whatif"]["modifications"] == []
            assert potential["agents"]["plan_challenger"]["result"]["findings"][0]["status"] == "potential"
        finally:
            plan_challenger_agent.review = original_review
            whatif_agent.compare = original_compare
        assert fingerprint() == before
    finally:
        remove_world(path)


def test_whatif_failure_keeps_the_other_agents_and_calls_do_not_share_state():
    path = temp_world()
    try:
        before = fingerprint()
        original_review = plan_challenger_agent.review
        original_compare = whatif_agent.compare

        def explode(_modifications, ripple=None):
            raise RuntimeError("what-if unavailable")

        plan_challenger_agent.review = _with_review([
            _finding("confirmed", "roads", "R6 is BLOCKED.", {"road": "R6"}),
        ])
        whatif_agent.compare = explode
        try:
            failed = coordinator_agent.brief(origin="Ratnagiri")
        finally:
            whatif_agent.compare = original_compare
        assert failed["agents"]["whatif"]["status"] == "failed"
        assert "what-if unavailable" in failed["agents"]["whatif"]["error"]
        assert failed["agents"]["whatif"]["modifications"] == [{"kind": "road", "road_id": "R6", "status": "BLOCKED"}]
        assert failed["agents"]["intake"]["status"] == "not_requested"
        for name in ("ripple", "plan_challenger"):
            assert failed["agents"][name]["status"] == "ok"
            assert failed["agents"][name]["result"]
        plan_challenger_agent.review = _with_review([
            _finding("confirmed", "roads", "R1 is BLOCKED.", {"road": "R1"}),
        ])
        try:
            first = coordinator_agent.brief(origin="Palghar")
            first["agents"]["whatif"]["modifications"].append({"kind": "stock", "hub_id": 1, "item": "Food kits", "qty": 1})
            plan_challenger_agent.review = _with_review([
                _finding("confirmed", "resources", "Use a smaller recorded quantity.",
                         {"hub_id": 2, "item": "Medical kits", "qty": 4}),
            ])
            second = coordinator_agent.brief(origin="Thane")
        finally:
            plan_challenger_agent.review = original_review
        assert first["agents"]["whatif"]["result"]["modifications"] != second["agents"]["whatif"]["result"]["modifications"]
        assert _road(first["agents"]["whatif"]["result"], "R1")["simulated_status"] == "BLOCKED"
        assert _road(second["agents"]["whatif"]["result"], "R1")["simulated_status"] == "OPEN"
        stock = next(row for row in second["agents"]["whatif"]["result"]["stock"]
                     if row["hub_id"] == 2 and row["item"] == "Medical kits")
        assert stock["simulated_qty"] == 4
        assert stock["recorded_qty"] != 4
        assert fingerprint() == before
    finally:
        plan_challenger_agent.review = original_review
        whatif_agent.compare = original_compare
        remove_world(path)


def live_stamp():
    path = ROOT / "disaster.db"
    raw = path.read_bytes()
    return hashlib.sha256(raw).hexdigest(), path.stat().st_mtime, len(raw)


def store_stock_overrun(plan, row, overrun_qty):
    """Save one plan version in the temporary database. The inventory rows stay as recorded."""
    version = int(plan["version"]) + 1
    supply = copy.deepcopy(plan["supply"])
    target = next(item for item in supply if item["key"] == row["key"])
    target["qty"] = overrun_qty
    for kind, rows in (("supply", supply), ("patient", plan["patients"])):
        for item in rows:
            payload = {key: value for key, value in item.items() if key not in ("key", "changed")}
            data.write("INSERT INTO plans(version, kind, plan_key, summary, data) VALUES (:v, :k, :key, :s, :d)",
                       v=version, k=kind, key=item["key"], s="Controlled stock overrun", d=json.dumps(payload))
    data.write("""INSERT INTO plan_runs VALUES (:v, :t, :tt, :tx, :s, :c, :ck, :sf, :up)""",
               v=version, t=data.now(), tt="TEST", tx="Controlled stock overrun", s="test", c=1, ck="[]",
               sf=json.dumps(plan["shortfalls"]), up=json.dumps(plan["unplaced"]))


def test_real_stock_finding_sets_the_hypothetical_to_the_recorded_quantity():
    path = temp_world()
    try:
        live_before = live_stamp()
        plan = data.get_plan()
        inventory = {(row["hub_id"], row["item"]): row["qty"]
                     for row in data.read("SELECT hub_id, item, qty FROM inventory")}
        row = next(item for item in plan["supply"]
                   if isinstance(inventory.get((item["hub_id"], item["item"])), int)
                   and not isinstance(inventory.get((item["hub_id"], item["item"])), bool))
        hub_id, item, recorded_qty = row["hub_id"], row["item"], inventory[(row["hub_id"], row["item"])]
        store_stock_overrun(plan, row, int(recorded_qty) + 1)
        snapshot = copy.deepcopy(plan_challenger_agent.load_snapshot())
        direct = plan_challenger_agent.review()
        finding = next(found for found in direct["findings"]
                       if found["status"] == "confirmed" and found["area"] == "resources"
                       and found["evidence"].get("hub_id") == hub_id and found["evidence"].get("item") == item)
        assert finding["evidence"]["qty"] == recorded_qty
        assert finding["risk"] == "critical"
        before = fingerprint()
        original_write = data.write

        def explode(*_args, **_kwargs):
            raise AssertionError("brief tried to write")

        data.write = explode
        try:
            result = coordinator_agent.brief(origin="Raigad")
        finally:
            data.write = original_write
        challenger = result["agents"]["plan_challenger"]["result"]
        assert challenger["findings"] == direct["findings"]
        assert challenger["findings"] == plan_challenger_agent.review()["findings"]
        whatif = result["agents"]["whatif"]
        assert whatif["status"] == "ok"
        assert whatif["hypothetical"] is True
        assert whatif["label"] == "hypothetical"
        assert whatif["modifications"] == [
            {"kind": "stock", "hub_id": hub_id, "item": item, "qty": recorded_qty}
        ]
        stock = next(entry for entry in whatif["result"]["stock"]
                     if entry["hub_id"] == hub_id and entry["item"] == item)
        assert stock["recorded_qty"] == recorded_qty
        assert stock["simulated_qty"] == recorded_qty
        assert stock["text"] == f"RECORDED stock: {recorded_qty}"
        assert whatif["result"]["supply"]["changed"] == 0
        assert plan_challenger_agent.load_snapshot() == snapshot
        assert fingerprint() == before
        assert data.DB_PATH == path
        assert live_stamp() == live_before
    finally:
        remove_world(path)


def test_received_simulation_is_kept_and_not_recalculated():
    path = temp_world()
    try:
        before = fingerprint()
        omitted = coordinator_agent.brief(origin="Ratnagiri")
        assert "received_simulation" not in omitted
        calls = {"count": 0}
        original = whatif_agent.compare

        def counting(modifications):
            calls["count"] += 1
            return original(modifications)

        whatif_agent.compare = counting
        try:
            produced = whatif_agent.compare([{"kind": "road", "road_id": "R6", "status": "BLOCKED"}])
        finally:
            whatif_agent.compare = original
        produced["marker"] = "from-whatif"
        produced["roads"][0]["simulated_status"] = produced["roads"][0].get("simulated_status")
        missing = {"recorded_plan_version": None, "modifications": None,
                   "roads": [{"id": "R1", "recorded_status": "OPEN", "simulated_status": None, "meaning": None}],
                   "severity": [{"zone": "Palghar", "recorded_probability": None, "simulated_probability": None,
                                 "recorded_score": None, "simulated_score": None, "missing": True}]}
        reports = [{"id": 9, "zone": "Ratnagiri", "classification": "Flood", "severity": "High", "people": 40,
                    "urgency": "HIGH", "priority": "HIGH", "validation_status": "valid",
                    "created_at": "2026-11-07 12:00:00"}]
        whatif_agent.compare = counting
        try:
            received = coordinator_agent.brief(origin="Ratnagiri", reports=reports, simulation=produced)
            unavailable = coordinator_agent.brief(origin="Ratnagiri", simulation="not-a-result")
            held = coordinator_agent.brief(origin="Ratnagiri", simulation=missing)
        finally:
            whatif_agent.compare = original
        assert calls["count"] == 1
        assert received["agents"]["whatif"]["modifications"] == omitted["agents"]["whatif"]["modifications"]
        assert received["agents"]["whatif"]["status"] == omitted["agents"]["whatif"]["status"]
        simulation = received["received_simulation"]
        assert simulation["status"] == "received"
        assert simulation["basis"] == "simulated"
        assert simulation["result"]["marker"] == "from-whatif"
        assert simulation["result"]["modifications"] == produced["modifications"]
        road = next(row for row in simulation["result"]["roads"] if row["id"] == "R6")
        assert road["recorded_status"] == "OPEN"
        assert road["simulated_status"] == "BLOCKED"
        assert "SIMULATED" in road["meaning"] and "RECORDED" in road["meaning"]
        ripple = received["agents"]["ripple"]["result"]
        assert ripple["primary"]["risk"] == omitted["agents"]["ripple"]["result"]["primary"]["risk"]
        assert ripple["primary"]["reported"][0]["severity"] == "High"
        assert ripple["primary"]["reported"][0]["basis"] == "reported"
        assert ripple["recommendations"] == omitted["agents"]["ripple"]["result"]["recommendations"]
        assert unavailable["received_simulation"] == {"status": "unavailable", "result": None, "basis": "simulated"}
        assert unavailable["agents"]["ripple"]["status"] == "ok"
        held_road = held["received_simulation"]["result"]["roads"][0]
        assert held_road["simulated_status"] is None
        assert held_road["meaning"] is None
        assert held["received_simulation"]["result"]["recorded_plan_version"] is None
        assert held["received_simulation"]["result"]["severity"][0]["simulated_score"] is None
        assert fingerprint() == before
    finally:
        remove_world(path)


def test_existing_ripple_result_reaches_whatif_without_another_analysis():
    path = temp_world()
    try:
        before = fingerprint()
        calls = {"analyse": 0}
        original_analyse = ripple_agent.analyse
        original_review = plan_challenger_agent.review

        def counting(origin, reports=None):
            calls["analyse"] += 1
            return original_analyse(origin, reports)

        ripple_agent.analyse = counting
        plan_challenger_agent.review = _with_review([
            _finding("confirmed", "roads", "R6 is BLOCKED.", {"road": "R6"}),
        ])
        try:
            result = coordinator_agent.brief(origin="Ratnagiri")
            idle = coordinator_agent.brief()
        finally:
            ripple_agent.analyse = original_analyse
            plan_challenger_agent.review = original_review
        assert calls["analyse"] == 1
        ripple = result["agents"]["ripple"]["result"]
        simulated = result["agents"]["whatif"]["result"]
        context = simulated["ripple_context"]
        assert context["status"] == "received"
        assert context["basis"] == "inferred"
        assert context["origin"] == ripple["origin"] == "Ratnagiri"
        assert context["affected_zones"][0]["risk"] == ripple["primary"]["risk"]
        assert context["recommendations"] == ripple["recommendations"]
        assert context["timeline"] == ripple["timeline"]
        plain = whatif_agent.compare([{"kind": "road", "road_id": "R6", "status": "BLOCKED"}])
        for key in ("roads", "severity", "hospitals", "stock", "modifications", "supply", "needs"):
            assert simulated[key] == plain[key]
        assert "ripple_context" not in plain
        assert idle["agents"]["ripple"]["status"] == "missing"
        assert "ripple_context" not in idle["agents"]["whatif"]["result"]
        assert fingerprint() == before
    finally:
        ripple_agent.analyse = original_analyse
        plan_challenger_agent.review = original_review
        remove_world(path)


def test_proposed_plan_is_reviewed_without_approval_or_writes():
    path = temp_world()
    try:
        before = fingerprint()
        omitted = coordinator_agent.brief(origin="Ratnagiri")
        assert "proposed_evaluation" not in omitted
        stored = data.get_plan()
        proposed = copy.deepcopy(stored)
        proposed["version"] = None
        proposed["basis"] = "proposed"
        row = dict(proposed["supply"][0])
        recorded_qty = next(item["qty"] for item in data.read("SELECT qty FROM inventory WHERE hub_id=:h AND item=:i",
                                                              h=row["hub_id"], i=row["item"]))
        row["qty"] = int(recorded_qty) + 1
        duplicate = dict(row)
        duplicate["key"] = "supply|duplicate"
        unknown = dict(row)
        unknown["zone"] = "Nowhere"
        unknown["key"] = "supply|nowhere"
        proposed["supply"] = [row, duplicate, unknown]
        untouched = copy.deepcopy(proposed)
        reviewed = coordinator_agent.brief(origin="Ratnagiri", proposed_plan=proposed)
        invalid = coordinator_agent.brief(origin="Ratnagiri", proposed_plan="not-a-plan")
        assert proposed == untouched
        evaluation = reviewed["proposed_evaluation"]
        assert evaluation["state"] == "evaluated"
        assert evaluation["basis"] == "proposed"
        assert evaluation["approved"] is False
        assert evaluation["executed"] is False
        assert evaluation["plan"] == untouched
        assert evaluation["plan"]["version"] is None
        findings = evaluation["result"]["findings"]
        assert any(item["status"] == "confirmed" and item["area"] == "resources" and item["evidence"].get("hub_id") == row["hub_id"]
                   for item in findings)
        assert any(item["status"] == "confirmed" and "Duplicate" in item["why"] for item in findings)
        assert any(item["status"] == "missing" and item["evidence"].get("zone") == "Nowhere" for item in findings)
        assert reviewed["agents"]["plan_challenger"]["result"]["findings"] == omitted["agents"]["plan_challenger"]["result"]["findings"]
        assert reviewed["agents"]["ripple"]["result"]["primary"]["risk"] == omitted["agents"]["ripple"]["result"]["primary"]["risk"]
        assert "proposed_evaluation" not in reviewed["agents"]["whatif"]
        assert invalid["proposed_evaluation"]["state"] == "unavailable"
        assert invalid["proposed_evaluation"]["plan"] is None
        assert invalid["proposed_evaluation"]["result"] is None
        assert invalid["proposed_evaluation"]["approved"] is False
        assert invalid["proposed_evaluation"]["executed"] is False
        assert invalid["agents"]["plan_challenger"]["status"] == "ok"
        assert fingerprint() == before
    finally:
        remove_world(path)


def test_stored_reports_reach_ripple_without_changing_records():
    path = temp_world()
    try:
        before = fingerprint()
        reports = [
            {"id": 1, "zone": "Ratnagiri", "classification": "Flood", "severity": "High", "people": 40,
             "urgency": "HIGH", "priority": "HIGH", "validation_status": "valid", "created_at": "2026-11-07 12:00:00"},
            {"id": 2, "zone": "Ratnagiri", "classification": "Flood", "severity": "Critical", "people": 12,
             "urgency": "HIGH", "priority": "CRITICAL", "validation_status": "valid", "created_at": "2026-11-07 12:05:00"},
            {"id": 3, "zone": None, "classification": "Flood", "severity": "High", "people": None,
             "urgency": "HIGH", "priority": "HIGH", "validation_status": "valid", "created_at": "2026-11-07 12:06:00"},
        ]
        plain = coordinator_agent.brief(origin="Ratnagiri")
        briefed = coordinator_agent.brief(origin="Ratnagiri", reports=reports)
        direct = ripple_agent.analyse("Ratnagiri")
        ripple = briefed["agents"]["ripple"]["result"]
        assert plain["agents"]["ripple"]["result"]["recommendations"] == direct["recommendations"]
        assert ripple["recommendations"] == direct["recommendations"]
        assert ripple["primary"]["risk"] == direct["primary"]["risk"]
        assert "reported" not in plain["agents"]["ripple"]["result"]["primary"]
        assert [row["id"] for row in ripple["primary"]["reported"]] == [1, 2]
        assert ripple["primary"]["reported"][0]["severity"] == "High"
        assert ripple["primary"]["reported"][0]["people"] == 40
        assert ripple["primary"]["reported"][1]["severity"] == "Critical"
        raigad = next(zone for zone in ripple["secondary"] if zone["zone"] == "Raigad")
        assert raigad["reported"] == []
        assert [row["id"] for row in ripple["unlocated_reports"]] == [3]
        assert ripple["unlocated_reports"][0]["people"] is None
        waiting = coordinator_agent.brief(reports=reports)
        assert waiting["agents"]["ripple"]["status"] == "missing"
        assert waiting["agents"]["ripple"]["result"] is None
        assert [row["id"] for row in waiting["agents"]["ripple"]["unlocated_reports"]] == [3]
        assert [row["id"] for row in waiting["agents"]["ripple"]["reports_awaiting_origin"]] == [1, 2]
        assert fingerprint() == before
    finally:
        remove_world(path)


def test_evidence_classes_stay_separate_across_the_handoff():
    path = temp_world()
    try:
        before = fingerprint()
        reports = [
            {"id": 1, "zone": "Ratnagiri", "classification": "Flood", "severity": "High", "people": 40,
             "urgency": "HIGH", "priority": "HIGH", "validation_status": "valid", "created_at": "2026-11-07 12:00:00"},
            {"id": 4, "zone": "Palghar", "classification": "Flood", "severity": "Low", "people": None,
             "urgency": None, "priority": None, "validation_status": "incomplete", "created_at": "2026-11-07 12:05:00"},
        ]
        calls = {"analyse": 0}
        original_analyse = ripple_agent.analyse
        original_compare = whatif_agent.compare

        def counting(origin, reports=None):
            calls["analyse"] += 1
            return original_analyse(origin, reports)

        ripple_agent.analyse = counting
        try:
            collected = coordinator_agent.brief(origin="Ratnagiri", reports=reports)
        finally:
            ripple_agent.analyse = original_analyse
        assert calls["analyse"] == 1
        cascade = collected["agents"]["ripple"]["result"]
        assert cascade["primary"]["reported"][0]["basis"] == "reported"
        assert cascade["primary"]["reported"][0]["people"] == 40
        recorded_probability = original_analyse("Ratnagiri")["primary"]["risk"]["probability"]
        assert cascade["primary"]["risk"]["probability"] == recorded_probability
        outside = cascade["reports_outside_ring"][0]
        assert outside["basis"] == "reported"
        assert outside["people"] is None

        def refuse_analyse(*_args, **_kwargs):
            raise AssertionError("what-if ran the cascade again")

        ripple_agent.analyse = refuse_analyse
        try:
            compared = original_compare([{"kind": "road", "road_id": "R6", "status": "BLOCKED"}], ripple=cascade)
        finally:
            ripple_agent.analyse = original_analyse
        compared["marker"] = "existing-comparison"
        road = next(row for row in compared["roads"] if row["id"] == "R6")
        assert road["recorded_status"] == "OPEN"
        assert road["simulated_status"] == "BLOCKED"
        assert compared["ripple_context"]["basis"] == "inferred"
        assert compared["ripple_context"]["affected_zones"][0]["risk"]["probability"] == recorded_probability

        def refuse_compare(*_args, **_kwargs):
            raise AssertionError("coordinator reran the simulation")

        stored = data.get_plan()
        proposed = copy.deepcopy(stored)
        proposed["version"] = None
        proposed["supply"] = [dict(stored["supply"][0], qty=10**9)]
        untouched = copy.deepcopy(proposed)
        whatif_agent.compare = refuse_compare
        try:
            handed = coordinator_agent.brief(origin="Ratnagiri", reports=reports, simulation=compared, proposed_plan=proposed)
        finally:
            whatif_agent.compare = original_compare
        assert proposed == untouched
        assert handed["received_simulation"]["basis"] == "simulated"
        assert handed["received_simulation"]["result"]["marker"] == "existing-comparison"
        assert handed["received_simulation"]["result"]["ripple_context"]["basis"] == "inferred"
        assert handed["agents"]["ripple"]["result"]["primary"]["reported"][0]["basis"] == "reported"
        assert handed["agents"]["ripple"]["result"]["primary"]["risk"]["probability"] == recorded_probability
        stored_findings = handed["agents"]["plan_challenger"]["result"]["findings"]
        assert all("basis" not in item for item in stored_findings)
        evaluation = handed["proposed_evaluation"]
        assert evaluation["basis"] == "proposed"
        assert evaluation["state"] == "evaluated"
        assert evaluation["approved"] is False
        assert evaluation["executed"] is False
        assert any(item["area"] == "resources" and item["status"] == "confirmed" for item in evaluation["result"]["findings"])
        assert not any(item.get("why") == evaluation["result"]["findings"][0]["why"]
                       and item.get("area") == "resources" and "10" in str(item.get("why"))
                       for item in handed["agents"]["plan_challenger"]["result"]["findings"])
        palghar = coordinator_agent.brief(origin="Palghar")
        unranked = palghar["agents"]["ripple"]["result"]["recommendations"]["unranked"]
        palghar_score = next(row for row in unranked if row["zone"] == "Palghar")
        assert palghar_score["score"] is None
        assert "proposed_evaluation" not in palghar
        assert "received_simulation" not in palghar
        assert fingerprint() == before
    finally:
        ripple_agent.analyse = original_analyse
        whatif_agent.compare = original_compare
        remove_world(path)


def test_stored_citizen_reports_are_listed_without_new_records():
    path = temp_world()
    try:
        intake_agent.submit({"source": "citizen", "zone": "Ratnagiri", "people": "40", "disaster_type": "Flood",
                             "urgency": "HIGH", "description": "Water in houses", "synthetic": 0})
        intake_agent.submit({"source": "citizen", "zone": "Raigad", "people": "", "disaster_type": "Flood",
                             "urgency": "MEDIUM", "description": "People count was not given", "synthetic": 0})
        listed = intake_agent.list_reports()
        before = fingerprint()
        shown = coordinator_agent.brief(origin="Ratnagiri", reports=listed)
        empty = coordinator_agent.brief(reports=[])
        omitted = coordinator_agent.brief(origin="Ratnagiri")
        facts = shown["citizen_reports"]["reports"]
        assert shown["citizen_reports"]["status"] == "available"
        assert shown["citizen_reports"]["basis"] == "reported"
        assert facts == [ripple_agent.report_fact(row) for row in listed]
        assert len(facts) == 2
        assert all(row["basis"] == "reported" for row in facts)
        assert any(row["people"] is None for row in facts)
        assert any(row["people"] == 40 for row in facts)
        for row in facts:
            for key in ("id", "classification", "severity", "urgency", "people", "priority",
                        "validation_status", "created_at"):
                assert key in row
        attached = shown["agents"]["ripple"]["result"]["primary"]["reported"]
        assert [row["id"] for row in attached] == [row["id"] for row in facts if row["zone"] == "Ratnagiri"]
        assert shown["agents"]["ripple"]["result"]["primary"]["risk"] == omitted["agents"]["ripple"]["result"]["primary"]["risk"]
        assert empty["citizen_reports"] == {"status": "empty", "basis": "reported", "reports": []}
        assert empty["agents"]["ripple"]["status"] == "missing"
        assert "citizen_reports" not in omitted
        assert data.read("SELECT COUNT(*) AS n FROM citizen_reports")[0]["n"] == 2
        assert fingerprint() == before
    finally:
        remove_world(path)
