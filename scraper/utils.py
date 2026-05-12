import re
import json
import csv
import os
from datetime import datetime


# ──────────────────────────────────────────────
# Rating parser
# ──────────────────────────────────────────────

def parse_rating(raw: str) -> tuple[str, str]:
    """
    Input : "4.9 stars 57 Reviews"
    Output: ("4.9", "57")
    """
    if not raw or raw == "N/A":
        return "N/A", "N/A"

    stars_match = re.search(r"([\d.]+)\s*star", raw, re.IGNORECASE)
    reviews_match = re.search(r"([\d,]+)\s*[Rr]eview", raw)

    stars = stars_match.group(1) if stars_match else "N/A"
    reviews = reviews_match.group(1).replace(",", "") if reviews_match else "N/A"
    return stars, reviews


# ──────────────────────────────────────────────
# Coordinate extractor from Google Maps URL
# ──────────────────────────────────────────────

def extract_coords(url: str) -> tuple[str, str]:
    """
    Pulls lat/lng from a Google Maps detail URL.
    Handles patterns like:
      !3d<lat>!4d<lng>
      @<lat>,<lng>,
    """
    # Pattern 1: !3d28.6139!4d77.2090
    m = re.search(r"!3d(-?[\d.]+)!4d(-?[\d.]+)", url)
    if m:
        return m.group(1), m.group(2)

    # Pattern 2: @18.5204,73.8567,
    m = re.search(r"@(-?[\d.]+),(-?[\d.]+)", url)
    if m:
        return m.group(1), m.group(2)

    return "N/A", "N/A"


# ──────────────────────────────────────────────
# Savers
# ──────────────────────────────────────────────

def save_json(data: list[dict], path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)
    print(f"  💾 Saved JSON → {path}  ({len(data)} records)")


def save_csv(data: list[dict], path: str) -> None:
    if not data:
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=data[0].keys())
        writer.writeheader()
        writer.writerows(data)
    print(f"  💾 Saved CSV  → {path}  ({len(data)} records)")


# ──────────────────────────────────────────────
# Timestamp helper
# ──────────────────────────────────────────────

def timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


# ──────────────────────────────────────────────
# Website phone scraper (fallback)
# ──────────────────────────────────────────────

# Indian phone number patterns — covers:
#   +91 98765 43210   |   +919876543210
#   098765 43210      |   9876543210
#   011-12345678      |   0-11-12345678  (landlines)
_PHONE_PATTERNS = [
    r"\+91[\s\-]?\d{5}[\s\-]?\d{5}",        # +91 98765 43210
    r"\+91[\s\-]?\d{10}",                    # +919876543210
    r"0\d{2,4}[\s\-]?\d{6,8}",              # 011-12345678 (landline)
    r"(?<!\d)[6-9]\d{9}(?!\d)",             # bare 10-digit mobile (starts 6-9)
]
_PHONE_RE = re.compile("|".join(_PHONE_PATTERNS))

# Pages to try, in order (relative paths appended to base URL)
_CONTACT_PATHS = ["", "/contact", "/contact-us", "/contactus", "/about", "/reach-us"]


async def scrape_phone_from_website(page, website_url: str) -> str:
    """
    Open the clinic website in a NEW TAB, scan up to a few pages for
    a phone number, close the tab, and return the number (or 'N/A').

    `page` is the current Playwright Page object (used to open a new tab
    via its browser context).
    """
    if not website_url or website_url == "N/A":
        return "N/A"

    # Strip trailing slash and query strings for clean path joining
    base = website_url.rstrip("/").split("?")[0]

    context = page.context          # reuse the existing browser context
    tab = await context.new_page()  # open a new tab — Maps stays open

    try:
        for path in _CONTACT_PATHS:
            url = base + path
            try:
                await tab.goto(url, timeout=10000, wait_until="domcontentloaded")
                await tab.wait_for_timeout(1500)
            except Exception:
                continue   # page timed out or 404 — try next path

            text = await tab.inner_text("body")
            match = _PHONE_RE.search(text)
            if match:
                number = match.group(0).strip()
                print(f"    📞 Found on website ({path or '/'}): {number}")
                return number

        return "N/A"

    except Exception as e:
        print(f"    ⚠️  Website scrape failed: {e}")
        return "N/A"

    finally:
        await tab.close()   # always close the tab, even on error