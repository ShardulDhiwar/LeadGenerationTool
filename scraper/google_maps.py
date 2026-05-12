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
  ✅ latitude / longitude  (from the detail-panel URL)
  ✅ specialty  (the search term used)
  ✅ city / area

Radius limiting:
  When an area has known coordinates in config/areas_coords.py,
  we open Maps at that lat/lng with zoom=12 (~10-14 km radius)
  before searching, so results stay local for MR coverage.
"""

import asyncio
import re
from .base_scraper import BaseScraper
from .utils import (
    parse_rating,
    extract_coords,
    save_json,
    save_csv,
    timestamp,
    scrape_phone_from_website,
)

# ── Area coordinates for radius-limited search ────────────────
try:
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from config.areas_coords import AREA_COORDS, DEFAULT_ZOOM
except ImportError:
    AREA_COORDS  = {}
    DEFAULT_ZOOM = 12


class GoogleMapsScraper(BaseScraper):

    # ── selectors ────────────────────────────────────────────────────
    LISTING_SELECTOR   = '//div[@role="article"]'
    NAME_LINK_SEL      = "a.hfpxzc"
    RATING_SEL         = 'span[aria-label*="stars"]'

    # Detail panel selectors (right-side panel after clicking a listing)
    PHONE_SEL          = 'button[data-item-id^="phone"]'
    ADDRESS_SEL        = 'button[data-item-id="address"]'
    WEBSITE_SEL        = 'a[data-item-id="authority"]'

    def __init__(
        self,
        headless: bool = False,
        slow_mo: int = 80,
        scroll_rounds: int = 6,
        max_listings: int = 20,
        zoom: int | None = None,
    ):
        super().__init__(headless=headless, slow_mo=slow_mo)
        self.scroll_rounds = scroll_rounds
        self.max_listings  = max_listings
        self.zoom          = zoom  # override DEFAULT_ZOOM if passed

    # ─────────────────────────────────────────────────────────────────
    # Public entry point
    # ─────────────────────────────────────────────────────────────────

    async def scrape(
        self,
        specialty: str = "dentist",
        city: str = "Pune",
        area: str = "",
        output_dir: str = "outputs",
    ) -> list[dict]:
        """
        Scrape Google Maps for a given specialty + location.
        Returns a list of lead dicts and saves JSON + CSV.
        """
        query = f"{specialty} in {city} {area}".strip()
        print(f"\n[SEARCH] Query: {query}")

        await self._navigate_to_maps(query, area)
        await self._scroll_results()

        listings = await self.page.query_selector_all(self.LISTING_SELECTOR)
        print(f"[INFO] Found {len(listings)} listing elements (capping at {self.max_listings})")
        listings = listings[: self.max_listings]

        leads = []
        for i, listing in enumerate(listings, 1):
            print(f"  [{i}/{len(listings)}] Extracting...", end=" ")
            lead = await self._extract_listing(listing, specialty, city, area)
            if lead:
                leads.append(lead)
                print(f"OK {lead['name']}")
            else:
                print("skipped")

        # ── save ──────────────────────────────────────────────────────
        slug = f"{specialty}_{city}_{area}".replace(" ", "_")
        save_json(leads, f"{output_dir}/{slug}.json")
        save_csv(leads,  f"{output_dir}/{slug}.csv")

        print(f"\n[DONE] {len(leads)} leads scraped for '{query}'")
        return leads

    # ─────────────────────────────────────────────────────────────────
    # Internal helpers
    # ─────────────────────────────────────────────────────────────────

    async def _navigate_to_maps(self, query: str, area: str = ""):
        """
        Navigate to Google Maps and search.

        If the area has known coordinates, we open Maps centered on that
        lat/lng at the configured zoom level BEFORE searching — this
        constrains the visible area to ~10-14 km so Google returns only
        nearby results, not city-wide ones.
        """
        zoom    = self.zoom or DEFAULT_ZOOM
        coords  = AREA_COORDS.get(area) if area else None

        if coords:
            lat, lng = coords
            # Open Maps pre-centered on the area at the right zoom level
            maps_url = f"https://www.google.com/maps/@{lat},{lng},{zoom}z"
            print(f"[GPS] Area '{area}' coords ({lat}, {lng}), zoom={zoom} (~{_zoom_radius(zoom)} km radius)")
            print(f"[NAV] Opening: {maps_url}")
            await self.page.goto(maps_url)
        else:
            print(f"[NAV] No coords for '{area}' — opening Maps normally")
            await self.page.goto("https://www.google.com/maps")

        await self.page.wait_for_timeout(3000)

        print("[KEY] Typing search query...")
        await self.page.keyboard.type(query, delay=80)
        await self.page.keyboard.press("Enter")

        # Wait for the results sidebar to appear
        await self.page.wait_for_selector(
            '//div[@role="feed"]', timeout=15000
        )
        await self.page.wait_for_timeout(3000)

    async def _scroll_results(self):
        """Scroll the results panel to load more listings."""
        print(f"[SCROLL] Scrolling results ({self.scroll_rounds} rounds)...")
        feed = await self.page.query_selector('//div[@role="feed"]')
        if not feed:
            for _ in range(self.scroll_rounds):
                await self.page.mouse.wheel(0, 5000)
                await self.page.wait_for_timeout(2000)
            return

        for _ in range(self.scroll_rounds):
            await feed.evaluate("el => el.scrollBy(0, 2000)")
            await self.page.wait_for_timeout(2000)

    async def _extract_listing(
        self,
        listing,
        specialty: str,
        city: str,
        area: str,
    ) -> dict | None:
        """Click a listing card, wait for the detail panel, then extract all fields."""
        try:
            # ── name from the listing card ────────────────────────────
            a_tag = await listing.query_selector(self.NAME_LINK_SEL)
            name = await a_tag.get_attribute("aria-label") if a_tag else None
            if not name:
                return None

            # ── rating from the listing card ──────────────────────────
            rating_raw = "N/A"
            rating_el = await listing.query_selector(self.RATING_SEL)
            if rating_el:
                rating_raw = await rating_el.get_attribute("aria-label") or "N/A"
            stars, reviews = parse_rating(rating_raw)

            # ── click into the detail panel ───────────────────────────
            await listing.click()
            await self.page.wait_for_timeout(3000)

            # ── phone ─────────────────────────────────────────────────
            phone = await self._get_phone()

            # ── address ───────────────────────────────────────────────
            address = await self._get_address()

            # ── website ───────────────────────────────────────────────
            website = await self._get_website()

            # ── phone fallback: scrape from website if Maps had none ───
            phone_source = "google_maps"
            if phone == "N/A" and website != "N/A":
                print(f"    [WEB] No phone on Maps — trying website...")
                phone = await scrape_phone_from_website(self.page, website)
                if phone != "N/A":
                    phone_source = "website"
                else:
                    print(f"    [MISS] No phone found on website either")
                    phone_source = "not_found"

            # ── coordinates from current URL ──────────────────────────
            current_url = self.page.url
            lat, lng = extract_coords(current_url)

            # ── distance from area center ─────────────────────────────
            area_center = AREA_COORDS.get(area) if area else None
            distance_km = "N/A"

            if area_center and lat != "N/A" and lng != "N/A":
                dist = _haversine(area_center[0], area_center[1], float(lat), float(lng))
                max_km = _zoom_radius(self.zoom or DEFAULT_ZOOM)
                if dist > max_km:
                    print(f"    [SKIP] {name} is {dist:.1f} km away (limit {max_km} km)")
                    return None
                distance_km = round(dist, 2)
                print(f"    [DIST] {distance_km} km", end=" ")

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

        except Exception as e:
            print(f"\n    [ERR] Error: {e}")
            return None

    async def _get_phone(self) -> str:
        try:
            btn = await self.page.wait_for_selector(self.PHONE_SEL, timeout=3000)
            if btn:
                aria = await btn.get_attribute("aria-label") or ""
                phone = aria.replace("Phone:", "").replace("phone:", "").strip()
                return phone if phone else "N/A"
        except Exception:
            pass
        return "N/A"

    async def _get_address(self) -> str:
        try:
            btn = await self.page.wait_for_selector(self.ADDRESS_SEL, timeout=3000)
            if btn:
                aria = await btn.get_attribute("aria-label") or ""
                address = re.sub(r"^[Aa]ddress:\s*", "", aria).strip()
                return address if address else "N/A"
        except Exception:
            pass
        return "N/A"

    async def _get_website(self) -> str:
        try:
            link = await self.page.wait_for_selector(self.WEBSITE_SEL, timeout=3000)
            if link:
                href = await link.get_attribute("href") or "N/A"
                return href
        except Exception:
            pass
        return "N/A"


# ─────────────────────────────────────────────────────────────────
# Geometry helpers
# ─────────────────────────────────────────────────────────────────

import math

def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Straight-line distance in km between two lat/lng points."""
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
    return R * 2 * math.asin(math.sqrt(a))

def _zoom_radius(zoom: int) -> float:
    """Approximate visible radius in km for a given Google Maps zoom level."""
    return {
        14: 3.0,
        13: 6.0,
        12: 12.0,
        11: 22.0,
        10: 40.0,
    }.get(zoom, 12.0)


# ─────────────────────────────────────────────────────────────────
# Convenience wrapper (used by main.py)
# ─────────────────────────────────────────────────────────────────

async def scrape_google_maps(
    specialty: str = "dentist",
    city: str = "Pune",
    area: str = "",
    max_listings: int = 20,
    output_dir: str = "outputs",
    user_data_dir: str = "C:/playwright-profile",
    zoom: int | None = None,
) -> list[dict]:
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
        )
    finally:
        await scraper.stop()