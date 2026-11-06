# =====================================================================
# demo_data.py  :  the made-up demo world, shared by BOTH options
# ---------------------------------------------------------------------
# Coastal Maharashtra. Every number here is DEMO DATA for the prototype:
# replace it with real data for your chosen region.
# Only plain Python lists and dictionaries: no database code.
# =====================================================================

import random  # random numbers (with a fixed seed, so the demo is the same every time)
from datetime import timedelta  # time differences for the 24-hour history

ITEMS = ["Food kits", "Medical kits", "Water packs", "Tarpaulins", "Oxygen cylinders"]  # supply types
NEED_TYPES = ["Medical", "Food", "Water", "Shelter", "Rescue"]  # kinds of SOS request
DISASTER_TYPES = ["Cyclone", "Flood", "Heatwave", "Landslide", "Earthquake"]  # disasters we can show

# Zones we monitor: (name, latitude, longitude, population, vulnerability 0 to 1)
ZONES = [
    ("Mumbai", 19.0760, 72.8777, 12400000, 0.70),
    ("Thane", 19.2183, 72.9781, 1900000, 0.60),
    ("Palghar", 19.6967, 72.7699, 3000000, 0.75),
    ("Raigad", 18.6414, 72.8722, 2600000, 0.80),
    ("Ratnagiri", 16.9902, 73.3120, 1600000, 0.85),
    ("Sindhudurg", 16.3500, 73.5500, 850000, 0.80),
    ("Pune", 18.5204, 73.8567, 9400000, 0.40),
]

# Supply hubs (warehouses): (id, name, latitude, longitude)
HUBS = [
    (1, "Pune Central Warehouse", 18.5204, 73.8567),
    (2, "Thane Relief Hub", 19.2500, 73.0500),
    (3, "Nashik Depot", 19.9975, 73.7898),
    (4, "Kolhapur Depot", 16.7050, 74.2433),
]

# Starting stock at each hub: hub id -> {item: quantity}
INVENTORY = {
    1: {"Food kits": 656, "Medical kits": 302, "Water packs": 1326, "Tarpaulins": 1203, "Oxygen cylinders": 1114},
    2: {"Food kits": 771, "Medical kits": 619, "Water packs": 2433, "Tarpaulins": 556, "Oxygen cylinders": 1928},
    3: {"Food kits": 330, "Medical kits": 322, "Water packs": 583, "Tarpaulins": 1095, "Oxygen cylinders": 1152},
    4: {"Food kits": 2269, "Medical kits": 308, "Water packs": 2498, "Tarpaulins": 1014, "Oxygen cylinders": 2432},
}

# Hospitals: (id, name, zone, lat, lon, total beds, free beds, ICU beds, free ICU, oxygen cylinders)
HOSPITALS = [
    (1, "Mumbai General Hospital", "Mumbai", 19.0000, 72.8400, 400, 60, 40, 6, 300),
    (2, "Thane Civil Hospital", "Thane", 19.1900, 72.9700, 250, 80, 20, 8, 200),
    (3, "Raigad District Hospital", "Raigad", 18.6400, 72.9300, 150, 25, 10, 2, 90),
    (4, "Ratnagiri Civil Hospital", "Ratnagiri", 16.9900, 73.3000, 180, 12, 12, 1, 60),
    (5, "Sindhudurg District Hospital", "Sindhudurg", 16.1200, 73.6900, 120, 30, 8, 4, 50),
    (6, "Pune Trauma Centre", "Pune", 18.5000, 73.8300, 300, 140, 30, 18, 350),
]

# Road network: (road id, from place, to place, normal travel minutes)
# Places are zone, hub or hospital names. Status starts OPEN for every road.
ROADS = [
    ("R1", "Palghar", "Thane", 90),
    ("R2", "Thane", "Mumbai", 45),
    ("R3", "Mumbai", "Raigad", 120),
    ("R4", "Thane", "Pune", 150),
    ("R5", "Pune", "Raigad", 150),
    ("R6", "Raigad", "Ratnagiri", 210),
    ("R7", "Pune", "Kolhapur Depot", 240),
    ("R8", "Kolhapur Depot", "Ratnagiri", 150),
    ("R9", "Kolhapur Depot", "Sindhudurg", 180),
    ("R10", "Ratnagiri", "Sindhudurg", 150),
    ("R11", "Nashik Depot", "Thane", 180),
    ("R12", "Nashik Depot", "Pune", 240),
    ("R13", "Thane Relief Hub", "Thane", 10),
    ("R14", "Pune Central Warehouse", "Pune", 10),
    ("R15", "Mumbai General Hospital", "Mumbai", 15),
    ("R16", "Thane Civil Hospital", "Thane", 10),
    ("R17", "Raigad District Hospital", "Raigad", 15),
    ("R18", "Ratnagiri Civil Hospital", "Ratnagiri", 10),
    ("R19", "Sindhudurg District Hospital", "Sindhudurg", 35),
    ("R20", "Pune Trauma Centre", "Pune", 15),
]

# Starting disaster type and chance for each zone
BASE_RISK = {
    "Mumbai": ("Flood", 0.35), "Thane": ("Flood", 0.30), "Palghar": ("Cyclone", 0.25),
    "Raigad": ("Cyclone", 0.55), "Ratnagiri": ("Cyclone", 0.70), "Sindhudurg": ("Cyclone", 0.60),
    "Pune": ("Flood", 0.10),
}

# SOS requests: (zone, need, people, minutes ago). For Medical, "people" = patients needing a bed.
INCIDENTS = [
    ("Palghar", "Medical", 80, 412), ("Sindhudurg", "Shelter", 51, 451), ("Palghar", "Medical", 32, 56),
    ("Mumbai", "Rescue", 115, 303), ("Sindhudurg", "Shelter", 21, 304), ("Palghar", "Water", 98, 484),
    ("Ratnagiri", "Food", 20, 188), ("Ratnagiri", "Medical", 22, 121), ("Sindhudurg", "Medical", 52, 463),
    ("Raigad", "Medical", 90, 147), ("Sindhudurg", "Water", 101, 509), ("Sindhudurg", "Shelter", 3, 278),
]


def zone_position(name):  # latitude and longitude of a zone
    for z in ZONES:  # look through every zone
        if z[0] == name:  # found it
            return z[1], z[2]  # its latitude and longitude
    raise KeyError(name)  # unknown zone name


def risk_history(now):  # readings every 6 hours for the last 24 hours, rising towards "now"
    rng = random.Random(7)  # fixed seed = same demo every time
    rows = []  # list of readings to return
    for hours_ago in [24, 18, 12, 6, 0]:  # five points in time
        ts = (now - timedelta(hours=hours_ago)).strftime("%Y-%m-%d %H:%M:%S")  # the time as text
        for name, lat, lon, _, _ in ZONES:  # one reading per zone
            dtype, base = BASE_RISK[name]  # disaster type and final chance
            prob = base * (0.6 + 0.4 * (1 - hours_ago / 24)) + rng.uniform(-0.03, 0.03)  # grows over the day
            rows.append((ts, name, lat, lon, dtype, round(min(max(prob, 0), 1), 2), "IMD (demo)"))  # save it
    return rows  # all readings


def incident_rows(now):  # SOS requests with a position near their zone centre
    rng = random.Random(42)  # fixed seed
    rows = []  # list to return
    for zone, need, people, minutes_ago in INCIDENTS:  # every demo request
        lat, lon = zone_position(zone)  # zone centre
        ts = (now - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%d %H:%M:%S")  # when it was reported
        rows.append((ts, lat + rng.uniform(-0.07, 0.07), lon + rng.uniform(-0.07, 0.07), zone, need, people))  # near the centre
    return rows  # all requests
