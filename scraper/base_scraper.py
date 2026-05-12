import asyncio
from playwright.async_api import async_playwright, BrowserContext, Page
from abc import ABC, abstractmethod


class BaseScraper(ABC):
    """
    Base class for all scrapers.
    Handles browser lifecycle, retries, and shared utilities.
    """

    def __init__(self, headless: bool = False, slow_mo: int = 80):
        self.headless = headless
        self.slow_mo = slow_mo
        self.browser: BrowserContext | None = None
        self.page: Page | None = None

    async def start(self, user_data_dir: str = "C:/playwright-profile"):
        """Launch persistent browser context (keeps your Google login)."""
        self._playwright = await async_playwright().start()
        self.browser = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=user_data_dir,
            headless=self.headless,
            slow_mo=self.slow_mo,
            args=["--start-maximized"],
        )
        self.page = await self.browser.new_page()
        print("✅ Browser started")

    async def stop(self):
        """Gracefully close browser."""
        if self.browser:
            await self.browser.close()
        if hasattr(self, "_playwright"):
            await self._playwright.stop()
        print("✅ Browser closed")

    async def safe_text(self, element, selector: str, default: str = "N/A") -> str:
        """Extract inner text safely — returns default if selector not found."""
        try:
            el = await element.query_selector(selector)
            if el:
                return (await el.inner_text()).strip()
        except Exception:
            pass
        return default

    async def safe_attr(self, element, selector: str, attr: str, default: str = "N/A") -> str:
        """Extract attribute safely — returns default if not found."""
        try:
            el = await element.query_selector(selector)
            if el:
                val = await el.get_attribute(attr)
                return val.strip() if val else default
        except Exception:
            pass
        return default

    async def retry(self, coro_fn, retries: int = 3, delay: float = 2.0):
        """Retry an async coroutine up to `retries` times."""
        for attempt in range(1, retries + 1):
            try:
                return await coro_fn()
            except Exception as e:
                print(f"  ⚠️  Attempt {attempt}/{retries} failed: {e}")
                if attempt < retries:
                    await asyncio.sleep(delay)
        return None

    @abstractmethod
    async def scrape(self, **kwargs):
        """Every scraper must implement this."""
        raise NotImplementedError