"""
scraper/justdial.py
───────────────────
Stable JustDial scraper
- fixes ERR_TOO_MANY_REDIRECTS by clearing storage/cookies and
  navigating via javascript redirect instead of direct goto
- clears bad cookies/session properly
- infinite scroll
- stable selectors
- phone reveal
"""

from __future__ import annotations

import asyncio
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from .base_scraper import BaseScraper
from .geocode import geocode, distance_from
from .utils import save_json, save_csv


# ─────────────────────────────────────────────────────────────
# URL HELPERS
# ─────────────────────────────────────────────────────────────

CITY_SLUGS = {
    "Pune": "Pune",
    "Mumbai": "Mumbai",
    "Bangalore": "Bangalore",
    "Hyderabad": "Hyderabad",
    "Delhi": "Delhi",
}

SPECIALTY_TERMS = {
    "dentist": "Dentists",
    "cardiologist": "Cardiologists",
    "dermatologist": "Dermatologists",
    "orthopedic doctor": "Orthopedic-Doctors",
    "gynecologist": "Gynecologists",
    "neurologist": "Neurologists",
    "general physician": "General-Physicians",
    "physiotherapist": "Physiotherapists",
}


def _jd_term(specialty: str) -> str:
    return SPECIALTY_TERMS.get(
        specialty.lower().strip(),
        specialty.replace(" ", "-").title()
    )


def _city_slug(city: str) -> str:
    return CITY_SLUGS.get(city, city)


# ─────────────────────────────────────────────────────────────
# SCRAPER
# ─────────────────────────────────────────────────────────────

class JustDialScraper(BaseScraper):

    CARD_SEL = "li.cntanr"

    def __init__(
        self,
        headless: bool = False,
        slow_mo: int = 100,
        max_listings: int = 20,
    ):
        super().__init__(headless=headless, slow_mo=slow_mo)
        self.max_listings = max_listings

    async def _clear_site_data(self) -> None:
        """
        Clears all cookies, localStorage, and sessionStorage for justdial.com
        to break any redirect loop caused by stale session data.
        """
        # 1. Clear all browser-level cookies
        try:
            await self.browser.clear_cookies()
        except Exception:
            pass

        # 2. Navigate to a blank page so we can safely clear storage
        try:
            await self.page.goto("about:blank", wait_until="commit", timeout=10_000)
        except Exception:
            pass

        # 3. Open justdial homepage with a clean slate via CDP / evaluate
        #    We land on the homepage with no cookies, then clear JS storage too.
        try:
            await self.page.goto(
                "https://www.justdial.com",
                wait_until="domcontentloaded",
                timeout=30_000,
            )
            await self.page.evaluate("""() => {
                try { localStorage.clear(); } catch(e) {}
                try { sessionStorage.clear(); } catch(e) {}
            }""")
        except Exception as e:
            print(f"[JUSTDIAL] Homepage pre-load warning: {e}")

        await self.page.wait_for_timeout(3_000)

    async def _navigate_to_search(self, search_url: str) -> bool:
        """
        Navigate to the search URL with multiple fallback strategies.
        Returns True if navigation succeeded (page loaded some content),
        False if all strategies failed.
        """

        strategies = [
            # Strategy 1: normal navigation with networkidle
            dict(wait_until="networkidle", timeout=45_000),
            # Strategy 2: only wait for DOM
            dict(wait_until="domcontentloaded", timeout=30_000),
            # Strategy 3: just wait for commit (any response)
            dict(wait_until="commit", timeout=20_000),
        ]

        for i, kwargs in enumerate(strategies, 1):
            try:
                print(f"[JUSTDIAL] Nav attempt {i}: {kwargs['wait_until']}...")
                await self.page.goto(search_url, **kwargs)
                await self.page.wait_for_timeout(5_000)
                # Check we actually landed on JustDial (not a redirect loop error page)
                current = self.page.url
                if "justdial.com" in current:
                    print(f"[JUSTDIAL] Landed on: {current}")
                    return True
            except Exception as e:
                print(f"[JUSTDIAL] Strategy {i} failed: {e}")
                # Wait before retry
                await self.page.wait_for_timeout(2_000)
                # Re-clear cookies between attempts to break redirect loop
                try:
                    await self.browser.clear_cookies()
                except Exception:
                    pass

        # Strategy 4: Use JS location assign (bypasses Playwright's redirect tracking)
        try:
            print("[JUSTDIAL] Trying JS location assign fallback...")
            await self.page.goto("https://www.justdial.com", wait_until="domcontentloaded", timeout=20_000)
            await self.page.wait_for_timeout(2_000)
            await self.page.evaluate(f"() => {{ window.location.assign('{search_url}'); }}")
            await self.page.wait_for_timeout(8_000)
            if "justdial.com" in self.page.url:
                print(f"[JUSTDIAL] JS assign landed on: {self.page.url}")
                return True
        except Exception as e:
            print(f"[JUSTDIAL] JS assign failed: {e}")

        return False

    async def scrape(
        self,
        specialty: str = "dentist",
        city: str = "Pune",
        area: str = "",
        output_dir: str = "outputs",
    ):

        city_slug = _city_slug(city)
        jd_term = _jd_term(specialty)

        if area:
            search_url = (
                f"https://www.justdial.com/"
                f"{city_slug}/{jd_term}/"
                f"{area.replace(' ', '-')}"
            )
        else:
            search_url = (
                f"https://www.justdial.com/"
                f"{city_slug}/{jd_term}"
            )

        print(f"\n[JUSTDIAL] URL      : {search_url}")
        print(f"[JUSTDIAL] Specialty: {specialty}")
        print(f"[JUSTDIAL] City     : {city}")
        print(f"[JUSTDIAL] Area     : {area or '—'}")

        origin_label = f"{area}, {city}" if area else city
        origin = geocode(origin_label) or geocode(city)

        print(f"[JUSTDIAL] Origin   : {origin_label} → {origin}")

        # ─────────────────────────────────────────────
        # CLEAR STALE SESSION DATA (fixes redirect loop)
        # ─────────────────────────────────────────────
        await self._clear_site_data()

        # ─────────────────────────────────────────────
        # NAVIGATE TO SEARCH PAGE
        # ─────────────────────────────────────────────
        success = await self._navigate_to_search(search_url)

        if not success:
            print("[JUSTDIAL] ❌ All navigation strategies failed. Aborting.")
            return []

        # ─────────────────────────────────────────────
        # POPUPS
        # ─────────────────────────────────────────────

        for popup in [
            'button[class*="close"]',
            'span[class*="close"]',
            'button:has-text("×")',
            'button:has-text("No Thanks")',
        ]:
            try:
                await self.page.click(popup, timeout=1500)
            except Exception:
                pass

        # ─────────────────────────────────────────────
        # SCROLL
        # ─────────────────────────────────────────────

        print("[JUSTDIAL] Loading listings via scroll...")

        for _ in range(10):
            await self.page.mouse.wheel(0, 3500)
            await self.page.wait_for_timeout(2000)

        # ─────────────────────────────────────────────
        # WAIT FOR CARDS
        # ─────────────────────────────────────────────

        try:
            await self.page.wait_for_selector(self.CARD_SEL, timeout=20_000)
        except Exception:
            print("[JUSTDIAL] No listing cards found — page may have blocked the request")
            # Dump page title for debugging
            try:
                title = await self.page.title()
                print(f"[JUSTDIAL] Page title: '{title}'")
            except Exception:
                pass
            return []

        cards = await self.page.query_selector_all(self.CARD_SEL)
        print(f"[JUSTDIAL] Found {len(cards)} cards")

        leads = []

        for i, card in enumerate(cards, 1):

            if len(leads) >= self.max_listings:
                break

            print(f"  [{i}] Extracting...", end=" ")

            lead = await self._extract_card(card, specialty, city, area, origin)

            if lead:
                leads.append(lead)
                print(f"✓ {lead['name']}")
            else:
                print("skipped")

        # ─────────────────────────────────────────────
        # SAVE
        # ─────────────────────────────────────────────

        slug = (
            f"justdial_{specialty}_{city}_{area}"
            .replace(" ", "_")
            .strip("_")
        )

        os.makedirs(output_dir, exist_ok=True)
        save_json(leads, f"{output_dir}/{slug}.json")
        save_csv(leads, f"{output_dir}/{slug}.csv")

        print(f"\n[JUSTDIAL] Done — {len(leads)} leads saved")
        return leads

    async def _extract_card(self, card, specialty, city, area, origin):

        try:

            # NAME
            name = "N/A"

            for selector in [
                "h2",
                "h3",
                'a[class*="store"]',
                'span[class*="lng_cont_name"]'
            ]:
                try:
                    el = await card.query_selector(selector)
                    if el:
                        txt = (await el.inner_text()).strip()
                        if txt and len(txt) > 2:
                            name = txt
                            break
                except Exception:
                    pass

            if name == "N/A":
                return None

            # RATING
            stars = "N/A"
            try:
                rating_el = await card.query_selector('span.green-box')
                if rating_el:
                    txt = (await rating_el.inner_text()).strip()
                    m = re.search(r'(\d+\.?\d*)', txt)
                    if m:
                        stars = m.group(1)
            except Exception:
                pass

            # REVIEWS
            reviews = "N/A"
            try:
                review_el = await card.query_selector('span[class*="review"]')
                if review_el:
                    txt = (await review_el.inner_text()).strip()
                    m = re.search(r'(\d+)', txt)
                    if m:
                        reviews = m.group(1)
            except Exception:
                pass

            # ADDRESS
            address = "N/A"
            for selector in [
                'div[class*="address"]',
                'span[class*="address"]',
                'p[class*="address"]',
            ]:
                try:
                    el = await card.query_selector(selector)
                    if el:
                        txt = (await el.inner_text()).strip()
                        if txt:
                            address = re.sub(r'\s+', ' ', txt)
                            break
                except Exception:
                    pass

            # PHONE
            phone = "N/A"
            try:
                tel = await card.query_selector('a[href^="tel:"]')
                if tel:
                    href = await tel.get_attribute("href")
                    if href:
                        phone = href.replace("tel:", "").strip()
            except Exception:
                pass

            # URL
            profile_url = "N/A"
            try:
                link_el = await card.query_selector('a[href*="/biz/"]')
                if link_el:
                    href = await link_el.get_attribute("href")
                    if href:
                        profile_url = (
                            href if href.startswith("http")
                            else "https://www.justdial.com" + href
                        )
            except Exception:
                pass

            # DISTANCE
            dist_km = "N/A"
            if origin and address != "N/A":
                try:
                    parts = [p.strip() for p in address.split(",") if p.strip()]
                    locality = parts[-1]
                    dest = geocode(f"{locality}, {city}")
                    if dest:
                        dist_km = distance_from(origin[0], origin[1], dest[0], dest[1])
                except Exception:
                    pass

            return {
                "source": "justdial",
                "name": name,
                "specialty": specialty,
                "city": city,
                "area": area,
                "stars": stars,
                "reviews": reviews,
                "address": address,
                "phone": phone,
                "phone_source": "justdial" if phone != "N/A" else "not_found",
                "website": "N/A",
                "years_in_business": "N/A",
                "latitude": "N/A",
                "longitude": "N/A",
                "distance_km": dist_km,
                "justdial_url": profile_url,
            }

        except Exception as exc:
            print(f"\n    [ERR] {exc}")
            return None


# ─────────────────────────────────────────────────────────────
# WRAPPER
# ─────────────────────────────────────────────────────────────

async def scrape_justdial(
    specialty: str = "dentist",
    city: str = "Pune",
    area: str = "",
    max_listings: int = 20,
    output_dir: str = "outputs",
    user_data_dir: str = "C:/playwright-profile",
):

    scraper = JustDialScraper(
        headless=False,
        slow_mo=100,
        max_listings=max_listings,
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