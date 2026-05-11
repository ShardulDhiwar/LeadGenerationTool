import asyncio
from scraper.google_maps import scrape_google_maps

if __name__ == "__main__":
    asyncio.run(scrape_google_maps())