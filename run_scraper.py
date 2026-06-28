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
    parser.add_argument("--max-comment-pages", type=int, default=config.DEFAULT_MAX_COMMENT_PAGES)
    parser.add_argument("--headless", action="store_true", help="Run Chromium headless (more auth walls; not recommended for first run).")
    parser.add_argument(
        "--reactors", action="store_true",
        help="Also open each post's reactions modal and harvest reactor names/profiles. Slower.",
    )
    parser.add_argument(
        "--max-reactor-scrolls", type=int, default=10,
        help="Cap on scroll cycles inside the reactors modal (per post). Default 10.",
    )
    parser.add_argument(
        "--output", default=None,
        help="Output directory for per-tab JSON files. Defaults to the repo root.",
    )
    args = parser.parse_args(argv)

    tabs = [t.strip() for t in args.tabs.split(",") if t.strip()]

    try:
        scrape_company(
            url=args.url,
            max_posts=args.max_posts,
            max_comment_pages=args.max_comment_pages,
            headless=args.headless,
            output_path=args.output,
            tabs=tabs,
            collect_reactors=args.reactors,
            max_reactor_scrolls=args.max_reactor_scrolls,
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
