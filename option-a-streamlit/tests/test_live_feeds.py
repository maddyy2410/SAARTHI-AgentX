"""Unit checks for live_feeds helpers. Network calls are optional and skipped when offline."""

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree as ET

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import live_feeds  # noqa: E402


SAMPLE_CAP = """<?xml version="1.0" encoding="UTF-8"?>
<cap:alert xmlns:cap="urn:oasis:names:tc:emergency:cap:1.2">
  <cap:identifier>IN-TEST_1</cap:identifier>
  <cap:sender>Test-SDMA</cap:sender>
  <cap:sent>2026-11-11T12:00:00+05:30</cap:sent>
  <cap:status>Actual</cap:status>
  <cap:msgType>Alert</cap:msgType>
  <cap:info>
    <cap:event>Heavy Rain</cap:event>
    <cap:severity>Severe</cap:severity>
    <cap:urgency>Immediate</cap:urgency>
    <cap:certainty>Likely</cap:certainty>
    <cap:headline>Heavy rain over Ratnagiri</cap:headline>
    <cap:parameter>
      <cap:valueName>Polygon URL</cap:valueName>
      <cap:value>https://example.test/polygon</cap:value>
    </cap:parameter>
    <cap:area>
      <cap:areaDesc>Ratnagiri district of Maharashtra</cap:areaDesc>
      <cap:geocode>
        <cap:valueName>LGD District Code</cap:valueName>
        <cap:value>528</cap:value>
      </cap:geocode>
    </cap:area>
  </cap:info>
</cap:alert>
"""


def test_key_status_booleans():
    status = live_feeds.key_status()
    assert set(status) == {"imd_api_key", "firms_map_key", "ors_api_key"}
    assert all(isinstance(status[key], bool) for key in status)


def test_maharashtra_keyword_filter():
    assert live_feeds._mentions_maharashtra("Alert for Ratnagiri coast")
    assert not live_feeds._mentions_maharashtra("Alert for Tripura")


def test_bbox_helper():
    assert live_feeds._in_bbox(16.99, 73.31, live_feeds.MH_BBOX)
    assert not live_feeds._in_bbox(28.61, 77.20, live_feeds.MH_BBOX)


def test_cap_xml_local_parse_helpers(monkeypatch):
    def fake_get(url, *, params=None, headers=None, as_text=True):
        assert "FetchXMLFile" in url or params
        return SAMPLE_CAP

    monkeypatch.setattr(live_feeds, "_get", fake_get)
    parsed = live_feeds.fetch_sachet_cap("1794393210079015")
    assert parsed["event"] == "Heavy Rain"
    assert parsed["severity"] == "Severe"
    assert "Ratnagiri" in parsed["aoi_summary"]
    assert parsed["geocodes"][0]["value"] == "528"
    assert parsed["polygon_url"] == "https://example.test/polygon"


def test_rss_item_parse():
    xml_text = """<?xml version="1.0"?>
    <rss version="2.0"><channel>
      <item><title>Flood watch Mumbai</title><link>http://x/1</link>
      <guid>99</guid><pubDate>Wed, 11 Nov 2026 10:00:00 GMT</pubDate></item>
    </channel></rss>"""
    items = live_feeds._parse_rss_items(xml_text, "test RSS")
    assert len(items) == 1
    assert ET.tostring(items[0]).decode().find("Mumbai") >= 0


def test_malformed_sachet_rss_raises_feed_error(monkeypatch):
    monkeypatch.setattr(live_feeds, "_get", lambda *a, **k: "<not-xml")
    with pytest.raises(live_feeds.FeedError, match="NDMA SACHET CAP RSS"):
        live_feeds.fetch_sachet_rss(limit=3)


def test_malformed_sachet_cap_xml_raises_feed_error(monkeypatch):
    monkeypatch.setattr(live_feeds, "_get", lambda *a, **k: "<alert>broken")
    with pytest.raises(live_feeds.FeedError, match="NDMA SACHET CAP XML"):
        live_feeds.fetch_sachet_cap("123")


def test_malformed_imd_cap_rss_raises_feed_error(monkeypatch):
    monkeypatch.setattr(live_feeds, "_get", lambda *a, **k: "<<<<")
    with pytest.raises(live_feeds.FeedError, match="IMD CAP RSS"):
        live_feeds.fetch_imd_cap_rss(limit=3)


def test_malformed_gdacs_xml_raises_feed_error(monkeypatch):
    monkeypatch.setattr(live_feeds, "_get", lambda *a, **k: "<rss><channel><item>")
    with pytest.raises(live_feeds.FeedError, match="GDACS RSS"):
        live_feeds.fetch_gdacs(limit=3, india_focus=False)


def test_malformed_open_meteo_json_raises_feed_error(monkeypatch):
    monkeypatch.setattr(live_feeds, "_get", lambda *a, **k: "{not-json")
    with pytest.raises(live_feeds.FeedError, match="Open-Meteo"):
        live_feeds.fetch_open_meteo(16.99, 73.31, "Ratnagiri")


def test_malformed_usgs_json_raises_feed_error(monkeypatch):
    monkeypatch.setattr(live_feeds, "_get", lambda *a, **k: "[]oops")
    with pytest.raises(live_feeds.FeedError, match="USGS earthquake API"):
        live_feeds.fetch_usgs_earthquakes()


def test_malformed_imd_wis2_json_raises_feed_error(monkeypatch):
    monkeypatch.setattr(live_feeds, "_get", lambda *a, **k: "{")
    with pytest.raises(live_feeds.FeedError, match="IMD WIS2 OAPI"):
        live_feeds.fetch_imd_wis2_collections()


def test_feed_error_preserves_parse_cause(monkeypatch):
    monkeypatch.setattr(live_feeds, "_get", lambda *a, **k: "<bad")
    with pytest.raises(live_feeds.FeedError) as raised:
        live_feeds.fetch_sachet_rss()
    assert isinstance(raised.value.__cause__, ET.ParseError)


def test_ui_facing_feed_error_can_be_caught_like_app_panels(monkeypatch):
    """Mirrors Early Warnings / map handlers: only FeedError is expected."""
    monkeypatch.setattr(live_feeds, "_get", lambda *a, **k: "not-xml")
    try:
        live_feeds.fetch_gdacs(limit=1)
        assert False, "expected FeedError"
    except live_feeds.FeedError as exc:
        assert "GDACS" in str(exc)
    # Successful path still returns rows when XML is valid.
    good = """<?xml version="1.0"?><rss version="2.0"><channel>
      <item><title>Green EQ India</title><link>http://x</link>
      <gdacs:iso3 xmlns:gdacs="http://www.gdacs.org">IND</gdacs:iso3>
      <gdacs:eventtype xmlns:gdacs="http://www.gdacs.org">EQ</gdacs:eventtype>
      <gdacs:alertlevel xmlns:gdacs="http://www.gdacs.org">Green</gdacs:alertlevel>
      <gdacs:country xmlns:gdacs="http://www.gdacs.org">India</gdacs:country>
      <georss:point xmlns:georss="http://www.georss.org/georss">19.0 73.0</georss:point>
      </item></channel></rss>"""
    monkeypatch.setattr(live_feeds, "_get", lambda *a, **k: good)
    rows = live_feeds.fetch_gdacs(limit=1, india_focus=True)
    assert rows and rows[0]["iso3"] == "IND"


def _response_with_bad_json(status_code=200):
    def bad_json():
        raise json.JSONDecodeError("Expecting value", "doc", 0)

    return SimpleNamespace(status_code=status_code, text="not-json", json=bad_json)


def test_malformed_imd_gateway_current_json_raises_feed_error(monkeypatch):
    monkeypatch.setattr(live_feeds, "_request", lambda *a, **k: _response_with_bad_json())
    with pytest.raises(live_feeds.FeedError, match="IMD API Gateway returned malformed JSON") as raised:
        live_feeds.fetch_imd_gateway_current()
    assert isinstance(raised.value.__cause__, json.JSONDecodeError)


def test_malformed_imd_gateway_cityforecast_json_raises_feed_error(monkeypatch):
    monkeypatch.setattr(live_feeds, "_request", lambda *a, **k: _response_with_bad_json())
    with pytest.raises(live_feeds.FeedError, match="IMD cityforecast returned malformed JSON") as raised:
        live_feeds.fetch_imd_gateway_cityforecast("43057")
    assert isinstance(raised.value.__cause__, json.JSONDecodeError)


def test_malformed_ors_directions_json_raises_feed_error(monkeypatch):
    monkeypatch.setenv("OPENROUTESERVICE_API_KEY", "test-ors-key")
    monkeypatch.setattr(live_feeds, "_request", lambda *a, **k: _response_with_bad_json())
    with pytest.raises(live_feeds.FeedError, match="OpenRouteService returned malformed JSON") as raised:
        live_feeds.fetch_ors_directions(73.85, 18.52, 73.31, 16.99)
    assert isinstance(raised.value.__cause__, json.JSONDecodeError)


def test_public_feeds_smoke():
    """Live smoke test for public endpoints. Skipped unless RUN_LIVE_FEED_TESTS=1."""
    if os.environ.get("RUN_LIVE_FEED_TESTS") != "1":
        pytest.skip("Set RUN_LIVE_FEED_TESTS=1 to hit live endpoints")
    rows = live_feeds.fetch_sachet_rss(limit=3)
    assert rows and rows[0]["id"]
    forecast = live_feeds.fetch_open_meteo(16.99, 73.31, "Ratnagiri")
    assert "current_temp_c" in forecast
    quakes = live_feeds.fetch_usgs_earthquakes()
    assert isinstance(quakes, list)
    gdacs = live_feeds.fetch_gdacs(limit=5, india_focus=False)
    assert gdacs
