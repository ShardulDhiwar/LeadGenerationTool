"""
main.py  —  LeadGenerationTool
─────────────────────────────────────────────────────────────────
Supported scraping sources:
  • google_maps  — Google Maps scraper
  • practo       — Practo scraper
  • both         — Google Maps + Practo (deduped merge output)
  • justdial     — JustDial scraper  (⚠️ work in progress — do not use via dashboard)
  • all          — All three sources  (⚠️ includes justdial — CLI only)

Each run creates a timestamped folder inside outputs/:

  outputs/
  ├── 2026-05-12_11-33-10/
  │   ├── gmaps_dentist_Pune_Baner.json / .csv
  │   ├── practo_dentist_Pune_Baner.json / .csv
  │   └── merged_dentist_Pune_Baner.json / .csv   ← deduped (source=both)
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


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def make_run_folder() -> str:
    ts     = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    folder = os.path.join("outputs", ts)
    os.makedirs(folder, exist_ok=True)
    print(f"📁 Run folder: {folder}")
    return folder


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


def save_merged(leads: list[dict], path_csv: str):
    if not leads:
        return
    path_json = path_csv.replace(".csv", ".json")
    with open(path_json, "w", encoding="utf-8") as f:
        json.dump(leads, f, ensure_ascii=False, indent=2)
    with open(path_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=leads[0].keys())
        writer.writeheader()
        writer.writerows(leads)
    print(f"  [MERGE] Saved {len(leads)} deduped leads → {path_csv}")


# ─────────────────────────────────────────────────────────────────────────────
# FIX 4: Source resolver
#   "both" → google_maps + practo   (justdial excluded — scraper not ready)
#   "all"  → all three              (CLI only, not exposed via dashboard)
# ─────────────────────────────────────────────────────────────────────────────

def _resolve_sources(source: str) -> list[str]:
    if source == "both":
        return ["google_maps", "practo"]     # ← justdial intentionally excluded
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

async def run_single(args, run_folder: str):
    common = dict(
        specialty     = args.specialty,
        city          = args.city,
        area          = args.area,
        max_listings  = args.max,
        output_dir    = run_folder,
        user_data_dir = args.user_data_dir,
    )

    sources    = _resolve_sources(args.source)
    all_leads: list[dict] = []

    for source in sources:
        leads = await _run_source(source, common)
        all_leads.extend(leads)

    if len(sources) > 1 and all_leads:
        slug   = f"merged_{args.specialty}_{args.city}_{args.area}".replace(" ", "_").strip("_")
        merged = dedupe_leads(all_leads)
        save_merged(merged, f"{run_folder}/{slug}.csv")
        print(f"\n[MERGE] {len(merged)} unique leads from {len(sources)} sources")


# ─────────────────────────────────────────────────────────────────────────────
# Combination runs
# ─────────────────────────────────────────────────────────────────────────────

async def run_combinations(combos: list, args, done: set, run_folder: str):
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
                output_dir    = run_folder,
                user_data_dir = args.user_data_dir,
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
            slug   = f"merged_{specialty}_{city}_{area}".replace(" ", "_").strip("_")
            merged = dedupe_leads(all_leads)
            save_merged(merged, f"{run_folder}/{slug}.csv")

        if i < total:
            print(f"  Waiting {args.pause}s...")
            await asyncio.sleep(args.pause)

    print(f"\n{'─' * 55}")
    print(f"  Done! Completed: {completed} | Skipped: {skipped}")
    print(f"  Files in: {run_folder}")
    print(f"{'─' * 55}\n")


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(description="Doctor Lead Generation Tool")

    # FIX 4: "both" added as a first-class choice (gmaps + practo, no justdial)
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
    args       = parse_args()
    run_folder = make_run_folder()
    done       = load_progress() if args.resume else set()

    if args.full_run:
        combos = list(product(SPECIALISTS, CITIES, AREAS))
        asyncio.run(run_combinations(combos, args, done, run_folder))

    elif args.all_specialists and args.all_areas:
        combos = list(product(SPECIALISTS, [args.city], AREAS))
        asyncio.run(run_combinations(combos, args, done, run_folder))

    elif args.all_specialists:
        combos = list(product(SPECIALISTS, [args.city], [args.area]))
        asyncio.run(run_combinations(combos, args, done, run_folder))

    else:
        asyncio.run(run_single(args, run_folder))