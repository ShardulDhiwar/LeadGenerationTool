import asyncio
from playwright.async_api import async_playwright
import json
import os

async def scrape_google_maps():
    async with async_playwright() as p:

        browser = await p.chromium.launch_persistent_context(
            user_data_dir="C:/playwright-profile",
            headless=False,
            slow_mo=100
        )

        page = await browser.new_page()

        search_query = "dentist in Pune"

        print("Opening Google Maps...")
        await page.goto("https://www.google.com/maps")

        await page.wait_for_timeout(10000)

        print("Typing search...")
        await page.keyboard.type(search_query, delay=100)
        await page.keyboard.press("Enter")

        await page.wait_for_timeout(8000)

        # Scroll
        for _ in range(5):
            await page.mouse.wheel(0, 5000)
            await page.wait_for_timeout(2000)

        leads = []

        listings = await page.query_selector_all('//div[@role="article"]')
        print(f"Found {len(listings)} listings")

        for listing in listings[:10]:
            try:
                # 🔥 BEST NAME EXTRACTION
                a_tag = await listing.query_selector('a.hfpxzc')
                name = await a_tag.get_attribute("aria-label") if a_tag else "N/A"

                # Rating
                rating = "N/A"
                rating_el = await listing.query_selector('span[aria-label*="stars"]')
                if rating_el:
                    rating = await rating_el.get_attribute("aria-label")

                leads.append({
                    "name": name,
                    "rating": rating
                })

            except Exception as e:
                print("Error:", e)

        # Save
        os.makedirs("data", exist_ok=True)
        with open("data/leads.json", "w", encoding="utf-8") as f:
            json.dump(leads, f, indent=4, ensure_ascii=False)

        print(f"\n✅ Scraped {len(leads)} leads successfully!")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(scrape_google_maps())
    