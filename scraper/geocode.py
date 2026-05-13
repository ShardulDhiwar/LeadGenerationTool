"""
scraper/geocode.py
──────────────────
Resolves a city / area name to (lat, lng) using the free
Nominatim OpenStreetMap API — no API key required.

Used by all scrapers to calculate distance_km automatically:
  start  = geocode("Pune")          → (18.5204, 73.8567)
  doctor = (lat, lng from scraper)  → e.g. (18.512, 73.853)
  distance_km = haversine(start, doctor)

Results are cached in-memory so the same city/area is only
geocoded once per run.
"""

from __future__ import annotations
import math
import urllib.request
import urllib.parse
import json
import time

# ── in-memory cache: "Pune" → (18.5204, 73.8567) ─────────────────────────────
_CACHE: dict[str, tuple[float, float] | None] = {}

# Nominatim requires a unique User-Agent identifying your app
_USER_AGENT = "LeadGenerationTool/1.0 (medical-rep-scraper)"

# Rate-limit: Nominatim allows max 1 request/second
_LAST_CALL  = 0.0


def geocode(location: str) -> tuple[float, float] | None:
    """
    Geocode a place name to (lat, lng).

    Uses OSM Nominatim — free, no key, 1 req/sec limit.
    Returns None if the location could not be resolved.

    Examples:
        geocode("Pune")            → (18.5204, 73.8567)
        geocode("Baner, Pune")     → (18.5590, 73.7868)
        geocode("Koregaon Park")   → (18.5362, 73.8938)
    """
    global _LAST_CALL

    key = location.strip().lower()
    if key in _CACHE:
        return _CACHE[key]

    # Nominatim rate-limit: enforce 1 req/sec
    elapsed = time.time() - _LAST_CALL
    if elapsed < 1.1:
        time.sleep(1.1 - elapsed)

    params = urllib.parse.urlencode({
        "q":      location,
        "format": "json",
        "limit":  1,
    })
    url = f"https://nominatim.openstreetmap.org/search?{params}"

    try:
        req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())
        _LAST_CALL = time.time()

        if data:
            result = (float(data[0]["lat"]), float(data[0]["lon"]))
            print(f"  [GEO] '{location}' → {result}")
            _CACHE[key] = result
            return result
        else:
            print(f"  [GEO] '{location}' → not found")
            _CACHE[key] = None
            return None

    except Exception as e:
        print(f"  [GEO] '{location}' → error: {e}")
        _CACHE[key] = None
        return None


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Straight-line distance in km between two lat/lng points."""
    R = 6_371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(dlon / 2) ** 2
    )
    return R * 2 * math.asin(math.sqrt(a))


def distance_from(
    origin_lat: float,
    origin_lng: float,
    dest_lat: str | float,
    dest_lng: str | float,
) -> float | str:
    """
    Calculate km distance from an origin point to a destination.

    dest_lat / dest_lng can be strings (as returned by the scraper)
    or floats. Returns "N/A" if either is missing.
    """
    if dest_lat == "N/A" or dest_lng == "N/A":
        return "N/A"
    try:
        return round(haversine(origin_lat, origin_lng, float(dest_lat), float(dest_lng)), 2)
    except (ValueError, TypeError):
        return "N/A"