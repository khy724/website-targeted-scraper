"""End-to-end orchestrator.

The flow:
    1. Launch Chromium with persistent profile + stealth init script.
    2. Attach the response interceptor BEFORE navigating.
    3. For each requested tab (home / about / posts / jobs / products):
         - Build the canonical URL and pin to it (re-navigate if the page drifts).
         - Reset captured payloads.
         - safe_goto, wait for the overview card, scroll/expand if relevant.
         - Build a per-tab record (API-first, DOM-patch) and write
           `scraped_<slug>_<tab>.json`.
    4. Raw payloads stay in `api_dumps/` for learning.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Iterable

from playwright.sync_api import Locator, Page

from . import api_extractor, browser, config, dom_extractor, interceptor
from . import auth
from .auth import handle_auth_wall


_SLUG_RE = re.compile(r"/company/([^/?#]+)")


def _slug_from_url(url: str) -> str:
    m = _SLUG_RE.search(url or "")
    return m.group(1) if m else ""


def _canonical_url(slug: str, tab_path: str) -> str:
    base = f"https://www.linkedin.com/company/{slug}/"
    return base + tab_path


def _pinned_goto(page: Page, target_url: str, slug: str, tab_path: str, attempts: int = 3) -> bool:
    """Navigate and verify the browser actually ended up on a URL containing
    /company/<slug>/<tab_path>. If it drifted (e.g. redirected to a post detail
    view or an auth wall), recover or re-navigate up to `attempts` times.
    Returns True on success.
    """
    from .auth import handle_auth_wall, is_auth_wall

    expected_fragment = f"/company/{slug}/{tab_path}".rstrip("/")
    for i in range(1, attempts + 1):
        browser.safe_goto(page, target_url)
        current = (page.url or "").rstrip("/")
        if expected_fragment in current:
            return True
        # safe_goto already ran handle_auth_wall once, but a JS-driven redirect
        # to /authwall can land *after* that check returned. Re-check now and
        # recover before counting this as a real drift.
        if is_auth_wall(page):
            print(f"[pin] attempt {i}: landed on auth wall ({page.url}); attempting recovery.")
            try:
                handle_auth_wall(page, return_to=target_url)
            except Exception as e:
                print(f"[pin] auth recovery failed: {e}")
                return False
            current = (page.url or "").rstrip("/")
            if expected_fragment in current:
                return True
        print(f"[pin] attempt {i}: page drifted to {page.url!r}; expected fragment {expected_fragment!r}")
        time.sleep(config.SHORT_WAIT_S)
    print(f"[pin] giving up after {attempts} attempts; continuing with {page.url}")
    return False


# ---------------------------------------------------------------------------
# Scroll loop
# ---------------------------------------------------------------------------
def _scroll_feed(page: Page, max_posts: int) -> None:
    """Scroll until we've loaded `max_posts` cards OR plateaued N cycles in a row.

    Alternates a `scrollBy` (incremental, so lazy-loaders that listen for
    scroll events fire) with a `scrollTo(scrollHeight)` (snaps to the bottom
    so IntersectionObservers on the sentinel element trigger).
    """
    plateau = 0
    last_count = 0
    cycle = 0
    while True:
        count = page.locator(config.POST_CARD).count()
        if count >= max_posts:
            print(f"[scroll] reached target {count}/{max_posts} posts.")
            return
        if count == last_count:
            plateau += 1
            if plateau >= config.SCROLL_PLATEAU_CYCLES:
                print(f"[scroll] plateau at {count} posts -- stopping.")
                return
        else:
            print(f"[scroll] loaded {count} posts so far (target {max_posts}).")
            plateau = 0
        last_count = count
        if cycle % 2 == 0:
            page.evaluate(f"window.scrollBy(0, {config.SCROLL_STEP_PX});")
        else:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight);")
        cycle += 1
        time.sleep(config.MEDIUM_WAIT_S)
        handle_auth_wall(page)


def _scroll_until_plateau(
    page: Page,
    max_cycles: int = 15,
    label: str = "tab",
) -> None:
    """Generic scroll for tabs that have no `POST_CARD` (products, jobs, about).

    Uses `document.body.scrollHeight` growth as the plateau signal so it works
    regardless of what cards LinkedIn renders. Smooth scrolling so it's
    visible on demo recordings.
    """
    plateau = 0
    last_height = 0
    for cycle in range(max_cycles):
        try:
            height = int(page.evaluate("document.body.scrollHeight") or 0)
        except Exception:
            height = 0
        if height and height == last_height:
            plateau += 1
            if plateau >= config.SCROLL_PLATEAU_CYCLES:
                print(f"[scroll:{label}] plateau at height={height} after {cycle} cycles -- stopping.")
                return
        else:
            if height:
                print(f"[scroll:{label}] cycle {cycle}: height={height}")
            plateau = 0
        last_height = height
        # Smooth scroll by a step so lazy-loaders that listen for scroll
        # events fire; every other cycle snap to bottom so any sentinel
        # IntersectionObservers trigger too.
        if cycle % 2 == 0:
            page.evaluate(
                f"window.scrollBy({{top: {config.SCROLL_STEP_PX}, behavior: 'smooth'}});"
            )
        else:
            page.evaluate(
                "window.scrollTo({top: document.body.scrollHeight, behavior: 'smooth'});"
            )
        time.sleep(config.MEDIUM_WAIT_S)
        handle_auth_wall(page)
    print(f"[scroll:{label}] hit max_cycles={max_cycles}; stopping.")


# ---------------------------------------------------------------------------
# Per-post comment expansion
# ---------------------------------------------------------------------------
def _click_until_gone(
    page: Page,
    region: Locator,
    selector: str,
    max_clicks: int,
    label: str,
) -> int:
    """Click matching buttons inside `region` until none remain or `max_clicks` reached.
    Returns the number of successful clicks.
    """
    clicks = 0
    for _ in range(max_clicks):
        btn = region.locator(selector).first
        if btn.count() == 0:
            break
        try:
            if not btn.is_visible():
                break
            btn.scroll_into_view_if_needed(timeout=2000)
            btn.click(timeout=2000)
            clicks += 1
            time.sleep(config.SHORT_WAIT_S)
            handle_auth_wall(page)
        except Exception:
            break
    if clicks:
        print(f"[expand] {label}: clicked {clicks}x")
    return clicks


def _expand_post_comments(page: Page, post: Locator, max_pages: int) -> None:
    """Open comments, expand the post body, page through comments,
    then expand any nested reply threads + 'see more' inside comments.
    `max_pages` caps each click loop separately (so e.g. max_pages=3 allows
    3 'Load more comments' clicks AND 3 'Show more replies' clicks).
    """
    # 1. Expand the post body itself ("see more") so DOM fallback gets full text.
    _click_until_gone(page, post, config.POST_SEE_MORE, max_clicks=2, label="post see-more")

    # 2. Open the comments panel.
    toggle = post.locator(config.COMMENT_TOGGLE).first
    if toggle.count() == 0:
        return
    try:
        toggle.scroll_into_view_if_needed(timeout=2000)
        toggle.click(timeout=2000)
    except Exception:
        return
    time.sleep(config.MEDIUM_WAIT_S)
    handle_auth_wall(page)

    # 3. Page through top-level comments.
    _click_until_gone(page, post, config.COMMENT_LOAD_MORE, max_clicks=max_pages, label="load-more-comments")

    # 4. Expand nested reply threads.
    _click_until_gone(page, post, config.COMMENT_REPLY_LOAD_MORE, max_clicks=max_pages, label="show-replies")

    # 5. Expand "see more" inside long comment bodies.
    _click_until_gone(page, post, config.COMMENT_SEE_MORE, max_clicks=max_pages * 4, label="comment see-more")


# ---------------------------------------------------------------------------
# Reactors modal
# ---------------------------------------------------------------------------
def _close_reactors_modal(page: Page) -> None:
    btn = page.locator(config.REACTORS_MODAL_DISMISS).first
    try:
        if btn.count() > 0 and btn.is_visible():
            btn.click(timeout=1500)
            return
    except Exception:
        pass
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass


def _collect_reactors_for_post(
    page: Page,
    post: Locator,
    max_scrolls: int,
) -> list[dict[str, str]]:
    """Click the reactions button on a post, scroll the modal until plateau
    (or `max_scrolls` reached), harvest reactor rows, close the modal.
    Returns [] on any failure.
    """
    btn = post.locator(config.POST_REACTIONS_BUTTON).first
    if btn.count() == 0:
        print("[reactors] no data-reaction-details button on this post -- skipping")
        return []
    try:
        btn.scroll_into_view_if_needed(timeout=2000)
        btn.click(timeout=2500)
    except Exception as e:
        print(f"[reactors] click failed: {e}")
        return []
    try:
        page.locator(config.REACTORS_MODAL).first.wait_for(state="visible", timeout=config.SELECTOR_TIMEOUT_MS)
    except Exception as e:
        print(f"[reactors] modal did not open: {e}")
        _close_reactors_modal(page)
        return []
    time.sleep(config.SHORT_WAIT_S)
    handle_auth_wall(page)

    last_count = 0
    plateau = 0
    # Modal-internal step size. Small enough that each scroll is *visibly*
    # animated in headed/recorded runs (an instant scrollTop = scrollHeight
    # jump is invisible to a 30fps camera -- it lands in a single frame).
    SCROLL_STEP_PX = 600
    # Resolve the real scrollable container at runtime instead of trusting a
    # fixed CSS class. LinkedIn's modal class names rotate, but the *only*
    # element inside the dialog whose scrollHeight exceeds its clientHeight
    # is the reactor list scroller. Walk every descendant once and pick it.
    scroller_finder_js = """
    (modalSel) => {
        const modal = document.querySelector(modalSel);
        if (!modal) return null;
        const candidates = modal.querySelectorAll('*');
        let best = null;
        let bestOverflow = 0;
        for (const el of candidates) {
            const style = getComputedStyle(el);
            const oy = style.overflowY;
            if (oy !== 'auto' && oy !== 'scroll' && oy !== 'overlay') continue;
            const overflow = el.scrollHeight - el.clientHeight;
            if (overflow > bestOverflow && el.clientHeight > 100) {
                best = el;
                bestOverflow = overflow;
            }
        }
        if (!best) return null;
        // Tag it so subsequent scrolls don't re-walk.
        best.setAttribute('data-reactor-scroller', '1');
        return true;
    }
    """
    modal_root_sel = config.REACTORS_MODAL.split(",")[0].strip()
    try:
        found = page.evaluate(scroller_finder_js, modal_root_sel)
    except Exception:
        found = None
    if not found:
        # No overflowing element means the reactor list fits the modal in one
        # screen -- i.e. this post genuinely has only as many reactors as are
        # already visible. Not an error; just nothing to scroll.
        initial = page.locator(config.REACTORS_MODAL).first.locator(config.REACTOR_ITEM).count()
        print(f"[reactors] no scrollable container (list fits in modal) -- {initial} rows total, no scroll needed")
        reactors = dom_extractor.extract_reactors_from_modal(page)
        _close_reactors_modal(page)
        time.sleep(config.SHORT_WAIT_S)
        return reactors
    scroller_sel = "[data-reactor-scroller='1']"
    for cycle in range(max_scrolls):
        items = page.locator(config.REACTORS_MODAL).first.locator(config.REACTOR_ITEM)
        count = items.count()
        if count == last_count:
            plateau += 1
            if plateau >= 2:
                # One last hard-bottom jump in case smooth scroll fell short
                # of triggering the final lazy-load batch.
                try:
                    page.evaluate(
                        "(sel) => { const el = document.querySelector(sel);"
                        " if (el) el.scrollTo({top: el.scrollHeight, behavior: 'smooth'}); }",
                        scroller_sel,
                    )
                    time.sleep(config.MEDIUM_WAIT_S)
                    final_count = page.locator(config.REACTORS_MODAL).first.locator(config.REACTOR_ITEM).count()
                    if final_count > count:
                        # Bottom-jump actually loaded more -- reset plateau and keep going.
                        last_count = final_count
                        plateau = 0
                        continue
                except Exception:
                    pass
                break
        else:
            print(f"[reactors] cycle {cycle}: {count} rows loaded")
            plateau = 0
        last_count = count
        try:
            # Smooth incremental scroll -- visible on camera AND still
            # triggers IntersectionObserver lazy-loads at the sentinel.
            page.evaluate(
                "(args) => { const el = document.querySelector(args.sel);"
                " if (el) el.scrollBy({top: args.dy, behavior: 'smooth'}); }",
                {"sel": scroller_sel, "dy": SCROLL_STEP_PX},
            )
        except Exception:
            pass
        time.sleep(config.SHORT_WAIT_S)
        handle_auth_wall(page)

    reactors = dom_extractor.extract_reactors_from_modal(page)
    print(f"[reactors] harvested {len(reactors)} rows (modal item count: {last_count})")
    _close_reactors_modal(page)
    time.sleep(config.SHORT_WAIT_S)
    return reactors


# ---------------------------------------------------------------------------
# Merge API + DOM
# ---------------------------------------------------------------------------
def _merge(
    page: Page,
    payloads: interceptor.CapturedPayloads,
    slug: str,
    tab: str,
) -> dict[str, Any]:
    company_overview = payloads.bucket("company_overview")
    updates = payloads.bucket("updates")
    comments_buckets = payloads.bucket("comments") + payloads.bucket("updates")
    profile_buckets = payloads.bucket("profile_lookups") + payloads.bucket("comments") + payloads.bucket("updates")
    # Rich Company records show up in many buckets, not just company_overview
    # (e.g. comments payloads carry the post author's company). Scanning wider
    # gives extract_company more chances to fill founded_year, website, logo.
    company_record_sources = (
        company_overview
        + payloads.bucket("graphql_other")
        + payloads.bucket("comments")
        + payloads.bucket("updates")
    )

    company = api_extractor.extract_company(company_record_sources, slug=slug) or {}
    header_dom = dom_extractor.extract_company_header(page)
    for k, v in header_dom.items():
        company.setdefault(k, v)

    # Products (only meaningful on the /products tab, harmless elsewhere).
    products = api_extractor.extract_products(
        payloads.bucket("products") + payloads.bucket("graphql_other")
    )

    posts = api_extractor.extract_posts(updates)
    comments = api_extractor.extract_comments(comments_buckets)
    profile_lookup = api_extractor.build_profile_lookup(profile_buckets)

    # Reactors from the SocialDashReactions endpoint (primary) -- much richer
    # and uncapped vs the DOM-modal scrape. The modal scrape stays as fallback
    # for posts where no reactions payload was captured.
    reactions_buckets = payloads.bucket("reactions")
    api_reactors_by_urn = api_extractor.extract_reactors_by_post(reactions_buckets)
    reactor_totals = api_extractor.reactor_totals(reactions_buckets)

    posts_by_urn = {p["urn"]: p for p in posts}
    dom_comments: list[dict[str, Any]] = []
    for post_card in page.locator(config.POST_CARD).all():
        urn = post_card.get_attribute("data-urn") or ""
        if not urn:
            continue
        post_text = dom_extractor.extract_post_text_fallback(post_card)
        posts_by_urn.setdefault(urn, {
            "urn": urn,
            "author_name": "",
            "author_profile": "",
            "text": post_text,
            "reactions_count": 0,
            "comments_count": 0,
        })
        existing = posts_by_urn[urn]
        if not existing.get("text"):
            existing["text"] = post_text or dom_extractor.extract_text_by_subtraction(post_card)

        # DOM fallback for reactions / comments counts: only fill when API gave 0.
        if not existing.get("reactions_count"):
            r = dom_extractor.extract_reactions_from_card(post_card)
            if r["reactions_count"]:
                existing["reactions_count"] = r["reactions_count"]
            if r["reactions_label"] and not existing.get("reactions_label"):
                existing["reactions_label"] = r["reactions_label"]
        if not existing.get("comments_count"):
            c_count = dom_extractor.extract_comments_count_from_card(post_card)
            if c_count:
                existing["comments_count"] = c_count

        for card in post_card.locator(config.COMMENT_CARD).all():
            author = dom_extractor.resolve_author(card, profile_lookup)
            body = ""
            try:
                body_loc = card.locator(config.COMMENT_TEXT).first
                if body_loc.count() > 0:
                    body = (body_loc.text_content(timeout=500) or "").strip()
            except Exception:
                pass
            if not body:
                body = dom_extractor.extract_text_by_subtraction(card)
            if not body:
                continue
            # Comment URN is in data-id on modern markup, data-urn on legacy.
            comment_urn = ""
            for attr in config.COMMENT_URN_ATTRS:
                v = card.get_attribute(attr) or ""
                if v.startswith("urn:li:comment:"):
                    comment_urn = v
                    break
            dom_comments.append({
                "comment_urn": comment_urn,
                "post_urn": urn,
                "text": body,
                **author,
            })

    seen_urns = {c["comment_urn"] for c in comments if c.get("comment_urn")}
    seen_texts = {(c.get("post_urn"), c.get("text")) for c in comments}
    for dc in dom_comments:
        if dc["comment_urn"] and dc["comment_urn"] in seen_urns:
            continue
        if (dc["post_urn"], dc["text"]) in seen_texts:
            continue
        comments.append(dc)

    posts_list = list(posts_by_urn.values())
    by_post: dict[str, list[dict[str, Any]]] = {}
    for c in comments:
        by_post.setdefault(c.get("post_urn") or "", []).append(c)
    for p in posts_list:
        p["comments"] = by_post.get(p["urn"], [])
        # Attach API-derived reactors here (primary source). The DOM-modal
        # scrape happens later in _scrape_one_tab and only fills gaps for
        # posts the reactions endpoint didn't cover.
        rx = api_reactors_by_urn.get(p["urn"])
        if rx:
            p["reactors"] = rx
            p["reactors_source"] = "api"
        total = reactor_totals.get(p["urn"])
        if total:
            p["reactions_total"] = total

    failed = sum(1 for c in comments if c.get("name_resolution") == "failed")
    print(
        f"[merge:{tab}] posts={len(posts_list)}  comments={len(comments)}  "
        f"unresolved_authors={failed}  reactors_api={sum(len(v) for v in api_reactors_by_urn.values())}  "
        f"products={len(products)}"
    )

    return {
        "source_url": page.url,
        "tab": tab,
        "slug": slug,
        "company": company,
        "posts": posts_list,
        "products": products,
        "stats": {
            "posts": len(posts_list),
            "comments": len(comments),
            "products": len(products),
            "unresolved_authors": failed,
            "reactors_via_api": sum(len(v) for v in api_reactors_by_urn.values()),
            "api_buckets": {
                name: len(payloads.bucket(name))
                for name in (
                    "company_overview", "updates", "comments",
                    "profile_lookups", "reactions",
                    "jobs", "products", "graphql_other",
                )
            },
        },
    }


# ---------------------------------------------------------------------------
# Per-tab scrape
# ---------------------------------------------------------------------------
def _scrape_one_tab(
    page: Page,
    payloads: interceptor.CapturedPayloads,
    slug: str,
    tab: str,
    tab_path: str,
    max_posts: int,
    max_comment_pages: int,
    collect_reactors: bool = False,
    max_reactor_scrolls: int = 25,
) -> dict[str, Any]:
    payloads.reset()
    target = _canonical_url(slug, tab_path)
    print(f"[main] tab={tab}  -> {target}")
    pinned = _pinned_goto(page, target, slug, tab_path)

    # Detect LinkedIn redirecting us off the requested sub-tab (e.g. company
    # has no /products/ -> LinkedIn JS jumps us to /jobs/). Without this guard
    # we'd happily scrape the destination tab and label the output as the
    # source tab, producing wrong data.
    current = (page.url or "").lower()
    expected_suffix = tab_path.rstrip("/").lower()
    if expected_suffix and expected_suffix not in current:
        msg = (
            f"redirected off /{expected_suffix}/ to {page.url} -- "
            f"company likely has no {tab!r} tab; skipping."
        )
        print(f"[main:{tab}] {msg}")
        return {"tab": tab, "slug": slug, "skipped": True, "reason": msg, "final_url": page.url}

    for sel in config.COMPANY_HEADER_HOOKS:
        try:
            page.locator(sel).first.wait_for(state="visible", timeout=config.SELECTOR_TIMEOUT_MS)
            break
        except Exception:
            continue
    time.sleep(config.SHORT_WAIT_S)

    reactors_by_urn: dict[str, list[dict[str, str]]] = {}
    # Posts/home tabs benefit from scroll + comment expansion. Others don't.
    if tab in ("home", "posts"):
        _scroll_feed(page, max_posts)
        # Snapshot URNs upfront. We CANNOT cache Locator objects across the
        # per-post loop because (a) _expand_post_comments injects hundreds
        # of comment nodes that shift sibling indices, and (b) LinkedIn's
        # feed virtualization detaches off-screen cards. Both break any
        # subsequent `.nth(i)` resolution with a 15s timeout. Re-locating
        # by data-urn before each operation is virtualization-stable.
        raw_cards = page.locator(config.POST_CARD).all()[:max_posts]
        post_urns: list[str] = []
        for c in raw_cards:
            try:
                u = c.get_attribute("data-urn", timeout=2000) or ""
            except Exception:
                u = ""
            if u and u not in post_urns:
                post_urns.append(u)
        print(f"[main:{tab}] captured {len(post_urns)} post URNs to process")

        for i, urn in enumerate(post_urns, 1):
            # Re-locate by URN -- survives any DOM churn caused by previous
            # iterations. Falls back to .first since data-urn is unique.
            card = page.locator(f"[data-urn='{urn}']").first
            try:
                card.scroll_into_view_if_needed(timeout=3000)
            except Exception:
                # If we can't even bring the card into view it's probably
                # been virtualized away or removed; skip.
                print(f"[main:{tab}] post {i}/{len(post_urns)} ({urn}): card no longer in DOM; skipping")
                continue
            print(f"[main:{tab}] expanding comments on post {i}/{len(post_urns)}")
            try:
                _expand_post_comments(page, card, max_comment_pages)
            except Exception as e:
                print(f"[main:{tab}] post {i}: expand_comments failed ({e}); continuing")
            if collect_reactors:
                # Re-locate again before the reactor step -- comment expansion
                # also moves siblings.
                card = page.locator(f"[data-urn='{urn}']").first
                try:
                    rx = _collect_reactors_for_post(page, card, max_reactor_scrolls)
                except Exception as e:
                    print(f"[main:{tab}] post {i}: collect_reactors failed ({e}); continuing")
                    rx = []
                if rx:
                    reactors_by_urn[urn] = rx
                    print(f"[main:{tab}] post {i}: collected {len(rx)} reactors")
    else:
        # No POST_CARD on this tab (products / jobs / about). Use generic
        # height-based plateau scroll -- the old 3-iteration window.scrollBy
        # finished in ~3.6s, which wasn't enough time for Voyager to fire
        # the products/jobs query, so we ended up with empty tabs.
        _scroll_until_plateau(page, max_cycles=15, label=tab)

    time.sleep(config.MEDIUM_WAIT_S)  # let trailing responses arrive
    result = _merge(page, payloads, slug=slug, tab=tab)
    # _merge has already attached API-derived reactors. The DOM-modal scrape
    # only fills gaps for posts the reactions endpoint didn't cover.
    if reactors_by_urn:
        for post in result.get("posts", []):
            if post.get("reactors"):
                continue  # API already filled this
            rx = reactors_by_urn.get(post.get("urn", ""))
            if rx:
                post["reactors"] = rx
                post["reactors_source"] = "dom_modal"
    result.setdefault("stats", {})["posts_with_reactors"] = sum(
        1 for p in result.get("posts", []) if p.get("reactors")
    )
    result["pinned"] = pinned
    return result


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def scrape_company(
    url: str,
    max_posts: int = config.DEFAULT_MAX_POSTS,
    max_comment_pages: int = config.DEFAULT_MAX_COMMENT_PAGES,
    headless: bool = False,
    output_path: Path | str | None = None,
    tabs: Iterable[str] = config.DEFAULT_TABS,
    collect_reactors: bool = False,
    max_reactor_scrolls: int = 25,
    record: bool = False,
) -> dict[str, dict[str, Any]]:
    """Scrape one or more sub-tabs of a LinkedIn company page.

    Writes one JSON per tab named `scraped_<slug>_<tab>.json` next to the
    repo root. Returns a dict keyed by tab name.

    `output_path`: if provided, must be a directory; per-tab files are
    written inside it. If None, files land next to `config.OUTPUT_FILE`.
    """
    config.API_DUMP_DIR.mkdir(parents=True, exist_ok=True)

    slug = _slug_from_url(url)
    if not slug:
        raise ValueError(f"Could not extract company slug from URL: {url!r}")

    tab_map = dict(config.TAB_PATHS)
    requested = [t for t in tabs if t in tab_map]
    if not requested:
        raise ValueError(f"No valid tabs in {list(tabs)!r}; pick from {list(tab_map)!r}")

    out_dir = Path(output_path) if output_path else config.SCRAPED_DATA_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    # Pre-flight: if the persistent profile has no live `li_at` cookie, do a
    # one-shot login inside this same browser launch so the user doesn't have
    # to run `--login` separately. handle_auth_wall on the first nav covers
    # cookie-expired / session-rotated cases.
    if not auth.profile_seems_authenticated():
        print(
            f"[main] persistent profile at {config.USER_DATA_DIR} is missing or "
            "has no li_at cookie -- running login flow before scraping."
        )

    results: dict[str, dict[str, Any]] = {}

    with browser.launch(headless=headless, record=record) as (context, page):
        # Attach to the context, not just the page: catches popups, popup-opened
        # docs, and any service-worker JSON fetches that bypass the page scope.
        payloads = interceptor.attach(context, dump_dir=config.API_DUMP_DIR)

        # If we flagged the profile as un-authed above, drive a login first.
        # handle_auth_wall will run auto_login (and manual-pause on MFA).
        if not auth.profile_seems_authenticated():
            print("[main] pre-flight: driving /login -> /feed/ inside recorded context")
            try:
                page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")
                try:
                    page.wait_for_load_state("load", timeout=5000)
                except Exception:
                    pass
                print(f"[main] pre-flight landed at {page.url}")
                if "/feed" not in (page.url or ""):
                    auth.handle_auth_wall(page, return_to="https://www.linkedin.com/feed/")
                else:
                    print("[main] pre-flight: already on /feed/, no auth wall handler needed")
            except Exception as e:
                print(f"[main] pre-flight login failed: {e} -- continuing; runtime auth recovery may still catch it.")
        else:
            print("[main] pre-flight check inside recorded context: profile IS authenticated, skipping login goto")

        for tab in requested:
            try:
                result = _scrape_one_tab(
                    page, payloads, slug, tab, tab_map[tab],
                    max_posts, max_comment_pages,
                    collect_reactors=collect_reactors,
                    max_reactor_scrolls=max_reactor_scrolls,
                )
            except Exception as e:
                print(f"[main:{tab}] failed: {e}")
                result = {"tab": tab, "slug": slug, "error": str(e)}
            results[tab] = result

            tab_file = out_dir / f"scraped_{slug}_{tab}.json"
            with open(tab_file, "w", encoding="utf-8") as f:
                json.dump(result, f, indent=2, ensure_ascii=False)
            print(f"[main] wrote {tab_file}")

    return results
