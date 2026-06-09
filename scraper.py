"""
scraper.py — CI entry point for GitHub Actions.
Runs the Google Maps scraper headlessly and saves to output/.
"""

import asyncio
import os
import sys
import argparse
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from scraper.google_maps import scrape_google_maps


def main():
    parser = argparse.ArgumentParser(description="Google Maps Scraper (CI)")
    parser.add_argument("--specialty", default="dentist")
    parser.add_argument("--city", default="Pune")
    parser.add_argument("--area", default="")
    parser.add_argument("--max", default=20, type=int)
    args = parser.parse_args()

    output_dir = "output"
    os.makedirs(output_dir, exist_ok=True)

    date_str = datetime.now().strftime("%Y-%m-%d")

    leads = asyncio.run(scrape_google_maps(
        specialty=args.specialty,
        city=args.city,
        area=args.area,
        max_listings=args.max,
        output_dir=output_dir,
        headless=True,
    ))

    json_path = os.path.join(output_dir, f"data_{date_str}.json")
    csv_path = os.path.join(output_dir, f"data_{date_str}.csv")

    from scraper.utils import save_json, save_csv
    save_json(leads, json_path)
    save_csv(leads, csv_path)

    print(f"\n[CI] Done — {len(leads)} leads saved to {output_dir}/")


if __name__ == "__main__":
    main()
