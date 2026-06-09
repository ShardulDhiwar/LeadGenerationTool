"""
scraper/google_maps.py
──────────────────────
Scrapes Google Maps for doctor listings.

Distance is now calculated automatically:
  • The search origin (city / area) is geocoded via OSM Nominatim
    (free, no API key) at the start of every run.
  • Each listing's lat/lng is extracted from the detail panel URL
    using the !3d/!4d data parameters (exact pin).
  • Haversine distance is computed between origin and listing pin.

No more AREA_COORDS dependency — it just works for any city/area.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from .base_scraper import BaseScraper
from .geocode import geocode, distance_from
from .utils import parse_rating, save_json, save_csv, scrape_phone_from_website

try:
    from config.areas_coords import DEFAULT_ZOOM
except ImportError:
    DEFAULT_ZOOM = 12


# ─────────────────────────────────────────────────────────────────────────────
# Coordinate extraction from Maps URL
# ─────────────────────────────────────────────────────────────────────────────

def extract_listing_coords(url: str) -> tuple[str, str]:
    """
    Extract listing pin lat/lng from a Google Maps detail panel URL.

    Pattern B — exact pin (preferred):   ...!3d18.52715!4d73.85530...
    Pattern A — viewport centre (±200m): .../@18.5271,73.8553,17z/...
    """
    m = re.search(r'!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)', url)
    if m:
        return m.group(1), m.group(2)

    m = re.search(r'@(-?\d+\.\d+),(-?\d+\.\d+),\d+(?:\.\d+)?z', url)
    if m:
        return m.group(1), m.group(2)

    return "N/A", "N/A"


def _zoom_radius(zoom: int) -> float:
    return {14: 3.0, 13: 6.0, 12: 12.0, 11: 22.0, 10: 40.0}.get(zoom, 12.0)


# ─────────────────────────────────────────────────────────────────────────────
# Scraper
# ─────────────────────────────────────────────────────────────────────────────

class GoogleMapsScraper(BaseScraper):

    LISTING_SELECTOR = '//div[@role="article"]'
    NAME_LINK_SEL    = "a.hfpxzc"
    RATING_SEL       = 'span[aria-label*="stars"]'
    PHONE_SEL        = 'button[data-item-id^="phone"]'
    ADDRESS_SEL      = 'button[data-item-id="address"]'
    WEBSITE_SEL      = 'a[data-item-id="authority"]'

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

    async def scrape(
        self,
        specialty: str = "dentist",
        city: str = "Pune",
        area: str = "",
        output_dir: str = "outputs",
    ) -> list[dict]:
        query = f"{specialty} in {city} {area}".strip()
        print(f"\n[GMAPS] Query: {query}")

        # ── geocode the search origin once ────────────────────────────────
        origin_label = f"{area}, {city}" if area else city
        origin       = geocode(origin_label) or geocode(city)

        if origin:
            print(f"[GMAPS] Origin: '{origin_label}' → {origin}")
        else:
            print(f"[GMAPS] WARNING: Could not geocode '{origin_label}' — distance will be N/A")

        # ── navigate + scroll ─────────────────────────────────────────────
        await self._navigate_to_maps(query, origin)
        await self._scroll_results()

        listings = await self.page.query_selector_all(self.LISTING_SELECTOR)
        listings = listings[: self.max_listings]
        print(f"[GMAPS] Processing {len(listings)} listings...")

        leads: list[dict] = []
        for i, listing in enumerate(listings, 1):
            print(f"\n  [{i}/{len(listings)}] Extracting...", end=" ")
            lead = await self._extract_listing(listing, specialty, city, area, origin)
            if lead:
                leads.append(lead)
                print(f"✓ {lead['name']}  |  {lead['distance_km']} km")
            else:
                print("skipped")

        slug = f"gmaps_{specialty}_{city}_{area}".replace(" ", "_").strip("_")
        os.makedirs(output_dir, exist_ok=True)
        save_json(leads, f"{output_dir}/{slug}.json")
        save_csv(leads,  f"{output_dir}/{slug}.csv")

        print(f"\n[GMAPS] Done — {len(leads)} leads saved to {output_dir}/")
        return leads

    # ── navigation ────────────────────────────────────────────────────────

    async def _navigate_to_maps(self, query: str, origin: tuple | None) -> None:
        zoom = self.zoom or DEFAULT_ZOOM

        if origin:
            lat, lng = origin
            url = f"https://www.google.com/maps/@{lat},{lng},{zoom}z"
            await self.page.goto(url)
        else:
            await self.page.goto("https://www.google.com/maps")

        await self.page.wait_for_timeout(3_000)
        await self.page.keyboard.type(query, delay=80)
        await self.page.keyboard.press("Enter")
        await self.page.wait_for_selector('//div[@role="feed"]', timeout=15_000)
        await self.page.wait_for_timeout(3_000)

    async def _scroll_results(self) -> None:
        feed = await self.page.query_selector('//div[@role="feed"]')
        for _ in range(self.scroll_rounds):
            if feed:
                await feed.evaluate("el => el.scrollBy(0, 2000)")
            else:
                await self.page.mouse.wheel(0, 5_000)
            await self.page.wait_for_timeout(2_000)

    # ── per-listing extraction ────────────────────────────────────────────

    async def _extract_listing(
        self,
        listing,
        specialty: str,
        city: str,
        area: str,
        origin: tuple[float, float] | None,
    ) -> dict | None:
        try:
            a_tag = await listing.query_selector(self.NAME_LINK_SEL)
            name  = await a_tag.get_attribute("aria-label") if a_tag else None
            if not name:
                return None

            rating_raw = "N/A"
            rating_el  = await listing.query_selector(self.RATING_SEL)
            if rating_el:
                rating_raw = await rating_el.get_attribute("aria-label") or "N/A"
            stars, reviews = parse_rating(rating_raw)

            # ── click into detail panel ───────────────────────────────────
            await listing.click()
            await self.page.wait_for_timeout(3_000)

            # ── detect stuck-on-search-URL and retry ──────────────────────
            if "/maps/search/" in self.page.url:
                await listing.evaluate("el => el.click()")
                await self.page.wait_for_timeout(4_000)
            if "/maps/search/" in self.page.url:
                print(f"[SKIP] Could not open panel for '{name}'")
                return None

            phone   = await self._get_phone()
            address = await self._get_address()
            website = await self._get_website()

            phone_source = "google_maps"
            if phone == "N/A" and website != "N/A":
                phone = await scrape_phone_from_website(self.page, website)
                phone_source = "website" if phone != "N/A" else "not_found"

            # ── coords from panel URL ─────────────────────────────────────
            await self.page.wait_for_timeout(1_500)
            current_url = self.page.url
            lat, lng    = extract_listing_coords(current_url)

            # ── distance: origin geocode → listing pin ────────────────────
            dist_km = distance_from(origin[0], origin[1], lat, lng) if origin else "N/A"

            return {
                "source":       "google_maps",
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
                "distance_km":  dist_km,
                "maps_url":     current_url,
            }

        except Exception as exc:
            print(f"\n    [ERR] {exc}")
            return None

    # ── field extractors ──────────────────────────────────────────────────

    async def _get_phone(self) -> str:
        try:
            btn = await self.page.wait_for_selector(self.PHONE_SEL, timeout=3_000)
            if btn:
                aria  = await btn.get_attribute("aria-label") or ""
                phone = re.sub(r"^[Pp]hone:\s*", "", aria).strip()
                return phone or "N/A"
        except Exception:
            pass
        return "N/A"

    async def _get_address(self) -> str:
        try:
            btn = await self.page.wait_for_selector(self.ADDRESS_SEL, timeout=3_000)
            if btn:
                aria    = await btn.get_attribute("aria-label") or ""
                address = re.sub(r"^[Aa]ddress:\s*", "", aria).strip()
                return address or "N/A"
        except Exception:
            pass
        return "N/A"

    async def _get_website(self) -> str:
        try:
            link = await self.page.wait_for_selector(self.WEBSITE_SEL, timeout=3_000)
            if link:
                return await link.get_attribute("href") or "N/A"
        except Exception:
            pass
        return "N/A"


# ─────────────────────────────────────────────────────────────────────────────
# Convenience wrapper
# ─────────────────────────────────────────────────────────────────────────────

async def scrape_google_maps(
    specialty: str = "dentist",
    city: str = "Pune",
    area: str = "",
    max_listings: int = 20,
    output_dir: str = "outputs",
    user_data_dir: str = "C:/playwright-profile",
    zoom: int | None = None,
    headless: bool = False,
) -> list[dict]:
    scraper = GoogleMapsScraper(
        headless=headless, slow_mo=80, scroll_rounds=6,
        max_listings=max_listings, zoom=zoom,
    )
    await scraper.start(user_data_dir=user_data_dir)
    try:
        return await scraper.scrape(
            specialty=specialty, city=city, area=area, output_dir=output_dir,
        )
    finally:
        await scraper.stop()