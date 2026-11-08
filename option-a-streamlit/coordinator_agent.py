"""Coordinator brief: a read-only collection of the four committed agents.

The brief keeps each agent's original result and names the source of every
finding. What-If runs only for a confirmed finding that already contains a
change What-If accepts. It does not log an incident, allocate a shipment, or
create a plan.
"""

import copy

import data
import intake_agent
import plan_challenger_agent
import ripple_agent
import whatif_agent

DISCLAIMER = (
    "This brief is advisory. It does not log an incident, allocate a shipment, "
    "dispatch responders, or create a plan. What-If values are hypothetical."
)
SOURCES = ("intake", "ripple", "plan_challenger", "whatif", "stored_plan", "records")


def brief(report=None, origin=None, reports=None, simulation=None, proposed_plan=None):
    """Collect one advisory brief. report, origin, reports, simulation and proposed_plan are optional.

    A missing zone is reported as missing information. One agent can fail
    while the other results stay in the brief. Stored citizen reports are
    attached by zone name and are not written as incidents. A simulation
    result from What-If is kept as received and is not calculated again.
    A proposed plan is reviewed by Plan Challenger and is not approved or stored.
    """
    out = {"disclaimer": DISCLAIMER, "reviewed_at": data.now(), "plan_version": None,
           "stored_plan": None, "records": None,
           "agents": {name: _section(name) for name in ("intake", "ripple", "plan_challenger", "whatif")},
           "findings": []}
    _read_state(out)
    intake_result = _collect_intake(out, report)
    _collect_stored_reports(out, reports)
    _collect_ripple(out, _origin_name(origin, intake_result), reports)
    _collect_challenger(out)
    _collect_whatif(out)
    _receive_simulation(out, simulation)
    _evaluate_proposed(out, proposed_plan)
    return out


def _section(source):
    return {"source": source, "status": "not_requested", "result": None, "error": None, "missing": []}


def _read_state(out):
    try:
        plan = data.get_plan()
    except Exception as exc:
        _fail(out, "stored_plan", exc)
        plan = None
    else:
        out["stored_plan"] = plan
        out["plan_version"] = plan.get("version")
    try:
        situation = data.situation()
    except Exception as exc:
        _fail(out, "records", exc)
        return
    out["records"] = {"source": "records", "situation": situation, "plan_version": out["plan_version"]}
    for row in situation.get("severity") or []:
        if row.get("score") is None or row.get("probability") is None or row.get("vulnerability") is None:
            _finding(out, "records", "missing",
                     f"Severity for {row.get('name')} is missing a recorded input. The missing input is not zero.",
                     {"zone": row.get("name")})


def _collect_stored_reports(out, reports):
    """Keep the stored report rows already retrieved. Nothing is written or scored again."""
    if reports is None:
        return
    if isinstance(reports, (str, bytes)) or not hasattr(reports, "__iter__"):
        out["citizen_reports"] = {"status": "unavailable", "basis": "reported", "reports": None}
        return
    facts = []
    for row in reports:
        if not isinstance(row, dict):
            continue
        facts.append(ripple_agent.report_fact(row))
    out["citizen_reports"] = {
        "status": "empty" if not facts else "available",
        "basis": "reported",
        "reports": facts,
    }


def _collect_intake(out, report):
    section = out["agents"]["intake"]
    if report is None:
        return None
    try:
        if not isinstance(report, dict):
            raise TypeError("A citizen report must be a dictionary.")
        zones = data.read("SELECT name, lat, lon, vulnerability FROM zones")
        existing = data.read("SELECT * FROM citizen_reports ORDER BY priority_score DESC, id DESC")
        result = intake_agent.evaluate(report, zones, existing)
    except Exception as exc:
        _fail(out, "intake", exc)
        return None
    section["status"] = "ok"
    section["result"] = result
    if result.get("people") is None:
        section["missing"].append("people")
        _finding(out, "intake", "missing",
                 "People affected is missing. The Coordinator does not read that missing count as zero.",
                 {"zone": result.get("zone")})
    if result.get("urgency") is None:
        section["missing"].append("urgency")
        _finding(out, "intake", "missing",
                 "Urgency is missing. Missing urgency is not LOW, and it is not a recorded urgency of zero.",
                 {"zone": result.get("zone")})
    if not result.get("zone"):
        section["missing"].append("zone")
        _finding(out, "intake", "missing",
                 "No monitored zone is recorded for this report. Cascade analysis needs a zone name.",
                 {})
    return result


def _collect_ripple(out, origin, reports=None):
    section = out["agents"]["ripple"]
    if not origin:
        section["status"] = "missing"
        section["missing"].append("zone")
        _finding(out, "ripple", "missing",
                 "No recorded zone was available, so cascade analysis was not run. This is missing information, not a score of zero.",
                 {})
        if reports is not None:
            names = [row["name"] for row in data.read("SELECT name FROM zones")]
            located, unlocated = ripple_agent.classify_reports(reports, names)
            section["unlocated_reports"] = unlocated
            section["reports_awaiting_origin"] = [fact for facts in located.values() for fact in facts]
        return
    try:
        result = ripple_agent.analyse(origin, reports)
    except Exception as exc:
        _fail(out, "ripple", exc, {"zone": origin})
        return
    section["status"] = "ok"
    section["result"] = result
    recommendations = result.get("recommendations") or {}
    unranked = recommendations.get("unranked") if isinstance(recommendations, dict) else []
    for row in unranked or []:
        section["missing"].append(row.get("zone"))
        _finding(out, "ripple", "missing",
                 f"Attention score for {row.get('zone')} is missing ({', '.join(row.get('missing') or [])}). "
                 "The missing score is not zero.",
                 {"zone": row.get("zone")})
    for row in result.get("stock") or []:
        if row.get("state") == "missing" or row.get("qty") is None:
            _finding(out, "ripple", "missing",
                     f"{row.get('text') or 'No stock row recorded'} at {row.get('hub')}. A missing stock row is not zero.",
                     {"hub": row.get("hub"), "hub_id": row.get("hub_id"), "item": row.get("item")})
        elif row.get("state") == "zero":
            _finding(out, "ripple", "recorded_zero",
                     f"Recorded stock is 0 {row.get('item')} at {row.get('hub')}. This zero is stored, not a missing row.",
                     {"hub": row.get("hub"), "hub_id": row.get("hub_id"), "item": row.get("item")})


def _collect_challenger(out):
    section = out["agents"]["plan_challenger"]
    try:
        result = plan_challenger_agent.review()
    except Exception as exc:
        _fail(out, "plan_challenger", exc)
        return
    section["status"] = "ok"
    section["result"] = result
    for finding in result.get("findings") or []:
        evidence = dict(finding.get("evidence") or {})
        _finding(out, "plan_challenger", finding.get("status") or "missing", finding.get("why"), evidence)


def _collect_whatif(out):
    """Turn confirmed findings into one hypothetical comparison, when the values are already present."""
    section = out["agents"]["whatif"]
    section["hypothetical"] = True
    section["modifications"] = []
    section["skipped"] = []
    challenger = out["agents"]["plan_challenger"]
    if challenger["status"] != "ok" or not isinstance(challenger.get("result"), dict):
        section["status"] = "not_requested"
        _finding(out, "whatif", "missing",
                 "Plan Challenger did not return a review, so no hypothetical change was created.", {})
        return
    try:
        roads, hospitals, inventory = _recorded_limits(out)
    except Exception as exc:
        _fail(out, "whatif", exc)
        return
    modifications = []
    for finding in challenger["result"].get("findings") or []:
        if finding.get("status") != "confirmed":
            continue
        modification, reason = _whatif_modification(finding, roads, hospitals, inventory)
        evidence = finding.get("evidence") or {}
        if modification is None:
            section["skipped"].append({"reason": reason, "evidence": evidence, "area": finding.get("area")})
            _finding(out, "whatif", "not_simulated", reason, evidence)
            continue
        modifications.append(modification)
    section["modifications"] = modifications
    if not modifications:
        section["status"] = "skipped" if section["skipped"] else "not_requested"
        return
    try:
        ripple_section = out["agents"]["ripple"]
        ripple_result = ripple_section.get("result") if ripple_section.get("status") == "ok" else None
        if ripple_result is None:
            result = whatif_agent.compare([dict(item) for item in modifications])
        else:
            result = whatif_agent.compare([dict(item) for item in modifications], ripple=ripple_result)
    except Exception as exc:
        _fail(out, "whatif", exc)
        return
    section["status"] = "ok"
    section["result"] = result
    section["label"] = "hypothetical"
    _finding(out, "whatif", "hypothetical",
             "What-If compared an in-memory copy. Simulated values are hypothetical and the recorded rows were left unchanged.",
             {})


def _recorded_limits(out):
    situation = (out.get("records") or {}).get("situation") or {}
    roads = situation.get("roads") or data.read("SELECT * FROM roads")
    hospitals = situation.get("hospitals") or data.read("SELECT * FROM hospitals")
    inventory = situation.get("inventory") or data.read("SELECT * FROM inventory")
    return roads, hospitals, inventory


def _whatif_modification(finding, roads, hospitals, inventory):
    """Return one accepted modification, or a reason that no value was invented."""
    area = finding.get("area")
    evidence = finding.get("evidence") or {}
    if area == "roads":
        road_id = evidence.get("road")
        if not road_id:
            return None, "Confirmed road finding has no road identifier. No road was invented."
        if not any(road["id"] == road_id for road in roads):
            return None, f"Road {road_id} is not in the recorded roads, so it was not simulated."
        return {"kind": "road", "road_id": road_id, "status": "BLOCKED"}, None
    if area == "hospital":
        hospital_id = evidence.get("hospital_id")
        if hospital_id is None:
            return None, "Confirmed hospital finding has no hospital id. No hospital was invented."
        hospital = next((row for row in hospitals if row["id"] == hospital_id), None)
        if hospital is None:
            return None, f"Hospital id {hospital_id} is not recorded. No bed count was invented."
        beds = evidence.get("available_beds")
        if isinstance(beds, bool) or not isinstance(beds, int):
            return None, "Confirmed hospital finding does not include a whole-number free-bed quantity. No bed count was invented."
        total = hospital.get("total_beds")
        if total is None:
            return None, f"{hospital.get('name')} has no recorded bed total, so free beds were not simulated."
        if beds < 0 or beds > int(total):
            return None, (f"Free beds {beds} are outside the recorded total of {int(total)}. "
                          "The quantity was not changed to fit.")
        return {"kind": "beds", "hospital_id": hospital_id, "available_beds": beds}, None
    if area == "resources" and evidence.get("hub_id") is not None and evidence.get("item"):
        hub_id = evidence["hub_id"]
        item = evidence["item"]
        qty = evidence.get("qty")
        if isinstance(qty, bool) or not isinstance(qty, int):
            return None, "Confirmed stock finding does not include a whole-number quantity. No stock quantity was invented."
        recorded = next((row for row in inventory if row["hub_id"] == hub_id and row["item"] == item), None)
        if recorded is None or recorded.get("qty") is None:
            return None, "No recorded stock row matches this hub and item. Missing stock was not treated as zero."
        recorded_qty = int(recorded["qty"])
        if qty < 0 or qty > recorded_qty:
            return None, (f"Hypothetical stock must be from 0 through the recorded quantity of {recorded_qty}. "
                          f"{qty} was not simulated.")
        return {"kind": "stock", "hub_id": hub_id, "item": item, "qty": qty}, None
    return None, "This confirmed finding is not a road status, free-bed count, or hub stock quantity that the hypothetical comparison can change."


def _receive_simulation(out, simulation):
    """Keep a What-If comparison that was already produced. Nothing is recalculated or stored."""
    if simulation is None:
        return
    if not isinstance(simulation, dict):
        out["received_simulation"] = {"status": "unavailable", "result": None, "basis": "simulated"}
        return
    try:
        received = copy.deepcopy(simulation)
    except Exception:
        out["received_simulation"] = {"status": "unavailable", "result": None, "basis": "simulated"}
        return
    out["received_simulation"] = {"status": "received", "result": received, "basis": "simulated"}


def _evaluate_proposed(out, proposed_plan):
    """Review a plan the caller already built. The plan is not rewritten, approved, or stored."""
    if proposed_plan is None:
        return
    unavailable = {"state": "unavailable", "basis": "proposed", "plan": None, "result": None,
                   "approved": False, "executed": False}
    if not isinstance(proposed_plan, dict):
        out["proposed_evaluation"] = unavailable
        return
    try:
        held = copy.deepcopy(proposed_plan)
    except Exception:
        out["proposed_evaluation"] = unavailable
        return
    try:
        result = plan_challenger_agent.review(held, plan_challenger_agent.load_snapshot())
    except Exception as exc:
        out["proposed_evaluation"] = {
            "state": "proposed", "basis": "proposed", "plan": held, "result": None,
            "error": f"{type(exc).__name__}: {exc}", "approved": False, "executed": False,
        }
        return
    out["proposed_evaluation"] = {
        "state": "evaluated", "basis": "proposed", "plan": held, "result": result,
        "approved": False, "executed": False,
    }


def _origin_name(origin, intake_result):
    if origin is not None:
        text = str(origin).strip()
        return text or None
    if isinstance(intake_result, dict) and intake_result.get("zone"):
        return intake_result["zone"]
    return None


def _fail(out, source, exc, evidence=None):
    message = f"{type(exc).__name__}: {exc}"
    if source in out["agents"]:
        out["agents"][source]["status"] = "failed"
        out["agents"][source]["error"] = message
    _finding(out, source, "agent_failure", message, evidence or {})


def _finding(out, source, kind, detail, evidence):
    clean = {key: value for key, value in (evidence or {}).items() if value is not None}
    out["findings"].append({"source": source, "kind": kind, "detail": detail, "evidence": clean})
