"""Chromium launch + safe navigation wrapper."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime

from playwright.sync_api import BrowserContext, Page, sync_playwright

from . import config
from .auth import handle_auth_wall


# Default size for recorded demo videos. 1440x900 reads well on most screens
# and matches our usual viewport so the recording isn't letterboxed.
_RECORD_VIDEO_SIZE = {"width": 1440, "height": 900}
# Slowing every Playwright operation by this many ms makes scroll/click
# interactions legible on camera without making the run unbearable.
_RECORD_SLOW_MO_MS = 120


@contextmanager
def launch(headless: bool = False, record: bool = False):
    """Yield (context, page) with persistent profile + stealth init script.

    When `record=True`, writes a `.webm` per page into
    `<repo>/demo_videos/<UTC-timestamp>/` and slows Playwright actions
    by ~120ms so demo motion is visible. Video files are only finalized
    on clean context close -- don't Ctrl-C mid-run if you want the file.
    """
    record_dir = None
    if record:
        record_dir = config.REPO_ROOT / "demo_videos" / datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
        record_dir.mkdir(parents=True, exist_ok=True)
        print(f"[browser] recording video to {record_dir}")

    with sync_playwright() as p:
        config.USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
        launch_kwargs = dict(
            user_data_dir=str(config.USER_DATA_DIR),
            headless=headless,
            args=config.LAUNCH_ARGS,
            user_agent=config.USER_AGENT,
            viewport=config.VIEWPORT,
            no_viewport=not headless,  # let the window decide its size when visible
        )
        if record:
            launch_kwargs["record_video_dir"] = str(record_dir)
            launch_kwargs["record_video_size"] = _RECORD_VIDEO_SIZE
            launch_kwargs["slow_mo"] = _RECORD_SLOW_MO_MS
            # Force a fixed viewport when recording so the .webm isn't tiny
            # if the headed window comes up smaller than _RECORD_VIDEO_SIZE.
            launch_kwargs["viewport"] = _RECORD_VIDEO_SIZE
            launch_kwargs["no_viewport"] = False
        context: BrowserContext = p.chromium.launch_persistent_context(**launch_kwargs)
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
            if record and record_dir is not None:
                print(f"[browser] video saved under {record_dir}")


def safe_goto(page: Page, url: str) -> None:
    """Navigate, then run the auth-wall handler. Use everywhere instead of `page.goto`.

    domcontentloaded fires before LinkedIn's JS-driven `trk=bf` fingerprint
    redirect rewrites the URL, so we additionally wait briefly for the `load`
    state. Without this, is_auth_wall sees the original /company URL and
    returns False even though the page is mid-flight to /authwall.

    Tolerates 'Navigation interrupted by another navigation' errors, which
    arise in two distinct cases:
        1. LinkedIn JS redirects mid-flight when the requested sub-tab
           doesn't exist for a company (e.g. /products/ on a company that
           only has /jobs/). We accept the landing URL; the caller's
           tab-drift check in _scrape_one_tab will mark it as skipped.
        2. A PRIOR safe_goto returned while a navigation was still pending
           (the `load` wait silently timed out). The pending nav then
           collides with our new goto. We resolve this by waiting for the
           network to quiesce and retrying once.
    """
    INTERRUPTED = "interrupted by another navigation"

    def _wait_settled() -> None:
        # Give in-flight navs a chance to commit. We try `load` first
        # (waits for window.load), then `networkidle` as a fallback so we
        # don't depart while LinkedIn's lazy XHR cascade is still firing.
        try:
            page.wait_for_load_state("load", timeout=10_000)
        except Exception:
            pass
        try:
            page.wait_for_load_state("networkidle", timeout=5_000)
        except Exception:
            pass

    try:
        page.goto(url, wait_until="domcontentloaded")
    except Exception as e:
        msg = str(e)
        if INTERRUPTED not in msg:
            raise
        print(f"[browser] safe_goto: nav to {url} was interrupted; settling then retrying.")
        _wait_settled()
        # One retry. If THIS one is also interrupted, accept the landing URL
        # silently -- it means LinkedIn really doesn't want us at `url`
        # (e.g. tab doesn't exist) and the drift check will skip it.
        try:
            page.goto(url, wait_until="domcontentloaded")
        except Exception as e2:
            if INTERRUPTED in str(e2):
                print(f"[browser] safe_goto: retry also interrupted; landed at {page.url}")
            else:
                raise

    _wait_settled()
    handle_auth_wall(page, return_to=url)
