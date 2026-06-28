"""Chromium launch + safe navigation wrapper."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from playwright.sync_api import BrowserContext, Page, sync_playwright

from . import config
from .auth import handle_auth_wall


@contextmanager
def launch(headless: bool = False):
    """Yield (context, page) with persistent profile + stealth init script."""
    with sync_playwright() as p:
        config.USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
        context: BrowserContext = p.chromium.launch_persistent_context(
            user_data_dir=str(config.USER_DATA_DIR),
            headless=headless,
            args=config.LAUNCH_ARGS,
            user_agent=config.USER_AGENT,
            viewport=config.VIEWPORT,
            no_viewport=not headless,  # let the window decide its size when visible
        )
        context.set_default_timeout(config.SELECTOR_TIMEOUT_MS)
        context.set_default_navigation_timeout(config.NAV_TIMEOUT_MS)

        page: Page = context.pages[0] if context.pages else context.new_page()
        page.add_init_script(config.STEALTH_INIT_SCRIPT)
        try:
            yield context, page
        finally:
            try:
                context.close()
            except Exception:
                pass


def safe_goto(page: Page, url: str) -> None:
    """Navigate, then run the auth-wall handler. Use everywhere instead of `page.goto`."""
    page.goto(url, wait_until="domcontentloaded")
    handle_auth_wall(page, return_to=url)
