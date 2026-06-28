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
    args = parser.parse_args(argv)

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
