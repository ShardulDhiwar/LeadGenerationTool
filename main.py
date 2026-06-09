"""
main.py  —  LeadGenerationTool
─────────────────────────────────────────────────────────────────
Supported scraping sources:
  • google_maps  — Google Maps scraper
  • practo       — Practo scraper
  • both         — Google Maps + Practo (deduped merge output)
  • justdial     — JustDial scraper  (⚠️ work in progress — do not use via dashboard)
  • all          — All three sources  (⚠️ includes justdial — CLI only)

Folder layout:

  outputs/                         ← raw per-source files
  ├── 2026-05-12_11-33-10/
  │   ├── gmaps_dentist_Pune_Baner.json / .csv
  │   └── practo_dentist_Pune_Baner.json / .csv
  └── ...

  merged/                          ← deduped combined files (separate from outputs)
  ├── 2026-05-12_11-33-10/
  │   └── dentist_Pune_Baner.json / .csv
  └── ...

Usage:

  python main.py                                              # dentist, Pune, Google Maps
  python main.py --source practo --specialty cardiologist --city Mumbai
  python main.py --source both   --specialty dentist --city Pune --area Baner
  python main.py --all-specialists --city Pune --source both
  python main.py --all-specialists --city Pune --all-areas --source both
  python main.py --full-run --source both
  python main.py --full-run --resume
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import re
import asyncio
import argparse
import json
import csv
from datetime import datetime
from itertools import product

from scraper.google_maps import scrape_google_maps
from scraper.practo      import scrape_practo
from scraper.justdial    import scrape_justdial
from config.specialists  import SPECIALISTS
from config.cities       import CITIES
from config.areas        import AREAS

PROGRESS_FILE = "outputs/.progress.json"

# All possible fields across every scraper — order defines CSV column order.
# Google Maps fields : source, name, specialty, city, area, stars, reviews,
#                      address, phone, phone_source, website, latitude,
#                      longitude, distance_km, maps_url
# Practo-only fields : specialization, experience, consultation_fee, practo_url
ALL_FIELDS = [
    "source", "name", "specialty", "specialization", "experience",
    "city", "area", "stars", "reviews", "address",
    "consultation_fee", "phone", "phone_source", "website",
    "latitude", "longitude", "distance_km",
    "practo_url", "maps_url",
]


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def make_run_folder() -> str:
    ts          = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    out_folder  = os.path.join("outputs", ts)
    mrg_folder  = os.path.join("merged",  ts)
    os.makedirs(out_folder, exist_ok=True)
    os.makedirs(mrg_folder, exist_ok=True)
    print(f"📁 Run folder: {out_folder}")
    return ts          # return just the timestamp — callers build both paths


def load_progress() -> set:
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE) as f:
            return set(json.load(f))
    return set()


def save_progress(done: set):
    os.makedirs("outputs", exist_ok=True)
    with open(PROGRESS_FILE, "w") as f:
        json.dump(list(done), f)


def combo_key(source: str, specialty: str, city: str, area: str) -> str:
    return f"{source}|{specialty}|{city}|{area}"


def dedupe_leads(leads: list[dict]) -> list[dict]:
    """
    Deduplicate leads across sources.
    Two leads are the same if their normalised name + city match.
    Later source wins on N/A fields (practo fills what gmaps misses).
    """
    seen: dict[str, dict] = {}
    for lead in leads:
        raw_name = lead.get("name", "")
        key      = re.sub(r'[^a-z0-9]', '', raw_name.lower()) + "|" + lead.get("city", "").lower()
        if key not in seen:
            seen[key] = lead
        else:
            existing = seen[key]
            for field, val in lead.items():
                if existing.get(field) in ("N/A", "", None) and val not in ("N/A", "", None):
                    existing[field] = val
    return list(seen.values())


def _merged_fieldnames(leads: list[dict]) -> list[str]:
    """
    Build column list: start with preferred order (ALL_FIELDS),
    then append any unexpected extra keys from the data so we never crash.
    """
    seen       = set(ALL_FIELDS)
    all_fields = list(ALL_FIELDS)
    for lead in leads:
        for k in lead.keys():
            if k not in seen:
                all_fields.append(k)
                seen.add(k)
    return all_fields


def save_merged(leads: list[dict], merged_folder: str, slug: str):
    """
    Save deduped leads to merged/<timestamp>/<slug>.json and .csv.

    Key fixes vs the old version:
      • fieldnames covers ALL_FIELDS (both gmaps + practo columns)
      • restval="N/A"      — missing keys filled with N/A (not blank)
      • extrasaction="ignore" — future extra keys never crash the writer
      • Writes to merged/ folder, not outputs/
    """
    if not leads:
        return

    os.makedirs(merged_folder, exist_ok=True)

    json_path = os.path.join(merged_folder, f"{slug}.json")
    csv_path  = os.path.join(merged_folder, f"{slug}.csv")

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(leads, f, ensure_ascii=False, indent=2)

    fieldnames = _merged_fieldnames(leads)
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=fieldnames,
            extrasaction="ignore",   # never crash on unknown keys
            restval="N/A",           # fill missing keys with N/A
        )
        writer.writeheader()
        writer.writerows(leads)

    print(f"  [MERGE] {len(leads)} deduped leads → {csv_path}")


# ─────────────────────────────────────────────────────────────────────────────
# Source resolver
#   "both" → google_maps + practo   (justdial excluded — scraper not ready)
#   "all"  → all three              (CLI only, not exposed via dashboard)
# ─────────────────────────────────────────────────────────────────────────────

def _resolve_sources(source: str) -> list[str]:
    if source == "both":
        return ["google_maps", "practo"]
    if source == "all":
        return ["google_maps", "practo", "justdial"]
    return [source]


async def _run_source(source: str, common: dict) -> list[dict]:
    if source == "google_maps":
        return await scrape_google_maps(**common)
    elif source == "practo":
        return await scrape_practo(**common)
    elif source == "justdial":
        return await scrape_justdial(**common)
    else:
        print(f"  [WARN] Unknown source '{source}' — skipping")
        return []


# ─────────────────────────────────────────────────────────────────────────────
# Single run
# ─────────────────────────────────────────────────────────────────────────────

async def run_single(args, ts: str):
    out_folder = os.path.join("outputs", ts)
    mrg_folder = os.path.join("merged",  ts)

    common = dict(
        specialty     = args.specialty,
        city          = args.city,
        area          = args.area,
        max_listings  = args.max,
        output_dir    = out_folder,
        user_data_dir = args.user_data_dir,
        channel       = args.channel,
        headless      = args.headless,
    )

    sources    = _resolve_sources(args.source)
    all_leads: list[dict] = []

    for source in sources:
        leads = await _run_source(source, common)
        all_leads.extend(leads)

    if len(sources) > 1 and all_leads:
        slug   = f"{args.specialty}_{args.city}_{args.area}".replace(" ", "_").strip("_")
        merged = dedupe_leads(all_leads)
        save_merged(merged, mrg_folder, slug)
        print(f"\n[MERGE] {len(merged)} unique leads from {len(sources)} sources")


# ─────────────────────────────────────────────────────────────────────────────
# Combination runs
# ─────────────────────────────────────────────────────────────────────────────

async def run_combinations(combos: list, args, done: set, ts: str):
    out_folder = os.path.join("outputs", ts)
    mrg_folder = os.path.join("merged",  ts)

    total     = len(combos)
    completed = 0
    skipped   = 0
    sources   = _resolve_sources(args.source)

    print(f"\n{'─' * 55}")
    print(f"  Sources      : {', '.join(sources)}")
    print(f"  Total combos : {total}")
    print(f"  Already done : {len(done)}")
    print(f"{'─' * 55}\n")

    for i, (specialty, city, area) in enumerate(combos, 1):
        all_leads: list[dict] = []

        for source in sources:
            key = combo_key(source, specialty, city, area)
            if key in done:
                skipped += 1
                print(f"  [{i}/{total}] Skipping ({source}): {specialty} | {city} | {area or '—'}")
                continue

            print(f"\n  [{i}/{total}] {source} | {specialty} | {city} | {area or '—'}")
            common = dict(
                specialty     = specialty,
                city          = city,
                area          = area,
                max_listings  = args.max,
                output_dir    = out_folder,
                user_data_dir = args.user_data_dir,
                channel       = args.channel,
                headless      = args.headless,
            )
            try:
                leads = await _run_source(source, common)
                all_leads.extend(leads)
                done.add(key)
                save_progress(done)
                completed += 1
            except Exception as e:
                print(f"  ERROR ({source}): {e} — continuing")

        if len(sources) > 1 and all_leads:
            slug   = f"{specialty}_{city}_{area}".replace(" ", "_").strip("_")
            merged = dedupe_leads(all_leads)
            save_merged(merged, mrg_folder, slug)

        if i < total:
            print(f"  Waiting {args.pause}s...")
            await asyncio.sleep(args.pause)

    print(f"\n{'─' * 55}")
    print(f"  Done! Completed: {completed} | Skipped: {skipped}")
    print(f"  Raw files : {out_folder}")
    print(f"  Merged    : {mrg_folder}")
    print(f"{'─' * 55}\n")


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(description="Doctor Lead Generation Tool")

    parser.add_argument("--source", default="google_maps",
                        choices=["google_maps", "practo", "both", "justdial", "all"],
                        help=(
                            "Which source(s) to scrape.\n"
                            "  google_maps — Google Maps only (default)\n"
                            "  practo      — Practo only\n"
                            "  both        — Google Maps + Practo, deduped merge\n"
                            "  justdial    — JustDial only (WIP)\n"
                            "  all         — All three sources (CLI only)"
                        ))
    parser.add_argument("--specialty",       default="dentist")
    parser.add_argument("--city",            default="Pune")
    parser.add_argument("--area",            default="")
    parser.add_argument("--max",             default=20, type=int,
                        help="Max listings per source per query")
    parser.add_argument("--user-data-dir",   default="C:/playwright-profile")
    parser.add_argument("--channel",         default=None,
                        help="Browser channel (e.g. 'chrome' for system Google Chrome)")
    parser.add_argument("--headless",        action="store_true", default=False,
                        help="Run browser in headless mode (no GUI)")
    parser.add_argument("--pause",           default=5, type=int,
                        help="Seconds between queries")

    parser.add_argument("--all-specialists", action="store_true")
    parser.add_argument("--all-areas",       action="store_true")
    parser.add_argument("--full-run",        action="store_true",
                        help="All specialists × all cities × all areas")
    parser.add_argument("--resume",          action="store_true",
                        help="Skip already-completed combos")

    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    ts   = make_run_folder()      # returns timestamp string, creates both folders
    done = load_progress() if args.resume else set()

    if args.full_run:
        combos = list(product(SPECIALISTS, CITIES, AREAS))
        asyncio.run(run_combinations(combos, args, done, ts))

    elif args.all_specialists and args.all_areas:
        combos = list(product(SPECIALISTS, [args.city], AREAS))
        asyncio.run(run_combinations(combos, args, done, ts))

    elif args.all_specialists:
        combos = list(product(SPECIALISTS, [args.city], [args.area]))
        asyncio.run(run_combinations(combos, args, done, ts))

    else:
        asyncio.run(run_single(args, ts))