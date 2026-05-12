"""
scraper/google_maps.py
──────────────────────
Phase 1 — Full detail extraction from Google Maps.

What we extract per listing:
  ✅ name
  ✅ rating (stars + review count)
  ✅ address
  ✅ phone
  ✅ website
  ✅ latitude / longitude  (from !3d / !4d data params in panel URL — exact pin)
  ✅ specialty  (search term used)
  ✅ city / area
  ✅ distance_km  (haversine from START point → listing pin)

Coordinate extraction strategy:
  Pattern B (exact):   ...!3d18.52715!4d73.85530...   ← preferred
  Pattern A (approx):  .../@18.5271,73.8553,17z/...   ← fallback

Distance calculation:
  - Start point = Cardiologist in Pune (lat/lng scraped from their Maps listing)
  - Destination = Each searched place   (lat/lng scraped from their Maps listing)
  - Formula     = Haversine (straight-line, no API needed)

Distance guard priority:
  1. start_point coords  (lat/lng of the cardiologist listing)
  2. area coords         (e.g. "Koregaon Park" in AREA_COORDS) — fallback
  3. city coords         (e.g. "Pune" in AREA_COORDS)          — fallback
  4. no filter           (none available — distance stays "N/A")
"""

from __future__ import annotations

import asyncio
import math
import os
import re
import sys

# ── bring config into path ────────────────────────────────────────────────────
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    from config.areas_coords import AREA_COORDS, DEFAULT_ZOOM
    print(f"[CFG] Loaded AREA_COORDS with {len(AREA_COORDS)} entries: {list(AREA_COORDS.keys())}")
except ImportError:
    AREA_COORDS: dict[str, tuple[float, float]] = {}
    DEFAULT_ZOOM: int = 12
    print("[CFG] WARNING: config/areas_coords.py not found — distance filter disabled")

from .base_scraper import BaseScraper
from .utils import (
    parse_rating,
    save_json,
    save_csv,
    scrape_phone_from_website,
)


# ─────────────────────────────────────────────────────────────────────────────
# Geometry helpers
# ─────────────────────────────────────────────────────────────────────────────

def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
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


def _zoom_radius(zoom: int) -> float:
    """Approximate visible radius in km for a given Google Maps zoom level."""
    return {14: 3.0, 13: 6.0, 12: 12.0, 11: 22.0, 10: 40.0}.get(zoom, 12.0)


def distance_between_listings(place_a: dict, place_b: dict) -> float | str:
    """
    Calculate straight-line distance (km) between two scraped listings
    using their lat/lng already present in the JSON. No API needed.

    place_a : start point  (e.g. the Cardiologist listing)
    place_b : destination  (e.g. the searched place listing)
    """
    try:
        lat1 = float(place_a["latitude"])
        lng1 = float(place_a["longitude"])
        lat2 = float(place_b["latitude"])
        lng2 = float(place_b["longitude"])
    except (ValueError, KeyError, TypeError):
        return "N/A"
    return round(_haversine(lat1, lng1, lat2, lng2), 2)


# ─────────────────────────────────────────────────────────────────────────────
# Coordinate extraction
# ─────────────────────────────────────────────────────────────────────────────

def extract_listing_coords(url: str) -> tuple[str, str]:
    """
    Extract the listing's lat/lng from a Google Maps detail panel URL.

    Pattern B — exact pin (preferred):
        ...!3d18.52715!4d73.85530...

    Pattern A — viewport centre (fallback, ±200 m):
        .../@18.5271,73.8553,17z/...

    Returns (lat_str, lng_str) or ("N/A", "N/A").
    """
    # Pattern B: !3d<lat>!4d<lng>
    m = re.search(r'!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)', url)
    if m:
        return m.group(1), m.group(2)

    # Pattern A: @<lat>,<lng>,<zoom>z  (zoom may be decimal e.g. 17.5z)
    m = re.search(r'@(-?\d+\.\d+),(-?\d+\.\d+),\d+(?:\.\d+)?z', url)
    if m:
        return m.group(1), m.group(2)

    return "N/A", "N/A"


def _debug_coords(url: str) -> None:
    """
    Print every regex attempt so we can see exactly why coords fail.
    Called only when extract_listing_coords returns ("N/A", "N/A").
    """
    print(f"\n    [DEBUG-URL] Full URL:\n      {url}")

    m = re.search(r'!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)', url)
    print(f"    [DEBUG] Pattern B (!3d/!4d)      : {'MATCH → ' + m.group(0) if m else 'NO MATCH'}")

    m = re.search(r'@(-?\d+\.\d+),(-?\d+\.\d+),\d+(?:\.\d+)?z', url)
    print(f"    [DEBUG] Pattern A (@lat,lng,zoom) : {'MATCH → ' + m.group(0) if m else 'NO MATCH'}")


# ─────────────────────────────────────────────────────────────────────────────
# Main scraper class
# ─────────────────────────────────────────────────────────────────────────────

class GoogleMapsScraper(BaseScraper):

    # ── selectors ─────────────────────────────────────────────────────────────
    LISTING_SELECTOR = '//div[@role="article"]'
    NAME_LINK_SEL    = "a.hfpxzc"
    RATING_SEL       = 'span[aria-label*="stars"]'

    PHONE_SEL   = 'button[data-item-id^="phone"]'
    ADDRESS_SEL = 'button[data-item-id="address"]'
    WEBSITE_SEL = 'a[data-item-id="authority"]'

    def __init__(
        self,
        headless: bool = False,
        slow_mo: int = 80,
        scroll_rounds: int = 6,
        max_listings: int = 20,
        zoom: int | None = None,
    ) -> None:
        super().__init__(headless=headless, slow_mo=slow_mo)
        self.scroll_rounds = scroll_rounds
        self.max_listings  = max_listings
        self.zoom          = zoom

    # ─────────────────────────────────────────────────────────────────────────
    # Public entry point
    # ─────────────────────────────────────────────────────────────────────────

    async def scrape(
        self,
        specialty: str = "dentist",
        city: str = "Pune",
        area: str = "",
        output_dir: str = "outputs",
        start_point: dict | None = None,   # ← Cardiologist listing dict (has lat/lng)
    ) -> list[dict]:
        """
        Scrape Google Maps for a given specialty + location.

        start_point : optional dict with 'latitude' and 'longitude' keys.
                      When provided (e.g. the Cardiologist listing),
                      distance_km is measured from that pin to each result.
                      Falls back to area/city centre if not provided.
        """
        query = f"{specialty} in {city} {area}".strip()

        # ── startup diagnostic ─────────────────────────────────────────────
        print(f"\n[SEARCH] Query          : {query}")
        print(f"[SEARCH] city           : '{city}'  (in AREA_COORDS: {city in AREA_COORDS})")
        print(f"[SEARCH] area           : '{area}'  (in AREA_COORDS: {area in AREA_COORDS if area else 'N/A — no area given'})")

        if start_point:
            print(
                f"[SEARCH] start_point    : ({start_point.get('latitude')}, "
                f"{start_point.get('longitude')})  ← distances measured from here"
            )
        elif not AREA_COORDS:
            print("[SEARCH] WARNING: AREA_COORDS is empty — distance will always be N/A")
        elif city not in AREA_COORDS and (not area or area not in AREA_COORDS):
            print(
                f"[SEARCH] WARNING: Neither '{city}' nor '{area}' found in AREA_COORDS.\n"
                f"         Distance will be N/A for all listings.\n"
                f"         Known keys: {list(AREA_COORDS.keys())}"
            )

        await self._navigate_to_maps(query, area, city)
        await self._scroll_results()

        listings = await self.page.query_selector_all(self.LISTING_SELECTOR)
        print(
            f"\n[INFO] Found {len(listings)} listing elements "
            f"(capping at {self.max_listings})"
        )
        listings = listings[: self.max_listings]

        leads: list[dict] = []
        for i, listing in enumerate(listings, 1):
            print(f"\n  [{i}/{len(listings)}] ── Extracting ──────────────────────")
            lead = await self._extract_listing(
                listing, specialty, city, area, start_point=start_point
            )
            if lead:
                leads.append(lead)
                print(f"  ✓ {lead['name']}  |  dist={lead['distance_km']} km")
            else:
                print("  ✗ skipped")

        slug = f"{specialty}_{city}_{area}".replace(" ", "_").strip("_")
        os.makedirs(output_dir, exist_ok=True)
        save_json(leads, f"{output_dir}/{slug}.json")
        save_csv(leads,  f"{output_dir}/{slug}.csv")

        print(f"\n[DONE] {len(leads)} leads scraped for '{query}'")
        return leads

    # ─────────────────────────────────────────────────────────────────────────
    # Navigation
    # ─────────────────────────────────────────────────────────────────────────

    async def _navigate_to_maps(
        self, query: str, area: str = "", city: str = ""
    ) -> None:
        """
        Navigate to Google Maps pre-centred on area (or city) coords,
        then fire the search query.
        """
        zoom = self.zoom or DEFAULT_ZOOM

        # Resolve best reference point
        ref_key    = None
        ref_coords = None

        if area and area in AREA_COORDS:
            ref_key    = area
            ref_coords = AREA_COORDS[area]
        elif city and city in AREA_COORDS:
            ref_key    = city
            ref_coords = AREA_COORDS[city]

        if ref_coords:
            lat, lng = ref_coords
            maps_url = f"https://www.google.com/maps/@{lat},{lng},{zoom}z"
            print(
                f"[GPS] Centre on '{ref_key}' → ({lat}, {lng}), "
                f"zoom={zoom} (~{_zoom_radius(zoom)} km radius)"
            )
            await self.page.goto(maps_url)
        else:
            print(
                f"[NAV] No coords for area='{area}' or city='{city}' in AREA_COORDS\n"
                f"      → Opening Maps without pre-centering.\n"
                f"      → Add an entry to config/areas_coords.py to enable "
                f"distance filtering."
            )
            await self.page.goto("https://www.google.com/maps")

        await self.page.wait_for_timeout(3_000)

        print("[KEY] Typing search query...")
        await self.page.keyboard.type(query, delay=80)
        await self.page.keyboard.press("Enter")

        await self.page.wait_for_selector(
            '//div[@role="feed"]', timeout=15_000
        )
        await self.page.wait_for_timeout(3_000)

    # ─────────────────────────────────────────────────────────────────────────
    # Scrolling
    # ─────────────────────────────────────────────────────────────────────────

    async def _scroll_results(self) -> None:
        """Scroll the results panel to load more listings."""
        print(f"[SCROLL] Scrolling results ({self.scroll_rounds} rounds)...")
        feed = await self.page.query_selector('//div[@role="feed"]')

        if not feed:
            for _ in range(self.scroll_rounds):
                await self.page.mouse.wheel(0, 5_000)
                await self.page.wait_for_timeout(2_000)
            return

        for _ in range(self.scroll_rounds):
            await feed.evaluate("el => el.scrollBy(0, 2000)")
            await self.page.wait_for_timeout(2_000)

    # ─────────────────────────────────────────────────────────────────────────
    # Per-listing extraction
    # ─────────────────────────────────────────────────────────────────────────

    async def _extract_listing(
        self,
        listing,
        specialty: str,
        city: str,
        area: str,
        start_point: dict | None = None,   # ← Cardiologist listing dict
    ) -> dict | None:
        try:
            # ── name ──────────────────────────────────────────────────────
            a_tag = await listing.query_selector(self.NAME_LINK_SEL)
            name  = await a_tag.get_attribute("aria-label") if a_tag else None
            if not name:
                return None

            # ── rating ────────────────────────────────────────────────────
            rating_raw = "N/A"
            rating_el  = await listing.query_selector(self.RATING_SEL)
            if rating_el:
                rating_raw = await rating_el.get_attribute("aria-label") or "N/A"
            stars, reviews = parse_rating(rating_raw)

            # ── click → detail panel ──────────────────────────────────────
            await listing.click()
            await self.page.wait_for_timeout(3_000)

            # ── contact details ───────────────────────────────────────────
            phone   = await self._get_phone()
            address = await self._get_address()
            website = await self._get_website()

            # ── phone fallback ────────────────────────────────────────────
            phone_source = "google_maps"
            if phone == "N/A" and website != "N/A":
                print(f"    [WEB] No phone on Maps — trying website...", end=" ")
                phone = await scrape_phone_from_website(self.page, website)
                if phone != "N/A":
                    phone_source = "website"
                    print(f"found: {phone}")
                else:
                    phone_source = "not_found"
                    print("not found")

            # ── coordinates ───────────────────────────────────────────────
            # Extra wait: Maps sometimes writes !3d/!4d into the URL
            # ~500 ms after the panel finishes rendering.
            await self.page.wait_for_timeout(1_500)
            current_url = self.page.url
            lat, lng    = extract_listing_coords(current_url)

            if lat == "N/A":
                print(f"    [WARN] Coords not found for '{name}'")
                _debug_coords(current_url)

            # ── distance calculation ───────────────────────────────────────
            # Priority:
            #   1. start_point lat/lng  (Cardiologist listing — most accurate)
            #   2. area coords          (AREA_COORDS fallback)
            #   3. city coords          (AREA_COORDS fallback)
            #   4. N/A                  (nothing available)

            center_coords: tuple[float, float] | None = None
            center_label  = ""

            if start_point and start_point.get("latitude") not in (None, "N/A") \
                           and start_point.get("longitude") not in (None, "N/A"):
                center_coords = (float(start_point["latitude"]), float(start_point["longitude"]))
                center_label  = start_point.get("name", "start_point")
            elif area and area in AREA_COORDS:
                center_coords = AREA_COORDS[area]
                center_label  = area
            elif city and city in AREA_COORDS:
                center_coords = AREA_COORDS[city]
                center_label  = city

            # ── verbose distance debug ─────────────────────────────────────
            print(f"    [DIST] name         : {name}")
            print(f"    [DIST] lat/lng      : {lat}, {lng}")
            print(f"    [DIST] center       : '{center_label}' → {center_coords}")

            distance_km: float | str = "N/A"

            if center_coords is None:
                print(
                    f"    [DIST] RESULT: N/A\n"
                    f"           Reason: no start_point provided and neither\n"
                    f"                   area='{area}' nor city='{city}'\n"
                    f"                   found in AREA_COORDS.\n"
                    f"           Fix   : pass start_point=<cardiologist_dict> to scrape(),\n"
                    f"                   or add to config/areas_coords.py:\n"
                    f"                     AREA_COORDS['{city}'] = (<lat>, <lng>)"
                )
            elif lat == "N/A":
                print(
                    f"    [DIST] RESULT: N/A\n"
                    f"           Reason: couldn't parse coordinates from Maps URL.\n"
                    f"                   See [DEBUG-URL] above for the raw URL."
                )
            else:
                dist   = _haversine(
                    center_coords[0], center_coords[1],
                    float(lat), float(lng),
                )
                max_km = _zoom_radius(self.zoom or DEFAULT_ZOOM)
                print(f"    [DIST] haversine    : {dist:.3f} km  (limit {max_km} km)")

                if dist > max_km:
                    print(f"    [DIST] RESULT: SKIPPED — {dist:.1f} km > {max_km} km limit")
                    return None

                distance_km = round(dist, 2)
                print(f"    [DIST] RESULT: {distance_km} km ✓")

            return {
                "name":         name,
                "specialty":    specialty,
                "city":         city,
                "area":         area,
                "stars":        stars,
                "reviews":      reviews,
                "address":      address,
                "phone":        phone,
                "phone_source": phone_source,
                "website":      website,
                "latitude":     lat,
                "longitude":    lng,
                "distance_km":  distance_km,
                "maps_url":     current_url,
            }

        except Exception as exc:
            print(f"\n    [ERR] {exc}")
            return None

    # ─────────────────────────────────────────────────────────────────────────
    # Detail-panel field extractors
    # ─────────────────────────────────────────────────────────────────────────

    async def _get_phone(self) -> str:
        try:
            btn = await self.page.wait_for_selector(self.PHONE_SEL, timeout=3_000)
            if btn:
                aria  = await btn.get_attribute("aria-label") or ""
                phone = re.sub(r"^[Pp]hone:\s*", "", aria).strip()
                return phone if phone else "N/A"
        except Exception:
            pass
        return "N/A"

    async def _get_address(self) -> str:
        try:
            btn = await self.page.wait_for_selector(self.ADDRESS_SEL, timeout=3_000)
            if btn:
                aria    = await btn.get_attribute("aria-label") or ""
                address = re.sub(r"^[Aa]ddress:\s*", "", aria).strip()
                return address if address else "N/A"
        except Exception:
            pass
        return "N/A"

    async def _get_website(self) -> str:
        try:
            link = await self.page.wait_for_selector(self.WEBSITE_SEL, timeout=3_000)
            if link:
                href = await link.get_attribute("href") or "N/A"
                return href
        except Exception:
            pass
        return "N/A"


# ─────────────────────────────────────────────────────────────────────────────
# Convenience wrapper  (used by main.py / CLI)
# ─────────────────────────────────────────────────────────────────────────────

async def scrape_google_maps(
    specialty: str = "dentist",
    city: str = "Pune",
    area: str = "",
    max_listings: int = 20,
    output_dir: str = "outputs",
    user_data_dir: str = "C:/playwright-profile",
    zoom: int | None = None,
    start_point: dict | None = None,   # ← pass the Cardiologist listing dict here
) -> list[dict]:
    """
    High-level entry point called from main.py.

    Parameters
    ──────────
    specialty     : what to search for   ("dentist", "cardiologist", …)
    city          : city name            ("Pune", "Mumbai", …)
    area          : sub-area / locality  ("Koregaon Park", "Baner", …)
                    Leave blank for a city-wide search.
    max_listings  : cap on listings to process per run
    output_dir    : folder for JSON + CSV output
    user_data_dir : Playwright persistent browser profile directory
    zoom          : override zoom level (12 ≈ 12 km, 13 ≈ 6 km, 14 ≈ 3 km)
    start_point   : dict with at minimum 'latitude', 'longitude', and 'name'.
                    When provided, distance_km for every result is the
                    straight-line haversine distance FROM this point.
                    Typically the Cardiologist listing scraped in a prior run.

    Example (two-step usage in main.py)
    ────────────────────────────────────
        # Step 1 — scrape the cardiologist (start point)
        cardio_leads = await scrape_google_maps(
            specialty="cardiologist", city="Pune"
        )
        cardiologist = cardio_leads[0]   # pick the one you want as origin

        # Step 2 — scrape destination specialty, distances from cardiologist
        dest_leads = await scrape_google_maps(
            specialty="physiotherapist",
            city="Pune",
            start_point=cardiologist,    # ← lat/lng used directly, no API
        )
    """
    scraper = GoogleMapsScraper(
        headless=False,
        slow_mo=80,
        scroll_rounds=6,
        max_listings=max_listings,
        zoom=zoom,
    )
    await scraper.start(user_data_dir=user_data_dir)
    try:
        return await scraper.scrape(
            specialty=specialty,
            city=city,
            area=area,
            output_dir=output_dir,
            start_point=start_point,
        )
    finally:
        await scraper.stop()