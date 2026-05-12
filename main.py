"""
main.py  —  LeadGenerationTool
─────────────────────────────────────────────────────────────────
Each run creates a timestamped folder inside outputs/:

  outputs/
  ├── 2026-05-12_11-33-10/        ← one folder per run
  │   ├── dentist_Pune_Baner.json
  │   ├── dentist_Pune_Baner.csv
  │   ├── cardiologist_Pune_Baner.json
  │   └── cardiologist_Pune_Baner.csv
  ├── 2026-05-12_15-20-05/
  │   └── ...

Usage:

  python main.py                                                  # dentist, Pune
  python main.py --specialty "cardiologist" --city "Mumbai" --area "Bandra"
  python main.py --all-specialists --city "Pune"
  python main.py --all-specialists --city "Pune" --all-areas
  python main.py --full-run
  python main.py --full-run --resume                              # continue interrupted run
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import asyncio
import argparse
import json
from datetime import datetime
from itertools import product
from scraper.google_maps import scrape_google_maps
from config.specialists import SPECIALISTS
from config.cities import CITIES
from config.areas import AREAS

PROGRESS_FILE = "outputs/.progress.json"


def make_run_folder() -> str:
    """
    Create outputs/YYYY-MM-DD_HH-MM-SS/ and return its path.
    Called once when the script starts — all files this run go here.
    """
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    folder = os.path.join("outputs", ts)
    os.makedirs(folder, exist_ok=True)
    print(f"📁 Run folder: {folder}")
    return folder


def load_progress() -> set:
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE, "r") as f:
            return set(json.load(f))
    return set()


def save_progress(done: set):
    os.makedirs("outputs", exist_ok=True)
    with open(PROGRESS_FILE, "w") as f:
        json.dump(list(done), f)


def combo_key(specialty, city, area) -> str:
    return f"{specialty}|{city}|{area}"


def parse_args():
    parser = argparse.ArgumentParser(description="Doctor Lead Generation Tool")

    parser.add_argument("--specialty",      default="dentist",               help="Single specialty to search")
    parser.add_argument("--city",           default="Pune",                  help="City to search in")
    parser.add_argument("--area",           default="",                      help="Area/neighbourhood (optional)")
    parser.add_argument("--max",            default=20,      type=int,       help="Max listings per query")
    parser.add_argument("--user-data-dir",  default="C:/playwright-profile", help="Playwright profile directory")
    parser.add_argument("--pause",          default=5,       type=int,       help="Seconds to wait between queries")

    parser.add_argument("--all-specialists", action="store_true", help="Loop all specialists (single city/area)")
    parser.add_argument("--all-areas",       action="store_true", help="Also loop all areas from config")
    parser.add_argument("--full-run",        action="store_true", help="All specialists x all cities x all areas")
    parser.add_argument("--resume",          action="store_true", help="Skip already-completed combos")

    return parser.parse_args()


async def run_single(args, run_folder: str):
    await scrape_google_maps(
        specialty=args.specialty,
        city=args.city,
        area=args.area,
        max_listings=args.max,
        output_dir=run_folder,
        user_data_dir=args.user_data_dir,
    )


async def run_combinations(combos: list, args, done: set, run_folder: str):
    total = len(combos)
    completed = 0
    skipped = 0

    print(f"\n{'─' * 55}")
    print(f"  Combination engine starting")
    print(f"  Total combos : {total}")
    print(f"  Already done : {len(done)} (will skip these)")
    print(f"{'─' * 55}\n")

    for i, (specialty, city, area) in enumerate(combos, 1):
        key = combo_key(specialty, city, area)

        if key in done:
            skipped += 1
            print(f"  [{i}/{total}] Skipping: {specialty} | {city} | {area or 'no area'}")
            continue

        print(f"\n  [{i}/{total}] {specialty} | {city} | {area or 'no area'}")

        try:
            await scrape_google_maps(
                specialty=specialty,
                city=city,
                area=area,
                max_listings=args.max,
                output_dir=run_folder,
                user_data_dir=args.user_data_dir,
            )
            done.add(key)
            save_progress(done)
            completed += 1
        except Exception as e:
            print(f"  ERROR: {e} — continuing to next combo")

        if i < total:
            print(f"  Waiting {args.pause}s...")
            await asyncio.sleep(args.pause)

    print(f"\n{'─' * 55}")
    print(f"  Run complete! Scraped: {completed} | Skipped: {skipped} | Failed: {total - completed - skipped}")
    print(f"  All files saved in: {run_folder}")
    print(f"{'─' * 55}\n")


if __name__ == "__main__":
    args = parse_args()
    run_folder = make_run_folder()          # ← created ONCE per command
    done = load_progress() if args.resume else set()

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