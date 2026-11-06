"""Intake and Triage agent.

Rule-based classification, validation and priority for citizen reports and
existing SOS rows. This does not predict disasters, send alerts, or dispatch
responders. A future Coordinator can call run() without saving, or submit()
to store a report for a human operator.

Database writes go through data.py. Existing incident, zone and plan tables
are not replaced.
"""

import math
import re

import data

INTAKE_SCHEMA = """
CREATE TABLE IF NOT EXISTS citizen_reports(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT,
    source TEXT,
    incident_id INTEGER,
    lat REAL,
    lon REAL,
    zone TEXT,
    disaster_type TEXT,
    people INTEGER,
    urgency TEXT,
    description TEXT,
    classification TEXT,
    severity TEXT,
    priority TEXT,
    priority_score REAL,
    priority_reason TEXT,
    validation_status TEXT,
    validation_notes TEXT,
    needs_review INTEGER,
    review_status TEXT,
    reviewed_by TEXT,
    duplicate_of INTEGER,
    synthetic INTEGER,
    sos_logged INTEGER DEFAULT 0,
    score_urgency REAL,
    score_people REAL,
    score_severity REAL,
    score_vulnerability REAL
);
"""

CATEGORIES = ("Flood", "Heatwave", "Cyclone", "Earthquake", "Fire", "Landslide")
PRIORITY_LEVELS = ("CRITICAL", "HIGH", "MEDIUM", "LOW")
URGENCY_POINTS = {"CRITICAL": 36, "HIGH": 24, "MEDIUM": 14, "LOW": 6}
SEVERITY_POINTS = {"Critical": 22, "High": 16, "Moderate": 10, "Low": 4}
SEVERITY_ORDER = ("Low", "Moderate", "High", "Critical")
CATEGORY_BASE = {
    "Flood": "High", "Cyclone": "High", "Earthquake": "High", "Fire": "High",
    "Heatwave": "Moderate", "Landslide": "Moderate", "Unclassified": "Low",
}
CATEGORY_WORDS = (
    ("Flood", ("flood", "floods", "flooding", "flooded", "inundation", "waterlogging")),
    ("Heatwave", ("heatwave", "heat wave", "heatstroke", "extreme heat")),
    ("Cyclone", ("cyclone", "cyclonic", "hurricane")),
    ("Earthquake", ("earthquake", "quake", "tremor")),
    ("Fire", ("fire", "fires", "wildfire", "blaze")),
    ("Landslide", ("landslide", "landslip")),
)
URGENCY_WORDS = {
    "CRITICAL": ("critical", "immediate", "life-threatening", "life threatening"),
    "HIGH": ("high", "urgent"),
    "MEDIUM": ("medium", "moderate"),
    "LOW": ("low", "minor"),
}
ZONE_MISMATCH_KM = 80
DUPLICATE_KM = 5
STOPWORDS = {"synthetic", "demo", "existing", "the", "a", "an", "of", "for", "and", "in", "on", "to", "is", "are"}

# Labelled practice reports. Nothing here is a live feed.
SYNTHETIC_EXAMPLES = (
    {"zone": "Raigad", "lat": 18.6414, "lon": 72.8722, "disaster_type": "Flood", "people": 80,
     "urgency": "HIGH", "description": "[SYNTHETIC DEMO] Rising flood water has entered houses in Raigad. Families need dry ground."},
    {"zone": "Raigad", "lat": 18.6500, "lon": 72.8800, "disaster_type": "Flood", "people": 84,
     "urgency": "HIGH", "description": "[SYNTHETIC DEMO] Rising flood water has entered houses in Raigad. Families need dry ground again."},
    {"zone": "Pune", "lat": 18.5204, "lon": 73.8567, "disaster_type": "Heatwave", "people": 30,
     "urgency": "MEDIUM", "description": "[SYNTHETIC DEMO] Extreme heat in Pune. Outdoor workers are fainting. This is not a live weather reading."},
    {"zone": "Mumbai", "lat": 19.0760, "lon": 72.8777, "disaster_type": "Fire", "people": 120,
     "urgency": "CRITICAL", "description": "[SYNTHETIC DEMO] A fire is burning in a Mumbai neighbourhood. People are moving away from the smoke."},
    {"zone": "Sindhudurg", "lat": 16.3500, "lon": 73.5500, "disaster_type": "Cyclone", "people": 60,
     "urgency": "HIGH", "description": "[SYNTHETIC DEMO] Cyclonic wind has damaged roofs in Sindhudurg. Practice report only."},
    {"zone": "Mumbai", "lat": 18.5204, "lon": 73.8567, "disaster_type": "Flood", "people": 12,
     "urgency": "HIGH", "description": "[SYNTHETIC DEMO] Buildings collapsed after the earthquake. The stated zone does not match these coordinates."},
    {"description": "[SYNTHETIC DEMO] Water is rising near the road. Location and the number of people were not provided."},
)


def ensure_schema():
    """Add the intake table and any new score columns. Existing rows stay in place."""
    data.write(INTAKE_SCHEMA)
    have = {row["name"] for row in data.read("PRAGMA table_info(citizen_reports)")}
    for name, kind in (("score_urgency", "REAL"), ("score_people", "REAL"), ("score_severity", "REAL"),
                       ("score_vulnerability", "REAL"), ("sos_logged", "INTEGER DEFAULT 0")):
        if name not in have:
            data.write(f"ALTER TABLE citizen_reports ADD COLUMN {name} {kind}")
    data.write("UPDATE citizen_reports SET sos_logged=0 WHERE sos_logged IS NULL")
    data.write("""UPDATE citizen_reports SET sos_logged=1
                  WHERE source != 'existing_sos' AND incident_id IS NOT NULL AND review_status='approved'""")
    _backfill_explanations()


def _backfill_explanations():
    """Fill score columns on reports saved before those columns existed. Decisions are kept."""
    rows = data.read("SELECT * FROM citizen_reports WHERE score_urgency IS NULL")
    if not rows:
        return
    zones = data.read("SELECT name, vulnerability FROM zones")
    for row in rows:
        zone_row = next((z for z in zones if z["name"] == row["zone"]), None)
        known = zone_row is not None and zone_row.get("vulnerability") is not None
        vulnerability = float(zone_row["vulnerability"]) if known else 0.5
        severity, severity_reason = assess_severity(row["classification"] or "Unclassified", row["people"])
        parts = score_breakdown(row["urgency"], row["people"], severity, vulnerability)
        reason = explain_priority(parts, severity_reason, row["zone"], vulnerability, known,
                                  row["urgency"], row["people"], row["source"] or "citizen")
        data.write("""UPDATE citizen_reports SET score_urgency=:u, score_people=:p, score_severity=:s,
                      score_vulnerability=:v, priority_reason=:reason, priority=:level, priority_score=:score,
                      severity=:severity WHERE id=:i""",
                   u=parts["urgency"], p=parts["people"], s=parts["severity"], v=parts["vulnerability"],
                   reason=reason, level=parts["level"], score=parts["score"], severity=severity, i=row["id"])


def monitored_zones():
    ensure_schema()
    return data.read("SELECT name, lat, lon, vulnerability FROM zones")


def list_reports():
    ensure_schema()
    return data.read("SELECT * FROM citizen_reports ORDER BY priority_score DESC, id DESC")


def run(report):
    """Classify, validate and score one report. Does not write to the database."""
    return evaluate(report, monitored_zones(), list_reports())


def submit(report):
    """Store one triaged report for human review. Does not create an SOS by itself."""
    result = run(report)
    report_id = data.write(
        """INSERT INTO citizen_reports(created_at, source, incident_id, lat, lon, zone, disaster_type, people,
           urgency, description, classification, severity, priority, priority_score, priority_reason,
           validation_status, validation_notes, needs_review, review_status, duplicate_of, synthetic, sos_logged,
           score_urgency, score_people, score_severity, score_vulnerability)
           VALUES (:created, :source, :incident_id, :lat, :lon, :zone, :disaster_type, :people, :urgency,
           :description, :classification, :severity, :priority, :score, :reason, :status, :notes,
           :review_flag, 'pending', :duplicate_of, :synthetic, 0, :score_urgency, :score_people,
           :score_severity, :score_vulnerability)""",
        created=data.now(), source=result["source"], incident_id=result["incident_id"], lat=result["lat"],
        lon=result["lon"], zone=result["zone"], disaster_type=result["stated_type"], people=result["people"],
        urgency=result["urgency"], description=result["description"], classification=result["classification"],
        severity=result["severity"], priority=result["priority"], score=result["priority_score"],
        reason=result["priority_reason"], status=result["validation_status"],
        notes=" | ".join(result["validation_notes"]), review_flag=int(result["needs_review"]),
        duplicate_of=result["duplicate_of"], synthetic=int(result["synthetic"]),
        score_urgency=result["components"]["urgency"], score_people=result["components"]["people"],
        score_severity=result["components"]["severity"], score_vulnerability=result["components"]["vulnerability"])
    data.log_event("INTAKE_SUBMITTED", f"Report #{report_id} triaged as {result['priority']} {result['classification']}")
    result["id"] = report_id
    result["review_status"] = "pending"
    return result


def import_open_sos():
    """Copy current SOS rows into the intake queue once each. They stay labelled as demo records."""
    ensure_schema()
    linked = {row["incident_id"] for row in data.read("SELECT incident_id FROM citizen_reports WHERE incident_id IS NOT NULL")}
    imported = []
    for incident in data.read("SELECT * FROM incidents WHERE status != 'RESOLVED' ORDER BY id"):
        if incident["id"] in linked:
            continue
        risk = data.read("SELECT disaster_type FROM risk_logs WHERE zone=:z ORDER BY id DESC LIMIT 1", z=incident["zone"])
        imported.append(submit({
            "source": "existing_sos",
            "incident_id": incident["id"],
            "lat": incident["lat"],
            "lon": incident["lon"],
            "zone": incident["zone"],
            "people": incident["people"],
            "disaster_type": risk[0]["disaster_type"] if risk else None,
            "description": (
                f"[EXISTING SYNTHETIC SOS] Request #{incident['id']} for {incident['need_type']}. "
                "Copied from the demo incident table. Not a live citizen message."
            ),
            "synthetic": 1,
        }))
    return imported


def load_synthetic_examples():
    """Insert the labelled practice reports that are not already stored."""
    ensure_schema()
    present = {row["description"] for row in data.read("SELECT description FROM citizen_reports")}
    added = []
    for example in SYNTHETIC_EXAMPLES:
        if example["description"] in present:
            continue
        added.append(submit(dict(example, source="citizen", synthetic=1)))
    return added


def review_report(report_id, decision, reviewer):
    """Record a human decision. This does not send an alert or dispatch anyone."""
    decision = (decision or "").strip().lower()
    if decision not in ("approved", "rejected"):
        raise data.ActionError("Decision must be approved or rejected.")
    reviewer = (reviewer or "").strip()
    if not reviewer:
        raise data.ActionError("Enter the reviewer's name.")
    with data.transaction():
        row = _pending_report(report_id)
        data.write("""UPDATE citizen_reports SET review_status=:d, reviewed_by=:r WHERE id=:i""",
                   d=decision, r=reviewer, i=row["id"])
        data.log_event("INTAKE_" + decision.upper(), f"Report #{row['id']} {decision} by {reviewer}")
    return data.read("SELECT * FROM citizen_reports WHERE id=:i", i=report_id)[0]


def approve_and_log_sos(report_id, reviewer):
    """Approve a report, if needed, and store one open field request.

    A second call does not create another request. No alert is sent and nobody is dispatched.
    """
    reviewer = (reviewer or "").strip()
    if not reviewer:
        raise data.ActionError("Enter the reviewer's name.")
    with data.transaction():
        row = _stored_report(report_id)
        if row["sos_logged"]:
            raise data.ActionError(
                f"Field request #{row['incident_id']} is already logged for this report. No second request was created.")
        if row["review_status"] == "rejected":
            raise data.ActionError("A rejected report cannot be logged as a field request.")
        if row["source"] == "existing_sos" and row["incident_id"]:
            stored = data.read("SELECT id, status FROM incidents WHERE id=:i", i=row["incident_id"])
            if not stored:
                raise data.ActionError("The original SOS is no longer in the field-request table.")
            data.write("""UPDATE citizen_reports SET review_status='approved', reviewed_by=:r, sos_logged=1 WHERE id=:i""",
                       r=reviewer, i=row["id"])
            data.log_event("INTAKE_APPROVED",
                           f"Report #{row['id']} confirmed existing field request #{row['incident_id']} by {reviewer}")
            return data.read("SELECT * FROM citizen_reports WHERE id=:i", i=report_id)[0]
        if not row["zone"] or row["people"] is None:
            raise data.ActionError("A known zone and a people count are required to log an SOS.")
        try:
            data.demo.zone_position(row["zone"])
        except KeyError:
            raise data.ActionError("That zone is not on the demo map, so an SOS cannot be placed.")
        incident_id = data.add_incident(row["zone"], suggest_need(row["description"], row["classification"]), int(row["people"]))
        stored = data.read("SELECT id, status FROM incidents WHERE id=:i", i=incident_id)
        if not stored or stored[0]["status"] != "OPEN":
            raise data.ActionError("The field request was not stored as an open request.")
        data.write("""UPDATE citizen_reports SET incident_id=:n, review_status='approved', reviewed_by=:r, sos_logged=1 WHERE id=:i""",
                   n=incident_id, r=reviewer, i=row["id"])
        data.log_event("INTAKE_APPROVED", f"Report #{row['id']} logged as open field request #{incident_id} by {reviewer}")
    return data.read("SELECT * FROM citizen_reports WHERE id=:i", i=report_id)[0]


def evaluate(report, zones, existing=None):
    """Pure triage. zones and existing are plain lists, so tests need no database."""
    existing = existing or []
    source = report.get("source") or "citizen"
    description = _clip(report.get("description"))
    stated = canonical_category(report.get("disaster_type"))
    guessed, guess_hits = categories_in_text(description)
    classification, class_notes = _choose_category(stated, guessed, guess_hits)
    people, people_problem = parse_people(report.get("people"))
    urgency, urgency_problem = parse_urgency(report.get("urgency"))
    lat, lon, coord_problem = parse_coordinates(report.get("lat"), report.get("lon"))
    zone, zone_row, zone_note = resolve_zone(report.get("zone"), zones)
    notes = []
    notes.extend(class_notes)
    if people_problem == "missing" and source != "existing_sos":
        notes.append("Number of people affected is missing.")
    elif people_problem == "invalid":
        notes.append("Number of people is not a whole number of zero or more.")
    elif people is not None and people > 100000:
        notes.append("People affected is unusually large for one report and should be checked.")
    if urgency_problem == "missing" and source != "existing_sos":
        notes.append("Urgency is missing.")
    elif urgency_problem == "missing" and source == "existing_sos":
        notes.append("Urgency was not recorded on the original SOS, so it adds nothing to the score.")
    elif urgency_problem == "invalid":
        notes.append("Urgency is not one of CRITICAL, HIGH, MEDIUM or LOW.")
    if coord_problem == "invalid":
        notes.append("Latitude or longitude is outside the valid range.")
    elif coord_problem == "partial":
        notes.append("Only one coordinate was provided.")
    if zone_note:
        notes.append(zone_note)
    if zone_row and lat is not None and lon is not None:
        distance = haversine_km(lat, lon, zone_row["lat"], zone_row["lon"])
        if distance > ZONE_MISMATCH_KM:
            notes.append(f"Coordinates are {distance:.0f} km from {zone_row['name']}, which is inconsistent with that zone.")
    located = zone_row is not None or (lat is not None and lon is not None and coord_problem is None)
    if not located:
        notes.append("Location is missing. Provide a monitored zone or both latitude and longitude.")
    if classification == "Unclassified" and source != "existing_sos":
        notes.append("Disaster type could not be classified from the report.")
    duplicate_of = find_duplicate({"classification": classification, "zone": zone, "people": people, "lat": lat,
                                   "lon": lon, "description": description}, existing)
    if duplicate_of is not None:
        notes.append(f"Possible duplicate of report #{duplicate_of}. A person should compare them.")
    status = _validation_status(notes, people_problem, urgency_problem, coord_problem, classification, source, duplicate_of)
    severity, severity_reason = assess_severity(classification, people)
    vulnerability = float(zone_row["vulnerability"]) if zone_row and zone_row.get("vulnerability") is not None else 0.5
    vulnerability_known = zone_row is not None and zone_row.get("vulnerability") is not None
    breakdown = score_breakdown(urgency, people, severity, vulnerability)
    reason = explain_priority(breakdown, severity_reason, zone, vulnerability, vulnerability_known, urgency, people, source)
    needs_review = status != "valid" or any("unusually large" in note for note in notes)
    return {"source": source, "incident_id": report.get("incident_id"), "lat": lat, "lon": lon, "zone": zone,
            "stated_type": stated, "description": description, "classification": classification, "severity": severity,
            "severity_reason": severity_reason, "urgency": urgency, "people": people, "priority": breakdown["level"],
            "priority_score": breakdown["score"], "priority_reason": reason, "components": breakdown,
            "validation_status": status, "validation_notes": notes, "needs_review": needs_review,
            "duplicate_of": duplicate_of, "synthetic": 1 if report.get("synthetic") else 0}


def canonical_category(value):
    text = re.sub(r"\s+", " ", str(value or "").strip().lower())
    if text in ("", "not sure", "unknown", "none"):
        return None
    aliases = {"floods": "Flood", "heat wave": "Heatwave", "quake": "Earthquake", "fires": "Fire", "wildfire": "Fire"}
    for name in CATEGORIES:
        if text == name.lower() or text == name.lower() + "s":
            return name
    return aliases.get(text)


def categories_in_text(description):
    text = (description or "").lower()
    found = []
    for name, words in CATEGORY_WORDS:
        hits = [word for word in words if re.search(r"\b" + re.escape(word) + r"\b", text)]
        if hits:
            found.append((name, hits))
    return [name for name, _ in found], found


def _choose_category(stated, guessed, guess_hits):
    notes = []
    if len(guessed) > 1:
        notes.append("Description mentions more than one disaster: " + ", ".join(guessed) + ".")
    if stated and guessed and stated not in guessed:
        notes.append(f"Stated type is {stated}, but the description looks like {guessed[0]}.")
        return stated, notes
    if stated:
        return stated, notes
    if len(guessed) == 1:
        return guessed[0], notes
    if len(guessed) > 1:
        winner = max(guess_hits, key=lambda item: len(item[1]))[0]
        notes.append(f"Classification uses {winner} because that wording appears most often. Please confirm.")
        return winner, notes
    return "Unclassified", notes


def parse_people(value):
    if value is None or (isinstance(value, str) and not value.strip()):
        return None, "missing"
    if isinstance(value, bool) or isinstance(value, float) and not value.is_integer():
        return None, "invalid"
    if isinstance(value, (int, float)) and value >= 0:
        return int(value), None
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip()), None
    return None, "invalid"


def parse_urgency(value):
    text = str(value or "").strip().lower()
    if text in ("", "not specified", "unknown", "none"):
        return None, "missing"
    for level, words in URGENCY_WORDS.items():
        if text == level.lower() or text in words:
            return level, None
    return None, "invalid"


def parse_coordinates(lat, lon):
    blank = lambda v: v is None or (isinstance(v, str) and not str(v).strip())
    if blank(lat) and blank(lon):
        return None, None, None
    if blank(lat) or blank(lon):
        return None, None, "partial"
    try:
        lat_n, lon_n = float(lat), float(lon)
    except (TypeError, ValueError):
        return None, None, "invalid"
    if not (-90 <= lat_n <= 90 and -180 <= lon_n <= 180):
        return None, None, "invalid"
    return lat_n, lon_n, None


def resolve_zone(value, zones):
    text = str(value or "").strip()
    if text.lower() in ("", "unknown", "not sure", "none"):
        return None, None, None
    for zone in zones:
        if zone["name"].lower() == text.lower():
            return zone["name"], zone, None
    return text, None, f"{text} is not one of the monitored zones."


def assess_severity(classification, people):
    base = CATEGORY_BASE.get(classification, "Low")
    level = base
    reason = f"{classification} starts at {base} severity."
    count = people or 0
    if count >= 500:
        level = "Critical"
        reason += f" {count} people affected raises it to Critical."
    elif count >= 100 and SEVERITY_ORDER.index(level) < SEVERITY_ORDER.index("Critical"):
        level = SEVERITY_ORDER[SEVERITY_ORDER.index(level) + 1]
        reason += f" {count} people affected raises it one step to {level}."
    else:
        reason += " The number of people does not raise it further."
    return level, reason


def score_breakdown(urgency, people, severity, vulnerability):
    parts = {"urgency": URGENCY_POINTS.get(urgency, 0),
             "people": round(min(30.0, (people or 0) / 10.0), 1),
             "severity": SEVERITY_POINTS[severity],
             "vulnerability": round(max(0.0, min(1.0, float(vulnerability))) * 12, 1)}
    parts["score"] = round(min(100.0, sum(parts.values())), 1)
    parts["level"] = priority_level(parts["score"])
    return parts


def priority_level(score):
    if score >= 75:
        return "CRITICAL"
    if score >= 50:
        return "HIGH"
    if score >= 25:
        return "MEDIUM"
    return "LOW"


def explain_priority(parts, severity_reason, zone, vulnerability, vulnerability_known, urgency, people, source):
    """Plain-language score. Missing urgency stays at 0 and is never described as LOW."""
    if urgency is None and source == "existing_sos":
        urgency_text = "Urgency: Not recorded in original SOS. Contribution: 0. Missing urgency is not treated as LOW."
    elif urgency is None:
        urgency_text = "Urgency: Not provided. Contribution: 0. Missing urgency is not treated as LOW."
    else:
        urgency_text = f"Urgency: {urgency}. Contribution: {parts['urgency']}."
    if people is None:
        people_text = "People affected: unavailable. Contribution: 0."
    else:
        people_text = f"People affected: {people}. Contribution: {parts['people']} (capped at 30)."
    if vulnerability_known:
        vuln_text = f"Zone {zone} vulnerability {vulnerability:.2f}. Contribution: {parts['vulnerability']}."
    else:
        vuln_text = f"Zone vulnerability: unavailable, so 0.50 is used. Contribution: {parts['vulnerability']}."
    unavailable = []
    if urgency is None:
        unavailable.append("urgency")
    if people is None:
        unavailable.append("people affected")
    if not vulnerability_known:
        unavailable.append("zone vulnerability")
    unavailable_text = ("Unavailable information: " + ", ".join(unavailable) + ". ") if unavailable else ""
    return (
        f"Priority {parts['level']} (score {parts['score']}). {urgency_text} {people_text} "
        f"{severity_reason} Severity contribution: {parts['severity']}. {vuln_text} {unavailable_text}"
        f"Score = {parts['urgency']} + {parts['people']} + {parts['severity']} + {parts['vulnerability']} "
        f"= {parts['score']}. "
        "CRITICAL is 75 or more, HIGH is 50 to 74.9, MEDIUM is 25 to 49.9, and LOW is below 25. "
        "This is a rule-based prioritisation score, not an AI prediction or a disaster forecast."
    )


def urgency_label(source, urgency):
    """What the screen should show. A blank original SOS is not LOW."""
    if urgency in URGENCY_POINTS:
        return urgency
    if source == "existing_sos":
        return "Not recorded in original SOS"
    return "Not provided"


def queue_state(row):
    """Pending, approved only, logged as a field request, or rejected."""
    if row.get("review_status") == "rejected":
        return "Rejected"
    if row.get("sos_logged") and row.get("incident_id"):
        return f"Logged as field request #{row['incident_id']}"
    if row.get("review_status") == "approved":
        return "Approved, not logged"
    return "Pending"


def citizen_form_error(urgency):
    """A new citizen report must name an urgency. Missing is not accepted as LOW."""
    level, problem = parse_urgency(urgency)
    if problem == "missing":
        return "Choose CRITICAL, HIGH, MEDIUM or LOW. Leaving urgency blank is not the same as LOW."
    if problem == "invalid" or level is None:
        return "Urgency must be CRITICAL, HIGH, MEDIUM or LOW."
    return None


def find_duplicate(candidate, existing):
    for prior in existing:
        if str(prior.get("review_status") or "pending").lower() == "rejected":
            continue
        if _same_report(candidate, prior):
            return prior.get("id")
    return None


def _same_report(candidate, prior):
    if candidate["classification"] != (prior.get("classification") or ""):
        return False
    same_zone = candidate.get("zone") and candidate.get("zone") == prior.get("zone")
    if candidate["classification"] == "Unclassified":
        return bool(same_zone) and _same_text(candidate.get("description"), prior.get("description"))
    people_close = _people_close(candidate.get("people"), prior.get("people"))
    near = _near(candidate, prior)
    similar_words = _similar(candidate.get("description"), prior.get("description"))
    if same_zone and people_close and (similar_words or _same_text(candidate.get("description"), prior.get("description"))):
        return True
    return near and people_close


def _people_close(left, right):
    if left is None or right is None:
        return False
    return abs(int(left) - int(right)) <= max(5, 0.25 * max(int(left), int(right)))


def _near(candidate, prior):
    if None in (candidate.get("lat"), candidate.get("lon"), prior.get("lat"), prior.get("lon")):
        return False
    return haversine_km(candidate["lat"], candidate["lon"], prior["lat"], prior["lon"]) <= DUPLICATE_KM


def _similar(left, right):
    a, b = _tokens(left), _tokens(right)
    if len(a) < 4 or len(b) < 4:
        return False
    return len(a & b) / len(a | b) >= 0.5


def _same_text(left, right):
    return " ".join((left or "").lower().split()) == " ".join((right or "").lower().split()) and bool((left or "").strip())


def _tokens(text):
    return {word for word in re.findall(r"[a-z0-9]+", (text or "").lower()) if word not in STOPWORDS}


def _validation_status(notes, people_problem, urgency_problem, coord_problem, classification, source, duplicate_of):
    if people_problem == "invalid" or coord_problem == "invalid" or urgency_problem == "invalid":
        return "invalid"
    if any(note.startswith("Coordinates are") or note.startswith("Stated type") or note.startswith("Description mentions") for note in notes):
        return "inconsistent"
    if duplicate_of is not None:
        return "duplicate"
    missing = [note for note in notes if note.startswith("Location is missing") or note.startswith("Number of people")
               or note.startswith("Urgency is missing") or note.startswith("Disaster type could not")]
    if missing and source != "existing_sos":
        return "incomplete"
    if classification == "Unclassified" and source == "existing_sos":
        return "incomplete"
    return "valid"


def suggest_need(description, classification):
    text = (description or "").lower()
    if re.search(r"\b(medic|injured|injury|hospital|patient)\b", text):
        return "Medical"
    if "food" in text:
        return "Food"
    if "water" in text and classification != "Flood":
        return "Water"
    if re.search(r"\b(shelter|roof|homeless)\b", text):
        return "Shelter"
    return "Rescue"


def haversine_km(lat1, lon1, lat2, lon2):
    radius = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlat, dlon = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def _clip(description):
    text = str(description or "").strip()
    if len(text) > 2000:
        return text[:2000]
    return text


def _pending_report(report_id):
    row = _stored_report(report_id)
    if row["review_status"] != "pending":
        raise data.ActionError("That report has already been reviewed.")
    return row


def _stored_report(report_id):
    ensure_schema()
    rows = data.read("SELECT * FROM citizen_reports WHERE id=:i", i=report_id)
    if not rows:
        raise data.ActionError("That report does not exist.")
    return rows[0]
