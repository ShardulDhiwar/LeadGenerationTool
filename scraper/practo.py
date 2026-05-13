"""
scraper/practo.py
─────────────────
Practo scraper — rewritten to intercept the internal JSON API.

ROOT CAUSES OF PREVIOUS FAILURE:
  1. Wrong URL: /pune/dentists  →  correct is /pune/dentist  (singular)
  2. Bot detection: Practo detects Playwright and serves only the navbar
     shell (React never hydrates), so DOM selectors find nothing.

FIX — API interception strategy:
  • Navigate to the listing page normally (triggers React hydration attempt).
  • Simultaneously intercept XHR/fetch responses to Practo's internal
    doctor-listing API  (/api/doctor_listing  or  /api/doctor_search).
  • Parse the JSON response directly — no DOM scraping needed at all.
  • Falls back to DOM scraping if the API response isn't captured.

URL format (confirmed 2026):
  https://www.practo.com/{city}/{specialty_slug}       ← SINGULAR slug
  e.g.  https://www.practo.com/pune/dentist
        https://www.practo.com/pune/dentist?page=2
        https://www.practo.com/pune/dentist/koregaon-park  (area)
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from .base_scraper import BaseScraper
from .geocode import geocode, distance_from
from .utils import save_json, save_csv

from config.specialists import SPECIALISTS
from config.areas import AREAS


# ─────────────────────────────────────────────
# URL helpers
# ─────────────────────────────────────────────

def _city_slug(city: str) -> str:
    return city.lower().replace(" ", "-")


def _specialty_slug(specialty: str) -> str:
    """
    Returns the SINGULAR slug Practo uses in its URLs.
    /pune/dentist  NOT  /pune/dentists
    """
    specialty_map = {
        "dentist":              "dentist",
        "cardiologist":         "cardiologist",
        "dermatologist":        "dermatologist",
        "orthopedic doctor":    "orthopedic-surgeon",
        "gynecologist":         "gynecologist",
        "neurologist":          "neurologist",
        "general physician":    "general-physician",
        "physiotherapist":      "physiotherapist",
        "nephrologist":         "nephrologist",
        "endocrinologist":      "endocrinologist",
        "gastroenterologist":   "gastroenterologist",
        "diabetologist":        "diabetologist",
        "geriatrics":           "geriatrician",
        "ivf center":           "infertility-ivf-specialist",
        "weight loss center":   "bariatric-surgeon",
        "ophthalmologist":      "ophthalmologist",
        "ent specialist":       "ent-specialist",
        "psychiatrist":         "psychiatrist",
        "pediatrician":         "pediatrician",
    }
    return specialty_map.get(
        specialty.lower().strip(),
        specialty.lower().replace(" ", "-")
    )


def _area_slug(area: str) -> str:
    return area.lower().replace(" ", "-")


# ─────────────────────────────────────────────
# Practo Scraper
# ─────────────────────────────────────────────

class PractoScraper(BaseScraper):

    # Partial URL patterns that match Practo's listing API calls
    _API_PATTERNS = [
        "api/doctor_listing",
        "api/doctor_search",
        "api/doctors",
        "/listing",
    ]

    def __init__(
        self,
        headless: bool = False,
        slow_mo: int = 80,
        max_pages: int = 3,
        max_listings: int = 20,
    ) -> None:
        super().__init__(headless=headless, slow_mo=slow_mo)
        self.max_pages    = max_pages
        self.max_listings = max_listings
        self._captured_responses: list[dict] = []

    async def scrape(
        self,
        specialty: str = "dentist",
        city: str = "Pune",
        area: str = "",
        output_dir: str = "outputs",
    ) -> list[dict]:

        city_slug      = _city_slug(city)
        specialty_slug = _specialty_slug(specialty)

        # ── correct URL (singular slug) ────────────────────────────────────
        if area:
            base_url = (
                f"https://www.practo.com/{city_slug}/{specialty_slug}"
                f"/{_area_slug(area)}"
            )
        else:
            base_url = f"https://www.practo.com/{city_slug}/{specialty_slug}"

        print(f"\n[PRACTO] URL       : {base_url}")
        print(f"[PRACTO] Specialty : {specialty}  (slug: {specialty_slug})")
        print(f"[PRACTO] City      : {city}")
        print(f"[PRACTO] Area      : {area or '—'}")

        origin_label = f"{area}, {city}" if area else city
        origin = geocode(origin_label) or geocode(city)
        print(f"[PRACTO] Origin    : {origin_label} → {origin}")

        # Attach network interceptor before first navigation
        await self._attach_interceptor()

        leads: list[dict] = []

        for page_num in range(1, self.max_pages + 1):

            if len(leads) >= self.max_listings:
                break

            url = f"{base_url}?page={page_num}" if page_num > 1 else base_url
            print(f"\n[PRACTO] Page {page_num}: {url}")

            self._captured_responses.clear()

            try:
                await self.page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            except Exception as e:
                print(f"[PRACTO] Navigation failed: {e}")
                continue

            # Dismiss common popups
            for popup in [
                'button:has-text("Skip")',
                'button:has-text("Close")',
                'button[class*="close"]',
                '[data-qa-id="popup_close"]',
            ]:
                try:
                    await self.page.click(popup, timeout=1_500)
                except Exception:
                    pass

            # Scroll to trigger lazy-load XHR calls
            for _ in range(6):
                await self.page.mouse.wheel(0, 2_500)
                await self.page.wait_for_timeout(1_500)

            await self.page.wait_for_timeout(3_000)

            # ── Strategy A: intercepted API JSON ───────────────────────────
            if self._captured_responses:
                print(f"[PRACTO] ✓ {len(self._captured_responses)} API response(s) captured")
                page_leads = self._parse_api_responses(
                    self._captured_responses, specialty, city, area, origin
                )
                if page_leads:
                    leads.extend(page_leads)
                    print(f"[PRACTO] {len(page_leads)} doctors from API")
                    continue

            # ── Strategy B: DOM fallback ───────────────────────────────────
            print("[PRACTO] No API response — trying DOM fallback...")
            page_leads = await self._scrape_dom(specialty, city, area, origin)
            if page_leads:
                leads.extend(page_leads)
            else:
                print("[PRACTO] DOM fallback found nothing")
                try:
                    snippet = await self.page.evaluate(
                        "() => document.body.innerHTML.substring(0, 2000)"
                    )
                    print(f"[PRACTO DEBUG] Snippet:\n{snippet}\n")
                except Exception:
                    pass

        leads = leads[: self.max_listings]

        slug = (
            f"practo_{specialty}_{city}_{area}"
            .replace(" ", "_")
            .strip("_")
        )

        os.makedirs(output_dir, exist_ok=True)
        save_json(leads, f"{output_dir}/{slug}.json")
        save_csv(leads,  f"{output_dir}/{slug}.csv")

        print(f"\n[PRACTO] Done — {len(leads)} leads saved")
        return leads

    # ── Network interceptor ───────────────────────────────────────────────

    async def _attach_interceptor(self) -> None:
        async def handle_response(response):
            url = response.url
            if any(pat in url for pat in self._API_PATTERNS):
                try:
                    body = await response.json()
                    print(f"  [API HIT] {url}")
                    self._captured_responses.append(body)
                except Exception:
                    pass

        self.page.on("response", handle_response)

    # ── API response parser ───────────────────────────────────────────────

    def _parse_api_responses(self, responses, specialty, city, area, origin):
        leads = []
        for resp in responses:
            doctors_raw = (
                resp.get("doctors")
                or resp.get("results")
                or resp.get("data", {}).get("doctors")
                or resp.get("data", {}).get("results")
                or []
            )
            if not isinstance(doctors_raw, list):
                continue
            for doc in doctors_raw:
                try:
                    lead = self._parse_doctor_json(doc, specialty, city, area, origin)
                    if lead:
                        leads.append(lead)
                except Exception as e:
                    print(f"    [PARSE ERR] {e}")
        return leads

    def _parse_doctor_json(self, doc, specialty, city, area, origin):
        name = (
            doc.get("name") or doc.get("full_name") or doc.get("doctor_name") or ""
        ).strip()
        if not name:
            return None

        spec = (
            doc.get("specialization")
            or (doc.get("specializations") or ["N/A"])[0]
            or "N/A"
        )
        if isinstance(spec, dict):
            spec = spec.get("name", "N/A")

        exp_val    = doc.get("experience") or doc.get("years_of_experience") or ""
        experience = f"{exp_val} years" if exp_val else "N/A"
        stars      = str(doc.get("rating") or doc.get("score") or "N/A")
        reviews    = str(
            doc.get("feedback_count") or doc.get("total_feedback")
            or doc.get("review_count") or "N/A"
        )

        practices = doc.get("practice_list") or doc.get("practices") or []
        clinic = locality = fee = "N/A"
        if practices and isinstance(practices, list):
            p        = practices[0]
            clinic   = p.get("name") or p.get("clinic_name") or "N/A"
            locality = p.get("locality") or p.get("area") or "N/A"
            fee_raw  = p.get("consultation_fees") or p.get("fees") or ""
            fee      = str(fee_raw) if fee_raw else "N/A"

        parts   = [x for x in [clinic, locality, city] if x and x != "N/A"]
        address = ", ".join(parts) if parts else city

        profile_url = doc.get("profile_url") or doc.get("url") or "N/A"
        if profile_url and profile_url != "N/A" and profile_url.startswith("/"):
            profile_url = f"https://www.practo.com{profile_url}"

        dist_km = "N/A"
        if origin and locality != "N/A":
            dest = geocode(f"{locality}, {city}")
            if dest:
                dist_km = distance_from(origin[0], origin[1], dest[0], dest[1])

        return {
            "source": "practo", "name": name,
            "specialty": specialty, "specialization": spec,
            "experience": experience, "city": city, "area": area,
            "stars": stars, "reviews": reviews, "address": address,
            "consultation_fee": fee, "phone": "N/A",
            "phone_source": "not_available", "website": "N/A",
            "latitude": "N/A", "longitude": "N/A",
            "distance_km": dist_km, "practo_url": profile_url,
        }

    # ── DOM fallback ──────────────────────────────────────────────────────

    async def _scrape_dom(self, specialty, city, area, origin):
        await self.page.wait_for_timeout(5_000)

        try:
            await self.page.wait_for_selector('a[href*="/doctor/"]', timeout=10_000)
        except Exception:
            return []

        CARD_SELECTORS = [
            'div[data-qa-id="doctor_card"]',
            'div[class*="u-border-general--bottom"]',
            'div[class*="card-doctor"]',
            'div[class*="doctor-card"]',
            'div[class*="DoctorCard"]',
            'div[class*="listing__header"]',
        ]

        cards = []
        used_sel = None
        for sel in CARD_SELECTORS:
            try:
                found = await self.page.query_selector_all(sel)
                if found:
                    cards, used_sel = found, sel
                    break
            except Exception:
                pass

        if not cards:
            try:
                cards = await self.page.query_selector_all('a[href*="/doctor/"]')
                used_sel = "doctor-link-fallback"
            except Exception:
                return []

        print(f"[PRACTO DOM] {len(cards)} elements (selector: {used_sel})")
        leads = []

        for card in cards:
            try:
                if used_sel == "doctor-link-fallback":
                    href = await card.get_attribute("href") or ""
                    if "/doctor/" not in href:
                        continue
                    name = (await card.inner_text()).strip()
                    if not name:
                        continue
                    pu = f"https://www.practo.com{href}" if href.startswith("/") else href
                    leads.append({
                        "source": "practo", "name": name,
                        "specialty": specialty, "specialization": "N/A",
                        "experience": "N/A", "city": city, "area": area,
                        "stars": "N/A", "reviews": "N/A", "address": city,
                        "consultation_fee": "N/A", "phone": "N/A",
                        "phone_source": "not_available", "website": "N/A",
                        "latitude": "N/A", "longitude": "N/A",
                        "distance_km": "N/A", "practo_url": pu,
                    })
                    continue

                async def txt(sels):
                    for s in sels:
                        try:
                            el = await card.query_selector(s)
                            if el:
                                t = (await el.inner_text()).strip()
                                if t:
                                    return t
                        except Exception:
                            pass
                    return "N/A"

                name = await txt([
                    'h2[data-qa-id="doctor_name"]',
                    'a[data-qa-id="doctor_name"]', 'h2 a', 'h2',
                ])
                if name == "N/A":
                    continue

                spec     = await txt(['div[data-qa-id="doctor_specialization"]', 'div[class*="specialization"]'])
                exp_raw  = await txt(['div[data-qa-id="doctor_experience"]', 'span[class*="experience"]'])
                exp_m    = re.search(r'(\d+)', exp_raw)
                exp      = f"{exp_m.group(1)} years" if exp_m else "N/A"
                rr       = await txt(['div[data-qa-id="star_rating"]', 'span[class*="rating"]'])
                sm       = re.search(r'(\d+\.?\d*)', rr)
                stars    = sm.group(1) if sm else "N/A"
                revr     = await txt(['p[data-qa-id="total_feedback"]', 'span[class*="feedback"]'])
                rm       = re.search(r'(\d+)', revr.replace(",", ""))
                reviews  = rm.group(1) if rm else "N/A"
                clinic   = await txt(['span[data-qa-id="practice_name"]',   'div[class*="clinic"]'])
                locality = await txt(['span[data-qa-id="practice_locality"]','span[class*="locality"]'])
                fee      = await txt(['div[data-qa-id="consultation_fee"]',  'span[class*="fee"]'])
                parts    = [x for x in [clinic, locality, city] if x and x != "N/A"]
                address  = ", ".join(parts) if parts else city

                href = "N/A"
                for ls in ['a[data-qa-id="doctor_profile_link"]', 'a[href*="/doctor/"]']:
                    try:
                        el = await card.query_selector(ls)
                        if el:
                            v = await el.get_attribute("href")
                            if v:
                                href = v; break
                    except Exception:
                        pass
                pu = f"https://www.practo.com{href}" if href.startswith("/") else href

                dist_km = "N/A"
                if origin and locality != "N/A":
                    dest = geocode(f"{locality}, {city}")
                    if dest:
                        dist_km = distance_from(origin[0], origin[1], dest[0], dest[1])

                leads.append({
                    "source": "practo", "name": name,
                    "specialty": specialty, "specialization": spec,
                    "experience": exp, "city": city, "area": area,
                    "stars": stars, "reviews": reviews, "address": address,
                    "consultation_fee": fee, "phone": "N/A",
                    "phone_source": "not_available", "website": "N/A",
                    "latitude": "N/A", "longitude": "N/A",
                    "distance_km": dist_km, "practo_url": pu,
                })

            except Exception as e:
                print(f"  [DOM ERR] {e}")

        return leads


# ─────────────────────────────────────────────
# Convenience wrapper
# ─────────────────────────────────────────────

async def scrape_practo(
    specialty: str = "dentist",
    city: str = "Pune",
    area: str = "",
    max_listings: int = 20,
    max_pages: int = 3,
    output_dir: str = "outputs",
    user_data_dir: str = "C:/playwright-profile",
):
    scraper = PractoScraper(
        headless=False,
        slow_mo=80,
        max_pages=max_pages,
        max_listings=max_listings,
    )
    await scraper.start(user_data_dir=user_data_dir)
    try:
        return await scraper.scrape(
            specialty=specialty, city=city,
            area=area, output_dir=output_dir,
        )
    finally:
        await scraper.stop()