"""CLI entry point for the LinkedIn company scraper.

Usage:
    python run_scraper.py --url https://www.linkedin.com/company/<slug>/
    python run_scraper.py --url <url> --tabs home,about,posts
    python run_scraper.py --url <url> --max-posts 5 --max-comment-pages 2 --headless

Writes one file per tab into the output directory: scraped_<slug>_<tab>.json
"""
from __future__ import annotations

import argparse
import sys

from scraper import config
from scraper.main import scrape_company


LOGIN_URL = "https://www.linkedin.com/login"
FEED_URL_FRAGMENT = "/feed"


def _do_login(headless: bool) -> int:
    """Open Chromium, run auto_login, wait for /feed (or manual completion).

    Used to seed the persistent profile when the scraper keeps hitting /authwall.
    Cookies are stored in `config.USER_DATA_DIR`; subsequent scrape runs reuse them.
    """
    from scraper import browser as _browser
    from scraper.auth import handle_auth_wall, is_auth_wall, profile_seems_authenticated

    if profile_seems_authenticated():
        print(f"[login] persistent profile at {config.USER_DATA_DIR} already has a live li_at cookie; skipping.")
        return 0

    print(f"[login] opening Chromium against {config.USER_DATA_DIR}")
    with _browser.launch(headless=headless) as (_context, page):
        try:
            page.goto(LOGIN_URL, wait_until="domcontentloaded")
            try:
                page.wait_for_load_state("load", timeout=5000)
            except Exception:
                pass
            # If we're already authed (cookie present), goto resolves to /feed.
            if FEED_URL_FRAGMENT in (page.url or ""):
                print(f"[login] already logged in -> {page.url}")
                return 0
            handle_auth_wall(page, return_to=LOGIN_URL)
            if is_auth_wall(page):
                print(f"[login] still walled at {page.url} after recovery; aborting.")
                return 1
            print(f"[login] success -> {page.url}")
            return 0
        except Exception as e:
            print(f"[login] failed: {e}")
            return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scrape one or more sub-tabs of a LinkedIn company page.")
    parser.add_argument(
        "--url", type=str,
        default="https://www.linkedin.com/company/elevenlabsio/",
        help="LinkedIn company URL, e.g. https://www.linkedin.com/company/google/",
    )
    parser.add_argument(
        "--tabs", type=str, default=",".join(config.DEFAULT_TABS),
        help=f"Comma-separated tab names. Available: {','.join(name for name, _ in config.TAB_PATHS)}",
    )
    parser.add_argument("--max-posts", type=int, default=config.DEFAULT_MAX_POSTS)
    parser.add_argument(
        "--all-posts", action="store_true",
        help=(
            "Scroll the feed until plateau (no new posts loaded for 2 consecutive "
            f"scroll cycles) or safety cap of {config.EXHAUSTIVE_POSTS} posts. "
            "Overrides --max-posts."
        ),
    )
    parser.add_argument("--max-comment-pages", type=int, default=config.DEFAULT_MAX_COMMENT_PAGES)
    parser.add_argument("--headless", action="store_true", help="Run Chromium headless (more auth walls; not recommended for first run).")
    parser.add_argument(
        "--reactors", action="store_true",
        help="Also open each post's reactions modal and harvest reactor names/profiles. Slower.",
    )
    parser.add_argument(
        "--max-reactor-scrolls", type=int, default=25,
        help="Cap on scroll cycles inside the reactors modal (per post). Default 25.",
    )
    parser.add_argument(
        "--all-reactors", action="store_true",
        help=(
            "Scrape EVERY reactor per post (stops on plateau or safety cap of "
            f"{config.EXHAUSTIVE_REACTOR_SCROLLS} scrolls). Implies --reactors and "
            "overrides --max-reactor-scrolls. Much slower; use selectively."
        ),
    )
    parser.add_argument(
        "--all-comments", action="store_true",
        help=(
            "Load EVERY comment per post (stops on plateau or safety cap of "
            f"{config.EXHAUSTIVE_COMMENT_PAGES} page clicks). Overrides --max-comment-pages."
        ),
    )
    parser.add_argument(
        "--output", default=None,
        help=f"Output directory for per-tab JSON files. Defaults to {config.SCRAPED_DATA_DIR.name}/.",
    )
    parser.add_argument(
        "--login", action="store_true",
        help=(
            "One-time login mode. Opens Chromium against the persistent profile, "
            "navigates to /login, fills credentials from .env, then exits. "
            "Run this first if you keep being redirected to /authwall."
        ),
    )
    parser.add_argument(
        "--record", action="store_true",
        help=(
            "Record a Playwright video of the browser session. Writes one .webm "
            "per page into demo_videos/<UTC-timestamp>/, slows actions ~120ms so "
            "motion is visible, and forces a 1440x900 viewport. Let the run "
            "finish cleanly -- the video is only finalized on context close."
        ),
    )
    args = parser.parse_args(argv)

    if args.login:
        return _do_login(headless=args.headless)

    tabs = [t.strip() for t in args.tabs.split(",") if t.strip()]

    # Translate --all-* shortcuts into the underlying numeric caps.
    collect_reactors = args.reactors or args.all_reactors
    max_posts = config.EXHAUSTIVE_POSTS if args.all_posts else args.max_posts
    max_reactor_scrolls = (
        config.EXHAUSTIVE_REACTOR_SCROLLS if args.all_reactors else args.max_reactor_scrolls
    )
    max_comment_pages = (
        config.EXHAUSTIVE_COMMENT_PAGES if args.all_comments else args.max_comment_pages
    )
    if args.all_posts:
        print(f"[run] --all-posts set: scroll until plateau or safety cap of {max_posts} posts.")
    if args.all_reactors:
        print(f"[run] --all-reactors set: capped at {max_reactor_scrolls} scrolls per post (plateau-stop).")
    if args.all_comments:
        print(f"[run] --all-comments set: capped at {max_comment_pages} 'Load more' clicks per post (plateau-stop).")

    try:
        scrape_company(
            url=args.url,
            max_posts=max_posts,
            max_comment_pages=max_comment_pages,
            headless=args.headless,
            output_path=args.output,
            tabs=tabs,
            collect_reactors=collect_reactors,
            max_reactor_scrolls=max_reactor_scrolls,
            record=args.record,
        )
    except KeyboardInterrupt:
        print("\n[run] interrupted by user.")
        return 130
    except Exception as e:
        print(f"[run] failed: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
