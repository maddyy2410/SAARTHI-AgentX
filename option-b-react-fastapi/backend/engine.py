# =====================================================================
# engine.py  :  THE "BRAIN" OF THE SYSTEM (shared by both options)
# ---------------------------------------------------------------------
# Only decision logic lives here. No screen code, no database code:
# it takes plain lists/dictionaries and returns plain lists/dictionaries.
#
#   1. travel_table()      how long it takes to drive between places (road network)
#   2. compute_severity()  which zone is in the worst situation
#   3. plan_supplies()     which hub sends what to which zone   (OR-Tools)
#   4. plan_patients()     which hospital takes which patients  (OR-Tools)
#   5. plan_rows() / diff_plans()  what changed since the last plan
#   6. validate_plan()     automatic checks used by the scenario tests
#   7. make_hash() / verify_chain()  tamper-evident supply ledger
#
# Because the screens never look inside these functions, the team can
# change the algorithms here without touching the dashboard.
# =====================================================================

import hashlib  # SHA-256 fingerprints for the ledger
import heapq  # a "priority queue", used to find shortest routes quickly
import json  # turns data into text before fingerprinting

try:  # OR-Tools is Google's optimisation library ("pip install ortools")
    from ortools.linear_solver import pywraplp  # its linear programming solver
except ImportError:  # not installed on this computer
    pywraplp = None  # remember that, and use the simple built-in rule instead

# ---------------------------------------------------------------------
# SETTINGS (tune these)
# ---------------------------------------------------------------------
LEVEL_CUTOFFS = [(50, "Critical"), (30, "High"), (15, "Moderate")]  # score thresholds; below 15 is Low
PRIORITY_LEVELS = ["Critical", "High"]  # zones at these levels get supply plans
NEED_PER_SCORE_POINT = {"Food kits": 20, "Medical kits": 5, "Water packs": 30}  # units needed per severity point
SLOW_FACTOR = 2.0  # a SLOW road takes twice as long
SHORTFALL_PENALTY = 1000  # cost of leaving one unit (or patient) unserved, per severity point
STATUS_FLOW = ["ALLOCATED", "DISPATCHED", "IN_TRANSIT", "RECEIVED", "DISTRIBUTED"]  # shipment steps, in order


def level_for(score):  # 63.5 -> "Critical"
    for cutoff, name in LEVEL_CUTOFFS:  # highest threshold first
        if score >= cutoff:  # reached it
            return name  # that level
    return "Low"  # none reached


def fmt_minutes(minutes):  # 135 -> "2h 15m"
    if minutes is None:  # unreachable
        return "no route"  # say so
    h, m = divmod(int(round(minutes)), 60)  # split into hours and minutes
    return f"{h}h {m:02d}m" if h else f"{m} min"  # "2h 15m" or "45 min"


# ---------------------------------------------------------------------
# 1. ROAD NETWORK: shortest travel time between places
# ---------------------------------------------------------------------
def road_minutes(road):  # travel time on one road, given its status
    status = road.get("status") or "OPEN"  # OPEN, SLOW or BLOCKED
    if status == "BLOCKED":  # cannot be used at all
        return None  # no time = not usable
    base = float(road["minutes"])  # normal minutes
    return base * SLOW_FACTOR if status == "SLOW" else base  # slow roads take longer


def shortest_from(roads, source):  # Dijkstra's algorithm: fastest route from one place to all others
    graph = {}  # place -> list of (neighbour, minutes, road id)
    for r in roads:  # add every usable road in both directions
        mins = road_minutes(r)  # its current travel time
        if mins is None:  # blocked
            continue  # skip it
        graph.setdefault(r["a"], []).append((r["b"], mins, r["id"]))  # a -> b
        graph.setdefault(r["b"], []).append((r["a"], mins, r["id"]))  # b -> a
    best = {source: 0.0}  # fastest known time to each place
    how = {}  # place -> (previous place, road id) on the fastest route
    queue = [(0.0, source)]  # places still to explore, closest first
    while queue:  # until nothing is left to explore
        t, place = heapq.heappop(queue)  # the closest unexplored place
        if t > best.get(place, float("inf")):  # an outdated entry
            continue  # ignore it
        for nxt, mins, rid in graph.get(place, []):  # each road leaving this place
            nt = t + mins  # time to reach the neighbour this way
            if nt < best.get(nxt, float("inf")):  # faster than anything known
                best[nxt] = nt  # remember the new best time
                how[nxt] = (place, rid)  # and how we got there
                heapq.heappush(queue, (nt, nxt))  # explore from it later
    return best, how  # times and route steps


def travel_table(roads, sources, targets):
    """Returns {(source, target): {"minutes": float or None, "roads": [road ids]}}."""
    table = {}  # the answer
    for s in sources:  # each starting place
        best, how = shortest_from(roads, s)  # fastest routes from it
        for t in targets:  # each destination
            if t not in best:  # cannot be reached
                table[(s, t)] = {"minutes": None, "roads": []}  # no route
                continue  # next destination
            path, place = [], t  # walk backwards from the destination
            while place != s:  # until we are back at the start
                place, rid = how[place]  # the previous place and road
                path.append(rid)  # collect the road id
            table[(s, t)] = {"minutes": best[t], "roads": list(reversed(path))}  # time and roads in order
    return table  # all pairs


# ---------------------------------------------------------------------
# 2. SEVERITY: which zone is worst?
# ---------------------------------------------------------------------
def compute_severity(zones, latest_risk, open_requests):
    """zones: [{name, lat, lon, population, vulnerability}]; latest_risk: zone -> {disaster_type, probability};
    open_requests: zone -> count. Returns zones sorted most severe first."""
    rows = []  # one row per zone
    for z in zones:  # each zone
        risk = latest_risk.get(z["name"], {})  # newest reading (may be missing)
        prob = float(risk.get("probability") or 0)  # chance 0 to 1
        vuln = float(z.get("vulnerability") or 0)  # fragility 0 to 1
        sos = int(open_requests.get(z["name"], 0))  # open SOS requests
        score = round(min(100 * prob * vuln + min(2 * sos, 10), 100), 1)  # the formula, capped at 100
        rows.append({"name": z["name"], "lat": z["lat"], "lon": z["lon"], "population": z.get("population"),
                     "vulnerability": vuln, "disaster_type": risk.get("disaster_type") or "None",
                     "probability": prob, "open_requests": sos, "score": score, "level": level_for(score)})  # save
    rows.sort(key=lambda r: r["score"], reverse=True)  # worst first
    return rows  # done


# ---------------------------------------------------------------------
# 3. SUPPLY PLAN (OR-Tools): move stock to the zones that need it most
# ---------------------------------------------------------------------
def supply_needs(severity, active_shipments):  # what each priority zone still needs
    needs = []  # list of {zone, item, need, score, level, ...}
    for z in severity:  # each zone
        if z["level"] not in PRIORITY_LEVELS:  # only Critical and High
            continue  # skip the rest
        for item, per_point in NEED_PER_SCORE_POINT.items():  # each item type
            total = int(z["score"] * per_point)  # total units needed
            on_way = sum(int(s["qty"]) for s in active_shipments
                         if s["destination"] == z["name"] and s["item"] == item)  # already travelling there
            if total - on_way > 0:  # still missing something
                needs.append({"zone": z["name"], "item": item, "need": total - on_way, "score": z["score"],
                              "level": z["level"], "disaster_type": z["disaster_type"],
                              "probability": z["probability"], "vulnerability": z["vulnerability"]})  # remember it
    return needs  # everything still missing


def _solve_supplies_ortools(needs, hubs, stock, travel):  # the optimal plan, using OR-Tools
    solver = pywraplp.Solver.CreateSolver("GLOP")  # GLOP = Google's linear programming solver
    if solver is None:  # could not start the solver
        return None  # caller falls back to the simple rule
    x = {}  # x[(hub id, need index)] = units sent from that hub for that need
    for n_i, n in enumerate(needs):  # each need
        for h in hubs:  # each hub
            leg = travel.get((h["name"], n["zone"]))  # route from hub to zone
            have = stock.get((h["id"], n["item"]), 0)  # hub's stock of this item
            if leg and leg["minutes"] is not None and have > 0:  # reachable and has stock
                x[(h["id"], n_i)] = solver.NumVar(0, have, f"x_{h['id']}_{n_i}")  # a decision variable
    short = {n_i: solver.NumVar(0, n["need"], f"short_{n_i}") for n_i, n in enumerate(needs)}  # unmet units
    for n_i, n in enumerate(needs):  # rule 1: every need is met, or counted as shortfall
        solver.Add(solver.Sum([v for (h_id, k), v in x.items() if k == n_i]) + short[n_i] == n["need"])
    for h in hubs:  # rule 2: a hub cannot send more than it has
        for item in NEED_PER_SCORE_POINT:  # per item
            vars_ = [v for (h_id, k), v in x.items() if h_id == h["id"] and needs[k]["item"] == item]  # its shipments
            if vars_:  # only if it ships this item at all
                solver.Add(solver.Sum(vars_) <= stock.get((h["id"], item), 0))  # stay within stock
    cost = []  # the objective: total travel time, plus a big penalty for anything left unmet
    for (h_id, n_i), v in x.items():  # travel part
        hub_name = next(h["name"] for h in hubs if h["id"] == h_id)  # hub name for the travel lookup
        cost.append(travel[(hub_name, needs[n_i]["zone"])]["minutes"] * v)  # minutes x units
    for n_i, v in short.items():  # shortfall part: worse zones cost more to leave short
        cost.append(SHORTFALL_PENALTY * needs[n_i]["score"] * v)  # penalty x units
    solver.Minimize(solver.Sum(cost))  # find the cheapest plan
    if solver.Solve() != pywraplp.Solver.OPTIMAL:  # no optimal answer found
        return None  # caller falls back
    return {key: int(v.solution_value() + 1e-6) for key, v in x.items() if v.solution_value() >= 0.5}  # whole units


def _solve_supplies_simple(needs, hubs, stock, travel):  # fallback: worst zone first, nearest hub first
    left = dict(stock)  # stock we can still use
    plan = {}  # (hub id, need index) -> units
    order = sorted(range(len(needs)), key=lambda i: -needs[i]["score"])  # worst zones first
    for n_i in order:  # each need
        n = needs[n_i]  # the need itself
        remaining = n["need"]  # still to cover
        options = []  # reachable hubs with this item
        for h in hubs:  # each hub
            leg = travel.get((h["name"], n["zone"]))  # route
            if leg and leg["minutes"] is not None and left.get((h["id"], n["item"]), 0) > 0:  # usable
                options.append((leg["minutes"], h["id"]))  # remember time and hub
        for _, h_id in sorted(options):  # nearest first
            take = min(remaining, left[(h_id, n["item"])])  # as much as we can
            if take > 0:  # something to send
                plan[(h_id, n_i)] = take  # record it
                left[(h_id, n["item"])] -= take  # use up the stock
                remaining -= take  # less still needed
            if remaining <= 0:  # need covered
                break  # stop looking
    return plan  # the plan


def plan_supplies(severity, hubs, inventory, active_shipments, travel):
    """Returns (recommendations, shortfalls, solver name)."""
    needs = supply_needs(severity, active_shipments)  # what is missing
    stock = {(r["hub_id"], r["item"]): int(r["qty"]) for r in inventory}  # (hub, item) -> quantity
    plan, solver_name = None, "Built-in rule (install ortools for optimal plans)"  # defaults
    if pywraplp is not None and needs:  # OR-Tools available and something to plan
        plan = _solve_supplies_ortools(needs, hubs, stock, travel)  # optimal plan
        solver_name = "OR-Tools (GLOP linear programming)"  # say which solver ran
    if plan is None:  # no OR-Tools, or it failed
        plan = _solve_supplies_simple(needs, hubs, stock, travel)  # simple rule
        solver_name = "Built-in rule (install ortools for optimal plans)"  # say so
    hub_by_id = {h["id"]: h for h in hubs}  # quick lookup
    recs, sent = [], {}  # recommendations, and units planned per need
    for (h_id, n_i), qty in sorted(plan.items(), key=lambda kv: (-needs[kv[0][1]]["score"], kv[0][1])):  # worst first
        n, hub = needs[n_i], hub_by_id[h_id]  # the need and the hub
        leg = travel[(hub["name"], n["zone"])]  # the route used
        sent[n_i] = sent.get(n_i, 0) + qty  # count what we planned
        recs.append({"zone": n["zone"], "level": n["level"], "item": n["item"], "qty": qty,
                     "from_hub": hub["name"], "hub_id": h_id, "minutes": leg["minutes"], "route": leg["roads"],
                     "reason": (f"{n['level']} zone: {n['disaster_type']} probability {n['probability']:.0%}, "
                                f"vulnerability {n['vulnerability']:.2f}. Drive {fmt_minutes(leg['minutes'])} "
                                f"via {', '.join(leg['roads']) or 'local roads'}.")})  # plain-English reason
    shortfalls = []  # what nobody could supply, and why
    for i, n in enumerate(needs):  # each need
        missing = n["need"] - sent.get(i, 0)  # units still missing
        if missing > 0:  # something is short
            cut_off = not any((travel.get((h["name"], n["zone"])) or {}).get("minutes") is not None for h in hubs)  # no road in?
            shortfalls.append({"zone": n["zone"], "item": n["item"], "missing": missing,
                               "why": "Cut off: no open road from any hub" if cut_off else "Not enough stock in reachable hubs"})
    return recs, shortfalls, solver_name  # everything the screen needs


# ---------------------------------------------------------------------
# 4. PATIENT PLAN (OR-Tools): which hospital takes which patients
# ---------------------------------------------------------------------
def plan_patients(groups, hospitals, travel, zone_scores):
    """groups: medical SOS [{id, zone, people}]; hospitals: [{name, available_beds, total_beds, icu_available, ...}];
    zone_scores: zone -> severity score. Returns (assignments, unplaced, solver name)."""
    arcs = {}  # (group index, hospital index) -> minutes
    for g_i, g in enumerate(groups):  # each group of patients
        for h_i, h in enumerate(hospitals):  # each hospital
            leg = travel.get((g["zone"], h["name"]))  # route from the zone to the hospital
            if leg and leg["minutes"] is not None and int(h["available_beds"]) > 0:  # reachable, has beds
                arcs[(g_i, h_i)] = leg["minutes"]  # possible assignment
    plan, solver_name = None, "Built-in rule (install ortools for optimal plans)"  # defaults
    if pywraplp is not None and groups:  # OR-Tools available
        solver = pywraplp.Solver.CreateSolver("GLOP")  # linear programming solver
        if solver is not None:  # started
            y = {k: solver.NumVar(0, groups[k[0]]["people"], f"y_{k[0]}_{k[1]}") for k in arcs}  # patients sent
            u = {g_i: solver.NumVar(0, g["people"], f"u_{g_i}") for g_i, g in enumerate(groups)}  # patients unplaced
            for g_i, g in enumerate(groups):  # rule 1: every patient gets a bed or counts as unplaced
                solver.Add(solver.Sum([v for (a, b), v in y.items() if a == g_i]) + u[g_i] == g["people"])
            for h_i, h in enumerate(hospitals):  # rule 2: never more patients than free beds
                vars_ = [v for (a, b), v in y.items() if b == h_i]  # patients sent to this hospital
                if vars_:  # only if it can receive any
                    solver.Add(solver.Sum(vars_) <= int(h["available_beds"]))  # capacity limit
            cost = [arcs[k] * v for k, v in y.items()]  # travel minutes x patients
            cost += [SHORTFALL_PENALTY * (1 + zone_scores.get(groups[g_i]["zone"], 0)) * v for g_i, v in u.items()]  # unplaced penalty
            solver.Minimize(solver.Sum(cost))  # cheapest plan
            if solver.Solve() == pywraplp.Solver.OPTIMAL:  # found it
                plan = {k: int(round(v.solution_value())) for k, v in y.items() if v.solution_value() >= 0.5}  # whole patients
                solver_name = "OR-Tools (GLOP linear programming)"  # say so
    if plan is None:  # fallback: worst zone first, nearest hospital with beds first
        free = {h_i: int(h["available_beds"]) for h_i, h in enumerate(hospitals)}  # beds left
        plan = {}  # (group, hospital) -> patients
        for g_i in sorted(range(len(groups)), key=lambda i: -zone_scores.get(groups[i]["zone"], 0)):  # worst first
            left = groups[g_i]["people"]  # patients to place
            for (a, h_i), mins in sorted(((k, m) for k, m in arcs.items() if k[0] == g_i), key=lambda km: km[1]):  # nearest first
                take = min(left, free[h_i])  # as many as fit
                if take > 0:  # room for some
                    plan[(a, h_i)] = take  # record
                    free[h_i] -= take  # beds used
                    left -= take  # fewer to place
                if left <= 0:  # all placed
                    break  # next group
    assignments, placed = [], {}  # output rows, patients placed per group
    for (g_i, h_i), n in sorted(plan.items(), key=lambda kv: (kv[0][0], arcs[kv[0]])):  # group by group, nearest first
        g, h = groups[g_i], hospitals[h_i]  # the group and hospital
        placed[g_i] = placed.get(g_i, 0) + n  # count
        leg = travel[(g["zone"], h["name"])]  # route used
        reachable = sorted((m, hospitals[b]) for (a, b), m in arcs.items() if a == g_i)  # hospitals by distance
        nearest = reachable[0][1] if reachable else None  # the closest one with free beds
        why = (f"{h['name']} has {h['available_beds']} of {h['total_beds']} beds free "
               f"(ICU {h.get('icu_available', 0)}/{h.get('icu_total', 0)}). Drive {fmt_minutes(leg['minutes'])}.")  # base reason
        if nearest is not None and nearest["name"] != h["name"]:  # a closer hospital was not used for these patients
            why += f" Nearer {nearest['name']} has no beds left for this group."  # explain why not
        assignments.append({"incident_id": g["id"], "zone": g["zone"], "patients": n, "hospital": h["name"],
                            "hospital_id": h.get("id"), "minutes": leg["minutes"], "route": leg["roads"],
                            "reason": why})  # save the row
    unplaced = [{"incident_id": g["id"], "zone": g["zone"], "patients": g["people"] - placed.get(i, 0)}
                for i, g in enumerate(groups) if g["people"] - placed.get(i, 0) > 0]  # nobody could take them
    return assignments, unplaced, solver_name  # everything the screen needs


# ---------------------------------------------------------------------
# 5. PLAN VERSIONS: what changed since the last plan?
# ---------------------------------------------------------------------
def plan_rows(recs, assignments):  # turns both plans into rows {kind, key, summary, data}
    rows = []  # the rows
    for r in recs:  # supply rows
        rows.append({"kind": "supply", "key": f"supply|{r['zone']}|{r['item']}|{r['from_hub']}",
                     "summary": f"{r['qty']} {r['item']} from {r['from_hub']} to {r['zone']} via {'-'.join(r['route'])}",
                     "data": r})  # key = what, summary = how
    for a in assignments:  # patient rows
        rows.append({"kind": "patient", "key": f"patient|{a['incident_id']}|{a['hospital']}",
                     "summary": f"{a['patients']} patients from SOS #{a['incident_id']} to {a['hospital']} via {'-'.join(a['route'])}",
                     "data": a})  # key and summary
    return rows  # all rows


def diff_plans(old_rows, new_rows):  # compares two plans
    old = {r["key"]: r["summary"] for r in old_rows}  # key -> summary, before
    new = {r["key"]: r["summary"] for r in new_rows}  # key -> summary, after
    changed = [k for k in new if old.get(k) != new[k]]  # new or different rows
    removed = [k for k in old if k not in new]  # rows that disappeared
    return {"changed": changed, "removed": removed, "count": len(changed) + len(removed)}  # summary of the change


# ---------------------------------------------------------------------
# 6. AUTOMATIC CHECKS (the scenario tests from our notes)
# ---------------------------------------------------------------------
def validate_plan(recs, assignments, shortfalls, hospitals, roads, inventory, severity, ledger_ok, changed_count,
                  travel=None, expect_change=True):
    blocked = {r["id"] for r in roads if r.get("status") == "BLOCKED"}  # blocked road ids
    beds = {}  # patients per hospital
    for a in assignments:  # add them up
        beds[a["hospital"]] = beds.get(a["hospital"], 0) + a["patients"]
    cap_ok = all(beds.get(h["name"], 0) <= int(h["available_beds"]) for h in hospitals)  # never over capacity
    roads_ok = all(not (set(x["route"]) & blocked) for x in list(recs) + list(assignments))  # no blocked road used
    used = {}  # units taken from each hub per item
    for r in recs:  # add them up
        used[(r["hub_id"], r["item"])] = used.get((r["hub_id"], r["item"]), 0) + r["qty"]
    stock = {(i["hub_id"], i["item"]): int(i["qty"]) for i in inventory}  # stock per hub and item
    stock_ok = all(q <= stock.get(k, 0) for k, q in used.items())  # never more than in stock
    score = {z["name"]: z["score"] for z in severity}  # zone -> score
    prio_ok = True  # worst zones first: if a zone is short of an item, no less severe zone gets that item
    for s in shortfalls:  # each shortage
        for r in recs:  # each planned move of the same item to a LESS severe zone...
            if r["item"] == s["item"] and score.get(r["zone"], 0) < score.get(s["zone"], 0):
                leg = (travel or {}).get((r["from_hub"], s["zone"]), {"minutes": 0})  # ...could that hub reach the short zone?
                if leg.get("minutes") is not None:  # yes: the more severe zone should have been served first
                    prio_ok = False  # rule broken
    return [  # one row per check: name, passed?, explanation
        {"check": "Allocation changed after the event" if expect_change else "Baseline plan created",
         "ok": changed_count > 0 if expect_change else True,
         "detail": f"{changed_count} allocations changed" if expect_change else "Starting plan for comparison"},
        {"check": "Hospital capacity respected", "ok": cap_ok, "detail": "No hospital gets more patients than free beds"},
        {"check": "Blocked roads avoided", "ok": roads_ok,
         "detail": f"Blocked: {', '.join(sorted(blocked)) or 'none'}"},
        {"check": "Stock limits respected", "ok": stock_ok, "detail": "No hub sends more than it holds"},
        {"check": "High-priority zones served first", "ok": prio_ok,
         "detail": "A shortage only falls on a more severe zone when no hub can reach it"},
        {"check": "Every movement recorded in the ledger", "ok": bool(ledger_ok), "detail": "Hash chain verified"},
    ]


# ---------------------------------------------------------------------
# 7. TAMPER-EVIDENT LEDGER (hash chain)
# ---------------------------------------------------------------------
def make_hash(prev_hash, record):  # fingerprint of (previous fingerprint + this record)
    return hashlib.sha256((prev_hash + json.dumps(record, sort_keys=True)).encode()).hexdigest()


def ledger_record(row):  # the fields protected by the fingerprint
    return {"shipment_id": row["shipment_id"], "timestamp": str(row["timestamp"]), "item": row["item"],
            "qty": int(row["qty"]), "source": row["source_hub"], "dest": row["destination"], "status": row["status"]}


def verify_chain(rows):  # (True, None) if intact, else (False, first broken entry id)
    prev = "GENESIS"  # the first record chains to this word
    for row in rows:  # oldest first
        if row["prev_hash"] != prev or row["hash"] != make_hash(prev, ledger_record(row)):  # mismatch
            return False, row["entry_id"]  # tampering found here
        prev = row["hash"]  # move along the chain
    return True, None  # all good
