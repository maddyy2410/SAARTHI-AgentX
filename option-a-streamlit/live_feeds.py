# =====================================================================
# live_feeds.py  :  read-only official / open hazard feeds for Option A
# ---------------------------------------------------------------------
# These helpers fetch live XML/JSON for the dashboard only.
# They never write SQLite tables, never replan, and never change
# allocation, inventory, incidents, or volunteer records.
#
# Sources:
#   NDMA SACHET CAP RSS + CAP/polygon XML
#   IMD API Gateway (api.imd.gov.in) + public IMD CAP RSS + WIS2 OAPI
#   Open-Meteo forecast
#   USGS earthquake GeoJSON
#   NASA FIRMS active fires (MAP_KEY)
#   GDACS multi-hazard RSS
#   OpenRouteService directions (API key)
# =====================================================================

from __future__ import annotations

import csv
import io
import json
import os
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

import certifi
import requests
import urllib3

USER_AGENT = "SAARTHI-AgentX/1.0 (disaster-command-centre; read-only feed client)"
REQUEST_TIMEOUT = 20

# Indian government hosts that often present incomplete public CA chains.
_RELAXED_SSL_HOSTS = {
    "api.imd.gov.in",
    "wis2box.imd.gov.in",
    "sachet.ndma.gov.in",
}

# Coastal Maharashtra AOI used by the demo world (west, south, east, north).
MH_BBOX = (72.5, 15.5, 74.8, 20.3)
INDIA_BBOX = (68.0, 6.0, 98.0, 37.5)

SACHET_RSS = "https://sachet.ndma.gov.in/cap_public_website/rss/rss_india.xml"
SACHET_CAP = "https://sachet.ndma.gov.in/cap_public_website/FetchXMLFile"
SACHET_POLYGON = "https://sachet.ndma.gov.in/cap_public_website/FetchPolygonXMLFile"

IMD_API_BASE = "https://api.imd.gov.in/api/v1"
IMD_CAP_RSS = "https://cap-sources.s3.amazonaws.com/in-imd-en/rss.xml"
IMD_WIS2_COLLECTIONS = "https://wis2box.imd.gov.in/oapi/collections"

OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
USGS_QUERY = "https://earthquake.usgs.gov/fdsnws/event/1/query"
FIRMS_AREA = "https://firms.modaps.eosdis.nasa.gov/api/area/csv"
GDACS_RSS = "https://www.gdacs.org/xml/rss.xml"
ORS_DIRECTIONS = "https://api.openrouteservice.org/v2/directions/driving-car/geojson"

CAP_NS = {"cap": "urn:oasis:names:tc:emergency:cap:1.2"}
GDACS_NS = {
    "gdacs": "http://www.gdacs.org",
    "geo": "http://www.w3.org/2003/01/geo/wgs84_pos#",
    "georss": "http://www.georss.org/georss",
}

MH_KEYWORDS = (
    "maharashtra", "mumbai", "thane", "palghar", "raigad", "ratnagiri",
    "sindhudurg", "pune", "konkon", "konkan", "nashik", "kolhapur",
)


class FeedError(Exception):
    """Raised when a live feed cannot be retrieved or parsed."""


def _session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "*/*"})
    return session


def _verify_for(url: str, *, relaxed: bool = False):
    if relaxed:
        return False
    return certifi.where()


def _request(method: str, url: str, *, params=None, headers=None, json_body=None):
    """GET/POST with certifi first; relax SSL only for known IMD/SACHET hosts."""
    session = _session()
    host = urlparse(url).hostname or ""
    attempts = [False]
    if host in _RELAXED_SSL_HOSTS:
        attempts.append(True)
    last_error = None
    for relaxed in attempts:
        try:
            with urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning):
                response = session.request(
                    method,
                    url,
                    params=params,
                    headers=headers or {},
                    json=json_body,
                    timeout=REQUEST_TIMEOUT,
                    verify=_verify_for(url, relaxed=relaxed),
                )
            return response
        except requests.exceptions.SSLError as exc:
            last_error = exc
            continue
        except requests.RequestException as exc:
            raise FeedError(f"Network error contacting {url}: {exc}") from exc
    raise FeedError(f"SSL error contacting {url}: {last_error}") from last_error


def _get(url: str, *, params=None, headers=None, as_text=True) -> Any:
    response = _request("GET", url, params=params, headers=headers)
    if response.status_code == 401:
        raise FeedError("Unauthorized. Set the required API key in the environment.")
    if response.status_code == 403:
        raise FeedError("Access forbidden by the remote service.")
    if response.status_code >= 400:
        raise FeedError(f"HTTP {response.status_code} from {url}")
    return response.text if as_text else response.content


def _local_text(node, path: str, default: str = "", ns=None) -> str:
    found = node.find(path, ns) if ns else node.find(path)
    if found is None or found.text is None:
        return default
    return str(found.text).strip()


def _mentions_maharashtra(text: str) -> bool:
    lowered = (text or "").lower()
    return any(word in lowered for word in MH_KEYWORDS)


def _in_bbox(lat, lon, bbox=MH_BBOX) -> bool:
    try:
        lat_f = float(lat)
        lon_f = float(lon)
    except (TypeError, ValueError):
        return False
    west, south, east, north = bbox
    return south <= lat_f <= north and west <= lon_f <= east


def _parse_xml(xml_text: str, source: str) -> ET.Element:
    """Parse XML from an external feed. Malformed payloads become FeedError."""
    try:
        return ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise FeedError(f"{source} returned malformed XML.") from exc


def _parse_json_text(text: str, source: str) -> Any:
    """Parse JSON from an external feed. Malformed payloads become FeedError."""
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise FeedError(f"{source} returned malformed JSON.") from exc


def _parse_rss_items(xml_text: str, source: str = "RSS feed") -> list[ET.Element]:
    root = _parse_xml(xml_text, source)
    return list(root.findall("./channel/item"))


def imd_api_key() -> str:
    return (os.environ.get("IMD_API_KEY") or os.environ.get("IMD_API_TOKEN") or "").strip()


def firms_map_key() -> str:
    return (os.environ.get("NASA_FIRMS_MAP_KEY") or os.environ.get("FIRMS_MAP_KEY") or "").strip()


def ors_api_key() -> str:
    return (os.environ.get("OPENROUTESERVICE_API_KEY") or os.environ.get("ORS_API_KEY") or "").strip()


# ---------------------------------------------------------------------
# NDMA SACHET CAP
# ---------------------------------------------------------------------
def fetch_sachet_rss(limit: int = 25) -> list[dict]:
    """All-India SACHET CAP index. Returns RSS rows; CAP XML is fetched separately."""
    xml_text = _get(SACHET_RSS)
    rows = []
    for item in _parse_rss_items(xml_text, "NDMA SACHET CAP RSS")[: max(1, int(limit))]:
        title = _local_text(item, "title")
        link = _local_text(item, "link")
        guid = _local_text(item, "guid") or link.rsplit("identifier=", 1)[-1]
        pub = _local_text(item, "pubDate")
        rows.append({
            "id": guid,
            "title": title,
            "link": link,
            "published": pub,
            "source": "NDMA SACHET",
            "maharashtra": _mentions_maharashtra(title),
            "cap_url": f"{SACHET_CAP}?identifier={guid}" if guid else link,
            "polygon_url": f"{SACHET_POLYGON}?identifier={guid}" if guid else "",
        })
    return rows


def fetch_sachet_cap(identifier: str) -> dict:
    """Parse one CAP 1.2 XML alert, including AOI areaDesc / geocodes / polygon URL."""
    identifier = str(identifier or "").strip()
    if not identifier:
        raise FeedError("SACHET identifier is required.")
    xml_text = _get(SACHET_CAP, params={"identifier": identifier})
    if "<html" in xml_text.lower() and "cap:alert" not in xml_text:
        raise FeedError("SACHET returned a non-CAP response for that identifier.")
    root = _parse_xml(xml_text, "NDMA SACHET CAP XML")
    info = root.find("cap:info", CAP_NS)
    if info is None:
        info = root.find("info")
    area = info.find("cap:area", CAP_NS) if info is not None else None
    if area is None and info is not None:
        area = info.find("area")

    def info_text(tag: str) -> str:
        if info is None:
            return ""
        return _local_text(info, f"cap:{tag}", ns=CAP_NS) or _local_text(info, tag)

    geocodes = []
    polygon_url = ""
    area_desc = ""
    if area is not None:
        area_desc = _local_text(area, "cap:areaDesc", ns=CAP_NS) or _local_text(area, "areaDesc")
        for code in list(area.findall("cap:geocode", CAP_NS)) or list(area.findall("geocode")):
            name = _local_text(code, "cap:valueName", ns=CAP_NS) or _local_text(code, "valueName")
            value = _local_text(code, "cap:value", ns=CAP_NS) or _local_text(code, "value")
            if name or value:
                geocodes.append({"name": name, "value": value})
    if info is not None:
        for param in list(info.findall("cap:parameter", CAP_NS)) or list(info.findall("parameter")):
            name = _local_text(param, "cap:valueName", ns=CAP_NS) or _local_text(param, "valueName")
            value = _local_text(param, "cap:value", ns=CAP_NS) or _local_text(param, "value")
            if name.lower() == "polygon url":
                polygon_url = value

    return {
        "identifier": _local_text(root, "cap:identifier", ns=CAP_NS) or _local_text(root, "identifier") or identifier,
        "sender": _local_text(root, "cap:sender", ns=CAP_NS) or _local_text(root, "sender"),
        "sent": _local_text(root, "cap:sent", ns=CAP_NS) or _local_text(root, "sent"),
        "status": _local_text(root, "cap:status", ns=CAP_NS) or _local_text(root, "status"),
        "msg_type": _local_text(root, "cap:msgType", ns=CAP_NS) or _local_text(root, "msgType"),
        "event": info_text("event"),
        "headline": info_text("headline"),
        "severity": info_text("severity"),
        "urgency": info_text("urgency"),
        "certainty": info_text("certainty"),
        "instruction": info_text("instruction"),
        "effective": info_text("effective"),
        "expires": info_text("expires"),
        "area_desc": area_desc,
        "geocodes": geocodes,
        "polygon_url": polygon_url or f"{SACHET_POLYGON}?identifier={identifier}",
        "aoi_summary": area_desc or "No area description in CAP XML",
        "raw_xml_chars": len(xml_text),
    }


def fetch_sachet_aoi_polygon(identifier: str) -> dict:
    """Fetch the CAP polygon AOI XML referenced by SACHET."""
    identifier = str(identifier or "").strip()
    if not identifier:
        raise FeedError("SACHET identifier is required.")
    xml_text = _get(SACHET_POLYGON, params={"identifier": identifier})
    point_count = xml_text.count(",") + 1 if xml_text.strip() else 0
    preview = " ".join(xml_text.split())
    return {
        "identifier": identifier,
        "polygon_url": f"{SACHET_POLYGON}?identifier={identifier}",
        "point_estimate": max(0, point_count // 2),
        "preview": preview[:400] + ("…" if len(preview) > 400 else ""),
        "raw_xml_chars": len(xml_text),
    }


# ---------------------------------------------------------------------
# IMD API Gateway + CAP RSS + WIS2
# ---------------------------------------------------------------------
def fetch_imd_cap_rss(limit: int = 20) -> list[dict]:
    xml_text = _get(IMD_CAP_RSS)
    rows = []
    for item in _parse_rss_items(xml_text, "IMD CAP RSS")[: max(1, int(limit))]:
        title = _local_text(item, "title")
        rows.append({
            "title": title,
            "link": _local_text(item, "link"),
            "published": _local_text(item, "pubDate"),
            "source": "IMD CAP",
            "maharashtra": _mentions_maharashtra(title + " " + _local_text(item, "description")),
        })
    return rows


def fetch_imd_gateway_current(station_id: str | None = None) -> dict:
    """IMD API Gateway current weather. Requires IMD_API_KEY when the gateway enforces auth."""
    key = imd_api_key()
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    params = {"id": station_id} if station_id else None
    payload = _request(
        "GET",
        f"{IMD_API_BASE}/current_wx",
        params=params,
        headers=headers,
    )
    if payload.status_code == 401:
        raise FeedError(
            "IMD API Gateway returned 401. Set IMD_API_KEY to use api.imd.gov.in. "
            "Public IMD CAP RSS remains available without a key.")
    if payload.status_code >= 400:
        raise FeedError(f"IMD API Gateway HTTP {payload.status_code}")
    try:
        data = payload.json()
    except json.JSONDecodeError as exc:
        raise FeedError("IMD API Gateway returned malformed JSON.") from exc
    return {"endpoint": f"{IMD_API_BASE}/current_wx", "authenticated": bool(key), "data": data}


def fetch_imd_gateway_cityforecast(station_id: str = "43057") -> dict:
    """IMD city 7-day forecast via the official API gateway."""
    key = imd_api_key()
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    payload = _request(
        "GET",
        f"{IMD_API_BASE}/cityforecast",
        params={"id": station_id},
        headers=headers,
    )
    if payload.status_code == 401:
        raise FeedError(
            "IMD API Gateway returned 401 for cityforecast. Set IMD_API_KEY. "
            "Open-Meteo can still supply a zone forecast without a key.")
    if payload.status_code >= 400:
        raise FeedError(f"IMD cityforecast HTTP {payload.status_code}")
    try:
        data = payload.json()
    except json.JSONDecodeError as exc:
        raise FeedError("IMD cityforecast returned malformed JSON.") from exc
    return {"endpoint": f"{IMD_API_BASE}/cityforecast", "station_id": station_id,
            "authenticated": bool(key), "data": data}


def fetch_imd_wis2_collections(limit: int = 12) -> list[dict]:
    """Public IMD WIS2 OAPI collection catalogue (API gateway discovery surface)."""
    payload = _get(IMD_WIS2_COLLECTIONS, params={"f": "json"})
    body = _parse_json_text(payload, "IMD WIS2 OAPI")
    rows = []
    for collection in (body.get("collections") or [])[: max(1, int(limit))]:
        rows.append({
            "id": collection.get("id"),
            "title": collection.get("title") or collection.get("id"),
            "description": (collection.get("description") or "")[:180],
        })
    return rows


# ---------------------------------------------------------------------
# Open-Meteo
# ---------------------------------------------------------------------
def fetch_open_meteo(lat: float, lon: float, zone_name: str = "") -> dict:
    params = {
        "latitude": float(lat),
        "longitude": float(lon),
        "current": "temperature_2m,precipitation,wind_speed_10m,relative_humidity_2m",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,wind_speed_10m_max",
        "forecast_days": 3,
        "timezone": "Asia/Kolkata",
    }
    text = _get(OPEN_METEO, params=params)
    body = _parse_json_text(text, "Open-Meteo")
    current = body.get("current") or {}
    daily = body.get("daily") or {}
    days = []
    for index, day in enumerate(daily.get("time") or []):
        days.append({
            "date": day,
            "tmax": _safe_index(daily.get("temperature_2m_max"), index),
            "tmin": _safe_index(daily.get("temperature_2m_min"), index),
            "rain_mm": _safe_index(daily.get("precipitation_sum"), index),
            "wind_max": _safe_index(daily.get("wind_speed_10m_max"), index),
        })
    return {
        "zone": zone_name,
        "lat": lat,
        "lon": lon,
        "current_temp_c": current.get("temperature_2m"),
        "current_precip_mm": current.get("precipitation"),
        "current_wind": current.get("wind_speed_10m"),
        "current_humidity": current.get("relative_humidity_2m"),
        "observed_at": current.get("time"),
        "days": days,
        "source": "Open-Meteo",
    }


def _safe_index(values, index):
    if not values or index >= len(values):
        return None
    return values[index]


# ---------------------------------------------------------------------
# USGS earthquakes
# ---------------------------------------------------------------------
def fetch_usgs_earthquakes(bbox=INDIA_BBOX, min_magnitude: float = 2.5, hours: int = 72) -> list[dict]:
    west, south, east, north = bbox
    start = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S")
    params = {
        "format": "geojson",
        "starttime": start,
        "minmagnitude": float(min_magnitude),
        "minlatitude": south,
        "maxlatitude": north,
        "minlongitude": west,
        "maxlongitude": east,
        "orderby": "time",
    }
    body = _parse_json_text(_get(USGS_QUERY, params=params), "USGS earthquake API")
    rows = []
    for feature in body.get("features") or []:
        props = feature.get("properties") or {}
        coords = (feature.get("geometry") or {}).get("coordinates") or [None, None, None]
        lon, lat, depth = (coords + [None, None, None])[:3]
        rows.append({
            "id": feature.get("id"),
            "title": props.get("title") or props.get("place") or "Earthquake",
            "place": props.get("place"),
            "mag": props.get("mag"),
            "time": datetime.fromtimestamp((props.get("time") or 0) / 1000, tz=timezone.utc).isoformat()
            if props.get("time") else "",
            "url": props.get("url"),
            "lat": lat,
            "lon": lon,
            "depth_km": depth,
            "label": f"M{props.get('mag')} {props.get('place') or ''}".strip(),
            "source": "USGS",
        })
    return rows


def earthquake_markers(rows: list[dict] | None = None) -> list[dict]:
    rows = rows if rows is not None else fetch_usgs_earthquakes()
    return [{
        "lat": row["lat"], "lon": row["lon"],
        "label": row["label"], "mag": row.get("mag"), "status": "QUAKE",
    } for row in rows if row.get("lat") is not None and row.get("lon") is not None]


# ---------------------------------------------------------------------
# NASA FIRMS
# ---------------------------------------------------------------------
def fetch_firms_fires(bbox=MH_BBOX, day_range: int = 1, source: str = "VIIRS_SNPP_NRT") -> list[dict]:
    key = firms_map_key()
    if not key:
        raise FeedError(
            "NASA FIRMS MAP_KEY is not set. Export NASA_FIRMS_MAP_KEY after registering at "
            "https://firms.modaps.eosdis.nasa.gov/api/map_key/")
    west, south, east, north = bbox
    area = f"{west},{south},{east},{north}"
    url = f"{FIRMS_AREA}/{key}/{source}/{area}/{int(day_range)}"
    text = _get(url)
    if text.strip().lower().startswith("<!doctype") or "Invalid" in text[:200]:
        raise FeedError("FIRMS returned an error payload. Check NASA_FIRMS_MAP_KEY.")
    reader = csv.DictReader(io.StringIO(text))
    rows = []
    for raw in reader:
        try:
            lat = float(raw.get("latitude"))
            lon = float(raw.get("longitude"))
        except (TypeError, ValueError):
            continue
        rows.append({
            "lat": lat,
            "lon": lon,
            "brightness": raw.get("bright_ti4") or raw.get("brightness"),
            "frp": raw.get("frp"),
            "acq_date": raw.get("acq_date"),
            "acq_time": raw.get("acq_time"),
            "confidence": raw.get("confidence"),
            "satellite": raw.get("satellite") or source,
            "label": f"Fire {raw.get('acq_date')} {raw.get('acq_time')} FRP {raw.get('frp')}",
            "source": "NASA FIRMS",
        })
    return rows


def fire_markers(rows: list[dict] | None = None) -> list[dict]:
    if rows is None:
        rows = fetch_firms_fires()
    return [{
        "lat": row["lat"], "lon": row["lon"],
        "label": row["label"], "status": "FIRE",
    } for row in rows]


# ---------------------------------------------------------------------
# GDACS
# ---------------------------------------------------------------------
def fetch_gdacs(limit: int = 40, india_focus: bool = True) -> list[dict]:
    xml_text = _get(GDACS_RSS)
    root = _parse_xml(xml_text, "GDACS RSS")
    rows = []
    for item in root.findall("./channel/item"):
        title = _local_text(item, "title")
        point = _local_text(item, "georss:point", ns=GDACS_NS) or _local_text(item, "gdacs:point", ns=GDACS_NS)
        lat = lon = None
        if point:
            parts = point.split()
            if len(parts) >= 2:
                try:
                    lat, lon = float(parts[0]), float(parts[1])
                except ValueError:
                    lat = lon = None
        country = _local_text(item, "gdacs:country", ns=GDACS_NS)
        iso3 = _local_text(item, "gdacs:iso3", ns=GDACS_NS)
        event_type = _local_text(item, "gdacs:eventtype", ns=GDACS_NS)
        alert_level = _local_text(item, "gdacs:alertlevel", ns=GDACS_NS)
        row = {
            "title": title,
            "link": _local_text(item, "link"),
            "published": _local_text(item, "pubDate"),
            "country": country,
            "iso3": iso3,
            "event_type": event_type,
            "alert_level": alert_level,
            "severity": _local_text(item, "gdacs:severity", ns=GDACS_NS),
            "lat": lat,
            "lon": lon,
            "cap_url": _local_text(item, "gdacs:cap", ns=GDACS_NS),
            "label": f"GDACS {alert_level} {event_type}: {title}"[:120],
            "source": "GDACS",
        }
        if india_focus:
            keep = (
                iso3 == "IND"
                or "india" in (country or "").lower()
                or _in_bbox(lat, lon, INDIA_BBOX)
                or _mentions_maharashtra(title)
            )
            if not keep:
                continue
        rows.append(row)
        if len(rows) >= limit:
            break
    if not rows and india_focus:
        # Fall back to the global top items so the panel is never empty solely due to filters.
        return fetch_gdacs(limit=min(limit, 15), india_focus=False)
    return rows


def gdacs_markers(rows: list[dict] | None = None) -> list[dict]:
    rows = rows if rows is not None else fetch_gdacs()
    return [{
        "lat": row["lat"], "lon": row["lon"],
        "label": row["label"], "status": row.get("alert_level") or "GDACS",
    } for row in rows if row.get("lat") is not None and row.get("lon") is not None]


# ---------------------------------------------------------------------
# OpenRouteService
# ---------------------------------------------------------------------
def fetch_ors_directions(lon1: float, lat1: float, lon2: float, lat2: float) -> dict:
    key = ors_api_key()
    if not key:
        raise FeedError(
            "OpenRouteService API key is not set. Export OPENROUTESERVICE_API_KEY from "
            "https://openrouteservice.org/dev/#/signup")
    headers = {
        "Authorization": key,
        "Content-Type": "application/json",
    }
    body = {"coordinates": [[float(lon1), float(lat1)], [float(lon2), float(lat2)]]}
    response = _request("POST", ORS_DIRECTIONS, headers=headers, json_body=body)
    if response.status_code == 401:
        raise FeedError("OpenRouteService rejected the API key (401).")
    if response.status_code >= 400:
        raise FeedError(f"OpenRouteService HTTP {response.status_code}: {response.text[:180]}")
    try:
        payload = response.json()
    except json.JSONDecodeError as exc:
        raise FeedError("OpenRouteService returned malformed JSON.") from exc
    features = payload.get("features") or []
    if not features:
        raise FeedError("OpenRouteService returned no route.")
    props = features[0].get("properties") or {}
    summary = (props.get("summary") or {})
    segments = props.get("segments") or []
    steps = []
    if segments:
        for step in (segments[0].get("steps") or [])[:8]:
            steps.append({
                "instruction": step.get("instruction"),
                "distance_m": step.get("distance"),
                "duration_s": step.get("duration"),
            })
    geometry = features[0].get("geometry") or {}
    return {
        "distance_km": round(float(summary.get("distance") or 0) / 1000.0, 2),
        "duration_min": round(float(summary.get("duration") or 0) / 60.0, 1),
        "steps": steps,
        "coordinates": geometry.get("coordinates") or [],
        "source": "OpenRouteService",
    }


# ---------------------------------------------------------------------
# Convenience bundle for dashboard health strip
# ---------------------------------------------------------------------
def key_status() -> dict:
    return {
        "imd_api_key": bool(imd_api_key()),
        "firms_map_key": bool(firms_map_key()),
        "ors_api_key": bool(ors_api_key()),
    }
