"""Tests for the Intake and Triage agent.

These use the built-in rule scorer and a temporary database. They do not
require OR-Tools or Streamlit.
"""

import os
import sqlite3
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DISASTER_DB", "/tmp/saarthi-intake-placeholder.db")

import data  # noqa: E402
import demo_data  # noqa: E402
import intake_agent  # noqa: E402

ZONES = [{"name": name, "lat": lat, "lon": lon, "vulnerability": vul}
         for name, lat, lon, _pop, vul in demo_data.ZONES]


def temp_world():
    fd, path = tempfile.mkstemp(prefix="saarthi-intake-", suffix=".db")
    os.close(fd)
    os.remove(path)
    data.DB_PATH = path
    data.init_db()
    return path


def remove_world(path):
    if path and os.path.exists(path):
        os.remove(path)


def flood(people=80, urgency="HIGH", **extra):
    report = {"zone": "Raigad", "lat": 18.6414, "lon": 72.8722, "disaster_type": "Flood",
              "people": people, "urgency": urgency,
              "description": "Rising flood water has entered houses in Raigad."}
    report.update(extra)
    return report


def test_valid_flood_is_explained():
    result = intake_agent.evaluate(flood(), ZONES)
    assert result["classification"] == "Flood"
    assert result["validation_status"] == "valid"
    assert result["needs_review"] is False
    assert result["priority"] in intake_agent.PRIORITY_LEVELS
    assert "Priority" in result["priority_reason"]
    assert "not an AI prediction or a disaster forecast" in result["priority_reason"]
    assert "Zone Raigad vulnerability 0.80" in result["priority_reason"]
    assert "Urgency: HIGH. Contribution: 24." in result["priority_reason"]
    assert result["components"]["urgency"] == 24
    assert result["components"]["severity"] == 16


def test_disaster_categories():
    samples = {
        "Flood": "The street is flooding.",
        "Heatwave": "This is a heatwave and people have heatstroke.",
        "Cyclone": "A cyclone is approaching the coast.",
        "Earthquake": "The earthquake cracked the road.",
        "Fire": "A fire is burning in the lane.",
    }
    for category, description in samples.items():
        result = intake_agent.evaluate({"zone": "Mumbai", "people": 4, "urgency": "LOW", "description": description,
                                        "lat": 19.076, "lon": 72.878}, ZONES)
        assert result["classification"] == category, description


def test_missing_information_is_incomplete():
    result = intake_agent.evaluate({"description": "Water is rising near the road."}, ZONES)
    assert result["validation_status"] == "incomplete"
    assert result["needs_review"] is True
    assert any("Location is missing" in note for note in result["validation_notes"])
    assert any("people affected is missing" in note for note in result["validation_notes"])
    assert any("Urgency is missing" in note for note in result["validation_notes"])
    assert result["classification"] == "Unclassified"


def test_invalid_values_and_edges():
    bad_people = intake_agent.evaluate(flood(people="many"), ZONES)
    assert bad_people["validation_status"] == "invalid"
    bad_coord = intake_agent.evaluate(flood(lat=95, lon=72.8), ZONES)
    assert bad_coord["validation_status"] == "invalid"
    partial = intake_agent.evaluate(flood(lat="", lon=None), ZONES)
    # blank coordinates with a known zone are still located
    assert partial["validation_status"] == "valid"
    one_coord = intake_agent.evaluate(flood(lat=18.64, lon=""), ZONES)
    assert any("Only one coordinate" in note for note in one_coord["validation_notes"])
    huge = intake_agent.evaluate(flood(people=250000), ZONES)
    assert huge["needs_review"] is True
    assert huge["components"]["people"] == 30
    long_text = "flood " + ("word " * 1000)
    clipped = intake_agent.evaluate(flood(description=long_text), ZONES)
    assert len(clipped["description"]) == 2000
    assert intake_agent.parse_people(True)[1] == "invalid"
    assert intake_agent.parse_people(3.5)[1] == "invalid"
    assert intake_agent.parse_people("12")[0] == 12
    assert intake_agent.parse_urgency("ASAP")[1] == "invalid"


def test_inconsistent_place_and_description():
    result = intake_agent.evaluate({
        "zone": "Mumbai", "lat": 18.5204, "lon": 73.8567, "disaster_type": "Flood", "people": 12,
        "urgency": "HIGH", "description": "Buildings collapsed after the earthquake.",
    }, ZONES)
    assert result["validation_status"] == "inconsistent"
    assert result["classification"] == "Flood"
    assert any("earthquake" in note.lower() or "Earthquake" in note for note in result["validation_notes"])
    assert any("km from Mumbai" in note for note in result["validation_notes"])
    unknown = intake_agent.evaluate(flood(zone="Atlantis"), ZONES)
    assert any("not one of the monitored zones" in note for note in unknown["validation_notes"])


def test_duplicates_and_rejected_reports():
    first = intake_agent.evaluate(flood(), ZONES)
    second = intake_agent.evaluate(flood(people=84, lat=18.65, lon=72.88, description=flood()["description"] + " again"),
                                   ZONES, existing=[{"id": 7, "review_status": "pending", **first}])
    assert second["validation_status"] == "duplicate"
    assert second["duplicate_of"] == 7
    different = intake_agent.evaluate({
        "zone": "Mumbai", "lat": 19.08, "lon": 72.88, "disaster_type": "Fire", "people": 10, "urgency": "HIGH",
        "description": "A fire is burning in a shop.",
    }, ZONES, existing=[{"id": 7, "review_status": "pending", **first}])
    assert different["duplicate_of"] is None
    nearby = intake_agent.evaluate({
        "zone": "Mumbai", "lat": 19.08, "lon": 72.88, "disaster_type": "Fire", "people": 10, "urgency": "LOW",
        "description": "Warehouse burning.",
    }, ZONES, existing=[{"id": 3, "review_status": "pending", "classification": "Fire", "zone": "Pune",
                         "people": 10, "lat": 19.081, "lon": 72.881, "description": "Market blaze."}])
    assert nearby["duplicate_of"] == 3
    after_reject = intake_agent.evaluate(flood(people=84), ZONES, existing=[{"id": 7, "review_status": "rejected", **first}])
    assert after_reject["duplicate_of"] is None
    same_unknown = intake_agent.evaluate({"zone": "Mumbai", "people": 1, "urgency": "LOW", "lat": 19.07, "lon": 72.87,
                                          "description": "Something happened."}, ZONES,
                                         existing=[{"id": 4, "review_status": "pending", "classification": "Unclassified",
                                                    "zone": "Mumbai", "people": 1, "description": "Something happened."}])
    assert same_unknown["duplicate_of"] == 4


def test_priority_uses_urgency_people_and_vulnerability():
    low = intake_agent.evaluate(flood(people=5, urgency="LOW", zone="Pune", lat=18.5204, lon=73.8567), ZONES)
    high = intake_agent.evaluate(flood(people=200, urgency="CRITICAL"), ZONES)
    assert high["priority_score"] > low["priority_score"]
    assert high["components"]["urgency"] > low["components"]["urgency"]
    assert high["components"]["people"] > low["components"]["people"]
    assert high["components"]["vulnerability"] > low["components"]["vulnerability"]
    assert intake_agent.assess_severity("Flood", 100)[0] == "Critical"
    assert intake_agent.assess_severity("Heatwave", 10)[0] == "Moderate"
    assert intake_agent.score_breakdown("MEDIUM", 10, "Low", 0.5)["score"] == 25
    assert intake_agent.score_breakdown("MEDIUM", 10, "Low", 0.5)["level"] == "MEDIUM"
    assert intake_agent.score_breakdown("MEDIUM", 0, "Low", 0.5)["level"] == "LOW"
    assert intake_agent.score_breakdown("HIGH", 0, "High", 0.75)["level"] == "MEDIUM"
    assert intake_agent.score_breakdown("HIGH", 0, "High", 10 / 12)["level"] == "HIGH"
    assert intake_agent.score_breakdown("CRITICAL", 40, "Critical", 1)["level"] == "HIGH"
    assert intake_agent.score_breakdown("CRITICAL", 50, "Critical", 1)["level"] == "CRITICAL"
    assert intake_agent.priority_level(0) == "LOW"
    assert intake_agent.priority_level(100) == "CRITICAL"


def test_database_queue_review_and_migration():
    path = temp_world()
    try:
        before = data.read("SELECT COUNT(*) AS n FROM incidents")[0]["n"]
        saved = intake_agent.submit(flood())
        assert data.read("SELECT COUNT(*) AS n FROM incidents")[0]["n"] == before
        assert saved["review_status"] == "pending"
        duplicate = intake_agent.submit(flood(people=82, description=flood()["description"] + " again"))
        assert duplicate["validation_status"] == "duplicate"
        rejected = intake_agent.review_report(saved["id"], "rejected", "Duty officer")
        assert rejected["review_status"] == "rejected"
        try:
            intake_agent.review_report(saved["id"], "approved", "Duty officer")
        except data.ActionError:
            pass
        else:
            raise AssertionError("a reviewed report should not be reviewed again")
        fresh = intake_agent.submit({"zone": "Mumbai", "lat": 19.076, "lon": 72.878, "disaster_type": "Fire",
                                     "people": 120, "urgency": "CRITICAL", "description": "A fire is burning in Mumbai."})
        logged = intake_agent.approve_and_log_sos(fresh["id"], "Duty officer")
        assert logged["review_status"] == "approved"
        assert logged["incident_id"]
        assert data.read("SELECT COUNT(*) AS n FROM incidents")[0]["n"] == before + 1
        imported = intake_agent.import_open_sos()
        assert imported
        assert intake_agent.import_open_sos() == []
        examples = intake_agent.load_synthetic_examples()
        assert examples
        assert all(row["synthetic"] == 1 for row in examples)
        assert intake_agent.load_synthetic_examples() == []
    finally:
        remove_world(path)


def test_existing_rows_survive_intake_migration():
    fd, path = tempfile.mkstemp(prefix="saarthi-migrate-", suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE zones(name TEXT PRIMARY KEY, lat REAL, lon REAL, population INTEGER, vulnerability REAL)")
    conn.execute("INSERT INTO zones VALUES ('Keepme', 19.0, 72.8, 10, 0.4)")
    conn.commit()
    conn.close()
    data.DB_PATH = path
    try:
        data.init_db()
        assert data.read("SELECT name, population FROM zones") == [{"name": "Keepme", "population": 10}]
        assert data.read("SELECT COUNT(*) AS n FROM citizen_reports")[0]["n"] == 0
        assert data.read("SELECT COUNT(*) AS n FROM incidents")[0]["n"] == 0
    finally:
        remove_world(path)


def test_missing_urgency_is_not_low_and_logging_is_once():
    missing = intake_agent.evaluate({"source": "existing_sos", "zone": "Raigad", "lat": 18.6414, "lon": 72.8722,
                                     "disaster_type": "Flood", "people": 40, "description": "Existing flood SOS."}, ZONES)
    low = intake_agent.evaluate(flood(people=40, urgency="LOW"), ZONES)
    assert missing["urgency"] is None
    assert missing["components"]["urgency"] == 0
    assert low["urgency"] == "LOW"
    assert low["components"]["urgency"] == 6
    assert missing["priority_score"] < low["priority_score"]
    assert "Urgency: Not recorded in original SOS." in missing["priority_reason"]
    assert "not treated as LOW" in missing["priority_reason"]
    assert "Unavailable information: urgency." in missing["priority_reason"]
    assert "People affected: 40. Contribution:" in missing["priority_reason"]
    assert "Severity contribution:" in missing["priority_reason"]
    assert "Zone Raigad vulnerability" in missing["priority_reason"]
    assert intake_agent.urgency_label("existing_sos", None) == "Not recorded in original SOS"
    assert intake_agent.urgency_label("citizen", None) == "Not provided"
    assert intake_agent.urgency_label("citizen", "LOW") == "LOW"
    assert intake_agent.citizen_form_error("Not specified")
    assert intake_agent.citizen_form_error("LOW") is None
    blank = intake_agent.evaluate(flood(urgency="Not specified"), ZONES)
    assert blank["urgency"] is None
    assert blank["validation_status"] == "incomplete"
    assert blank["components"]["urgency"] == 0

    path = temp_world()
    try:
        imported = intake_agent.import_open_sos()
        assert imported
        assert all(row["urgency"] is None for row in imported)
        assert all("Urgency: Not recorded in original SOS." in row["priority_reason"] for row in imported)
        before = data.read("SELECT COUNT(*) AS n FROM incidents")[0]["n"]
        saved = intake_agent.submit(flood())
        assert intake_agent.queue_state(saved) == "Pending"
        approved = intake_agent.review_report(saved["id"], "approved", "Duty officer")
        assert intake_agent.queue_state(approved) == "Approved, not logged"
        assert approved["incident_id"] is None
        logged = intake_agent.approve_and_log_sos(saved["id"], "Duty officer")
        assert logged["incident_id"]
        assert intake_agent.queue_state(logged) == f"Logged as field request #{logged['incident_id']}"
        field = data.read("SELECT * FROM incidents WHERE id=:i", i=logged["incident_id"])[0]
        assert field["status"] == "OPEN"
        assert field["zone"] == "Raigad"
        assert field["people"] == 80
        assert data.incident_ids(data.read("SELECT * FROM incidents"), "OPEN")
        assert logged["incident_id"] in data.incident_ids(data.read("SELECT * FROM incidents"), "OPEN")
        assert data.read("SELECT COUNT(*) AS n FROM incidents")[0]["n"] == before + 1
        try:
            intake_agent.approve_and_log_sos(saved["id"], "Duty officer")
        except data.ActionError as exc:
            assert "No second request was created" in str(exc)
        else:
            raise AssertionError("logging the same report twice should be refused")
        assert data.read("SELECT COUNT(*) AS n FROM incidents")[0]["n"] == before + 1
        assert data.read("SELECT incident_id FROM citizen_reports WHERE id=:i", i=saved["id"])[0]["incident_id"] == logged["incident_id"]
        original_ids = {row["id"] for row in data.read("SELECT id FROM incidents")}
        copied = imported[0]
        confirmed = intake_agent.approve_and_log_sos(copied["id"], "Duty officer")
        assert confirmed["sos_logged"] == 1
        assert confirmed["incident_id"] == copied["incident_id"]
        assert confirmed["incident_id"] in data.incident_ids(data.read("SELECT * FROM incidents"), "OPEN")
        assert {row["id"] for row in data.read("SELECT id FROM incidents")} == original_ids
        try:
            intake_agent.approve_and_log_sos(copied["id"], "Duty officer")
        except data.ActionError as exc:
            assert "No second request was created" in str(exc)
        else:
            raise AssertionError("confirming an imported SOS twice should be refused")
        assert {row["id"] for row in data.read("SELECT id FROM incidents")} == original_ids
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
