from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

try:  # pragma: no cover
    from playwright.async_api import async_playwright
except ImportError:  # pragma: no cover
    async_playwright = None  # type: ignore[assignment]


async def default_browser_factory() -> Any:
    """Launch a headed Chromium browser and return a Playwright Page context manager."""
    if async_playwright is None:  # pragma: no cover
        raise RuntimeError("playwright is not installed; run: playwright install chromium")

    class _PageCtx:
        async def __aenter__(self):
            self._pw_cm = async_playwright()
            pw = await self._pw_cm.__aenter__()
            # Use real installed Chrome to avoid Google's bot detection.
            # Falls back to Chromium if Chrome is not installed.
            try:
                self._browser = await pw.chromium.launch(
                    channel="chrome",
                    headless=False,
                    args=["--disable-blink-features=AutomationControlled"],
                )
            except Exception:
                self._browser = await pw.chromium.launch(
                    headless=False,
                    args=["--disable-blink-features=AutomationControlled"],
                )
            self._context = await self._browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/125.0.0.0 Safari/537.36"
                )
            )
            # Remove the `navigator.webdriver` flag that triggers bot detection
            await self._context.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
            )
            self.page = await self._context.new_page()
            return self

        async def __aexit__(self, *_):
            await self._browser.close()
            await self._pw_cm.__aexit__(None, None, None)

    return _PageCtx()
