"""Volunteers page persistence. Uses a temporary database, never disaster.db.

Exercises the post-merge master Volunteers API:
register_volunteer_team → create_rescue_task → assign_volunteer_team →
dispatch_volunteer_assignment → update_rescue_progress.
"""

import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DISASTER_DB", "/tmp/saarthi-volunteer-placeholder.db")

import data  # noqa: E402


def temp_world():
    fd, path = tempfile.mkstemp(prefix="saarthi-vol-", suffix=".db")
    os.close(fd)
    os.remove(path)
    previous = data.DB_PATH
    data.DB_PATH = path
    data.init_db()
    return previous, path


def restore_world(previous, path):
    data.DB_PATH = previous
    if path and os.path.exists(path):
        os.remove(path)


def incident_fingerprint():
    return [(row["id"], row["status"], row["people"], row["zone"])
            for row in data.read("SELECT id, status, people, zone FROM incidents ORDER BY id")]


def _teams():
    return data.read("SELECT * FROM volunteer_teams ORDER BY id DESC")


def _tasks():
    return data.read("SELECT * FROM rescue_tasks ORDER BY id DESC")


def _assignments():
    return data.read("SELECT * FROM volunteer_assignments ORDER BY id DESC")


def _history(assignment_id):
    return data.read(
        "SELECT status FROM volunteer_status_history WHERE assignment_id=:a ORDER BY id",
        a=assignment_id,
    )


def test_team_task_assignment_dispatch_and_progress():
    previous, path = temp_world()
    try:
        incidents = incident_fingerprint()
        version = data.latest_version()
        team_id = data.register_volunteer_team(
            "Konkan Rescue", "A. Kulkarni", "Unit head", "Ratnagiri", "Rescue", 8)
        task_id = data.create_rescue_task(
            "Evacuation at the creek", "Ratnagiri", "High", 40, "Creek road")
        team = _teams()[0]
        task = _tasks()[0]
        assert team["id"] == team_id
        assert team["status"] == "AVAILABLE"
        assert team["capacity"] == 8
        assert team["head_role"] == "Unit head"
        assert team["team_name"] == "Konkan Rescue"
        assert task["id"] == task_id
        assert task["status"] == "OPEN"
        assert task["zone"] == "Ratnagiri"
        assert task["people"] == 40
        assert task["lat"] is not None and task["lon"] is not None

        with pytest.raises(data.ActionError):
            data.assign_volunteer_team(task_id, 99999)
        assert _teams()[0]["status"] == "AVAILABLE"
        assert _assignments() == []

        assignment_id = data.assign_volunteer_team(task_id, team_id)
        assigned = _tasks()[0]
        team = _teams()[0]
        link = _assignments()[0]
        assert link["id"] == assignment_id
        assert assigned["status"] == "ASSIGNED"
        assert team["status"] == "ASSIGNED"
        assert link["status"] == "ASSIGNED"
        assert link["volunteers"] == 8

        with pytest.raises(data.ActionError):
            data.update_rescue_progress(assignment_id, "IN_PROGRESS")
        assert _tasks()[0]["status"] == "ASSIGNED"

        data.dispatch_volunteer_assignment(assignment_id)
        dispatched = _tasks()[0]
        team = _teams()[0]
        link = _assignments()[0]
        assert dispatched["status"] == "DISPATCHED"
        assert team["status"] == "DEPLOYED"
        assert link["status"] == "DISPATCHED"

        data.update_rescue_progress(assignment_id, "IN_PROGRESS")
        data.update_rescue_progress(assignment_id, "RESCUED")
        data.update_rescue_progress(assignment_id, "COMPLETED")
        finished = _tasks()[0]
        team = _teams()[0]
        assert finished["status"] == "COMPLETED"
        assert team["status"] == "AVAILABLE"
        logged = [row["status"] for row in _history(assignment_id)]
        assert logged == ["ASSIGNED", "DISPATCHED", "IN_PROGRESS", "RESCUED", "COMPLETED"]
        assert data.latest_version() == version
        assert incident_fingerprint() == incidents
        markers = data.read(
            "SELECT id, title, zone, lat, lon, priority, people, status FROM rescue_tasks WHERE id=:i",
            i=task_id)
        assert markers[0]["id"] == task_id
        assert "Rescue tasks" not in (
            "Risk zones", "Roads", "Hospitals", "Supply hubs", "SOS requests", "Shipments")
    finally:
        restore_world(previous, path)


def test_missing_volunteer_tables_are_added_without_touching_incidents():
    previous, path = temp_world()
    try:
        before = incident_fingerprint()
        data.write("DROP TABLE IF EXISTS volunteer_teams")
        data.register_volunteer_team(
            "Sindhudurg Care", "M. Patil", "NGO head", "Sindhudurg", "Shelter", 6)
        assert _teams()[0]["team_name"] == "Sindhudurg Care"
        assert incident_fingerprint() == before
        assert data.read("SELECT COUNT(*) AS n FROM zones")[0]["n"] == 7
    finally:
        restore_world(previous, path)
