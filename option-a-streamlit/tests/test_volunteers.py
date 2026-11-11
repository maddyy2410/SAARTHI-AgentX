"""Volunteers page persistence. Uses a temporary database, never disaster.db."""

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


def test_team_task_assignment_dispatch_and_progress():
    previous, path = temp_world()
    try:
        incidents = incident_fingerprint()
        version = data.latest_version()
        team_id = data.register_ngo_team("Konkan Rescue", "A. Kulkarni", "Unit head", "Ratnagiri", "Rescue", 8)
        task_id = data.create_rescue_task("Evacuation at the creek", "Ratnagiri", "High", 40, "Creek road")
        team = data.list_ngo_teams()[0]
        task = data.list_rescue_tasks()[0]
        assert team["id"] == team_id
        assert team["status"] == "AVAILABLE"
        assert team["available"] == 8
        assert team["lead_role"] == "Unit head"
        assert task["id"] == task_id
        assert task["status"] == "PENDING"
        assert task["zone"] == "Ratnagiri"
        assert task["people"] == 40
        assert task["lat"] is not None and task["lon"] is not None

        with pytest.raises(data.ActionError):
            data.assign_rescue_team(task_id, team_id, 9)
        assert data.list_ngo_teams()[0]["available"] == 8
        assert data.list_rescue_assignments() == []

        data.assign_rescue_team(task_id, team_id, 5)
        assigned = data.list_rescue_tasks()[0]
        team = data.list_ngo_teams()[0]
        link = data.list_rescue_assignments()[0]
        assert assigned["status"] == "ASSIGNED"
        assert team["status"] == "AVAILABLE"
        assert team["available"] == 3
        assert link["status"] == "ASSIGNED"
        assert link["dispatched_at"] is None
        assert data.volunteer_dashboard()["deployed"] == 0

        with pytest.raises(data.ActionError):
            data.update_rescue_progress(task_id, "IN_PROGRESS")
        assert data.list_rescue_tasks()[0]["status"] == "ASSIGNED"

        data.dispatch_rescue_assignment(link["id"])
        dispatched = data.list_rescue_tasks()[0]
        team = data.list_ngo_teams()[0]
        link = data.list_rescue_assignments()[0]
        assert dispatched["status"] == "DISPATCHED"
        assert team["status"] == "DEPLOYED"
        assert link["status"] == "DISPATCHED"
        assert link["dispatched_at"]
        board = data.volunteer_dashboard()
        assert board["teams"] == 1
        assert board["volunteers"] == 8
        assert board["available"] == 3
        assert board["active_tasks"] == 1
        assert board["deployed"] == 1
        assert board["completed"] == 0

        data.update_rescue_progress(task_id, "IN_PROGRESS")
        data.update_rescue_progress(task_id, "RESCUED")
        data.update_rescue_progress(task_id, "COMPLETED")
        finished = data.list_rescue_tasks()[0]
        team = data.list_ngo_teams()[0]
        assert finished["status"] == "COMPLETED"
        assert team["status"] == "AVAILABLE"
        assert team["available"] == 8
        logged = [row["status"] for row in data.rescue_status_log(task_id)]
        assert logged == ["PENDING", "ASSIGNED", "DISPATCHED", "IN_PROGRESS", "RESCUED", "COMPLETED"]
        done = data.volunteer_dashboard()
        assert done["active_tasks"] == 0
        assert done["completed"] == 1
        assert done["deployed"] == 0
        assert data.latest_version() == version
        assert incident_fingerprint() == incidents
        marker = data.rescue_task_markers()[0]
        assert marker["id"] == task_id
        assert "Rescue tasks" not in ("Risk zones", "Roads", "Hospitals", "Supply hubs", "SOS requests", "Shipments")
    finally:
        restore_world(previous, path)


def test_missing_volunteer_tables_are_added_without_touching_incidents():
    previous, path = temp_world()
    try:
        before = incident_fingerprint()
        data.write("DROP TABLE IF EXISTS ngo_teams")
        data.register_ngo_team("Sindhudurg Care", "M. Patil", "NGO head", "Sindhudurg", "Shelter", 6)
        assert data.list_ngo_teams()[0]["name"] == "Sindhudurg Care"
        assert incident_fingerprint() == before
        assert data.read("SELECT COUNT(*) AS n FROM zones")[0]["n"] == 7
    finally:
        restore_world(previous, path)
