"""DOM extraction -- used ONLY when API capture didn't yield a bucket.

Comment author name resolution runs through five tiers IN ORDER. Each failure
is logged so you can see exactly which tier had to handle a given card. If
all tiers fail, the record is emitted with `author_name=None` and
`name_resolution='failed'` along with the URN -- the string "Unknown User"
is **never** produced.

    Tier A : URN  -> look up in the API profile cache (passed in)
    Tier B : aria-label "View <Name>'s profile" link  (regex strip)
    Tier C : href '/in/<slug>' link visible text
    Tier D : nested span[aria-hidden='true']                 (last resort)
    Tier E : give up -> emit URN + name_resolution='failed'  (no fake name)
"""
from __future__ import annotations

import re
from typing import Any

from playwright.sync_api import Locator, Page

from . import config


_VIEW_RE = re.compile(r"^view[:\s]+", re.IGNORECASE)
# All three trailing patterns LinkedIn uses on author aria-labels.
_PROFILE_SUFFIX_RE = re.compile(
    r"['\u2019]s\s+(?:profile|graphic\s+link|page).*$",
    re.IGNORECASE,
)


def _safe_attr(loc: Locator, name: str) -> str | None:
    try:
        if loc.count() == 0:
            return None
        return loc.first.get_attribute(name, timeout=500)
    except Exception:
        return None


def _read_card_urn(card: Locator) -> str:
    """Read the comment URN from whichever attribute the markup uses."""
    for attr in config.COMMENT_URN_ATTRS:
        val = _safe_attr(card, attr)
        if val and val.startswith("urn:li:comment:"):
            return val
    return ""


def _safe_text(loc: Locator) -> str | None:
    try:
        if loc.count() == 0:
            return None
        t = loc.first.text_content(timeout=500)
        return (t or "").strip() or None
    except Exception:
        return None


def resolve_author(
    card: Locator,
    profile_lookup: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Run the 5-tier author resolution on a single comment card locator."""
    profile_lookup = profile_lookup or {}
    result: dict[str, Any] = {
        "author_urn": "",
        "author_name": None,
        "author_headline": "",
        "author_profile": "",
        "name_resolution": "failed",
    }

    # --- Tier A : URN -> API profile cache ---
    card_urn = _read_card_urn(card)
    if card_urn:
        result["author_urn"] = card_urn
        # comment URN may embed the commenter's profile URN: urn:li:comment:(activity:X,Y) doesn't, but
        # the card usually also contains a nested profile link with the URN.
        inner_urn = _safe_attr(card.locator("[data-entity-urn^='urn:li:fsd_profile'], [data-urn^='urn:li:fsd_profile']"), "data-entity-urn") \
                    or _safe_attr(card.locator("[data-entity-urn^='urn:li:fsd_profile'], [data-urn^='urn:li:fsd_profile']"), "data-urn")
        for cand in (inner_urn, card_urn):
            if cand and cand in profile_lookup:
                p = profile_lookup[cand]
                result.update({
                    "author_urn": cand,
                    "author_name": p.get("name") or None,
                    "author_headline": p.get("headline", ""),
                    "author_profile": p.get("url", ""),
                    "name_resolution": "dom_tier_A_urn",
                })
                return result

    # --- Tier B : aria-label "View <Name>'s profile" ---
    view_link = card.locator(config.COMMENT_AUTHOR_ARIA_VIEW).first
    if view_link.count() > 0:
        label = _safe_attr(view_link, "aria-label") or ""
        href = _safe_attr(view_link, "href") or ""
        if label:
            cleaned = _VIEW_RE.sub("", label).strip()
            cleaned = _PROFILE_SUFFIX_RE.sub("", cleaned).strip()
            # Some variants are "View: Name. Headline"
            if ". " in cleaned and not _PROFILE_SUFFIX_RE.search(label):
                name_part, _, headline_part = cleaned.partition(". ")
                cleaned = name_part.strip()
                result["author_headline"] = headline_part.strip()
            if cleaned:
                result["author_name"] = cleaned
                result["author_profile"] = (href.split("?")[0] if href else "")
                result["name_resolution"] = "dom_tier_B_aria"
                return result

    # --- Tier C : /in/<slug> link visible text ---
    profile_link = card.locator(config.COMMENT_AUTHOR_PROFILE_LINK).first
    if profile_link.count() > 0:
        text = _safe_text(profile_link)
        href = _safe_attr(profile_link, "href") or ""
        # Filter obvious non-names (e.g. "Author", emoji-only, very short noise)
        if text and len(text) > 1 and not text.lower().startswith(("see ", "view ")):
            result["author_name"] = text
            result["author_profile"] = href.split("?")[0]
            result["name_resolution"] = "dom_tier_C_inlink"
            return result

    # --- Tier D : nested aria-hidden span (last resort) ---
    hidden = card.locator(config.COMMENT_AUTHOR_HIDDEN_SPAN).first
    text = _safe_text(hidden)
    if text and len(text) > 1:
        result["author_name"] = text
        result["name_resolution"] = "dom_tier_D_hidden"
        return result

    # --- Tier E : give up. Keep URN if we found one. No fake name. ---
    return result


def extract_company_header(page: Page) -> dict[str, Any]:
    """Sanity check: pull what we can from the visible company header card."""
    out: dict[str, Any] = {}
    for sel in config.COMPANY_HEADER_HOOKS:
        loc = page.locator(sel).first
        if loc.count() == 0:
            continue
        # Name -- first h1 inside
        name = _safe_text(loc.locator("h1"))
        if name:
            out["name"] = name
        # Tagline / subtitle
        tag = _safe_text(loc.locator("p, h2"))
        if tag:
            out.setdefault("tagline", tag)
        break
    return out


def extract_post_text_fallback(post_card: Locator) -> str:
    """When the API didn't capture a post's body, scrape it from the DOM."""
    txt = _safe_text(post_card.locator(config.POST_TEXT))
    return txt or ""


_COUNT_RE = re.compile(r"([\d,]+)")


def _parse_count(label: str) -> int:
    """Pull the first integer (with commas) out of an aria-label like
    '10 reactions' / 'Like, Celebrate and 1,234 reactions' / '12 comments'.
    Returns 0 if nothing parseable.
    """
    if not label:
        return 0
    m = _COUNT_RE.search(label)
    if not m:
        return 0
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return 0


def extract_reactions_from_card(post_card: Locator) -> dict[str, Any]:
    """DOM fallback for reactions on a single post card.
    Returns {'reactions_count': int, 'reactions_label': str}.
    Reads `aria-label` first (richest), then visible text.
    """
    loc = post_card.locator(config.POST_REACTIONS).first
    if loc.count() == 0:
        return {"reactions_count": 0, "reactions_label": ""}
    label = _safe_attr(loc, "aria-label") or ""
    text = _safe_text(loc) or ""
    return {
        "reactions_count": _parse_count(label) or _parse_count(text),
        "reactions_label": label or text,
    }


def extract_comments_count_from_card(post_card: Locator) -> int:
    """DOM fallback for the comments count on a single post card."""
    loc = post_card.locator(config.POST_COMMENTS_COUNT).first
    if loc.count() == 0:
        return 0
    label = _safe_attr(loc, "aria-label") or _safe_text(loc) or ""
    return _parse_count(label)


def extract_reactors_from_modal(page: Page) -> list[dict[str, str]]:
    """Read every reactor row currently rendered in the open reactors modal.

    Returns a list of {name, profile_url, headline}. De-duped by profile_url
    (falls back to name when the URL is missing).
    """
    modal = page.locator(config.REACTORS_MODAL).first
    if modal.count() == 0:
        return []

    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for item in modal.locator(config.REACTOR_ITEM).all():
        name = _safe_text(item.locator(config.REACTOR_NAME)) or ""
        # Strip the "View X's profile" visually-hidden suffix if it leaked in.
        if name:
            name = _PROFILE_SUFFIX_RE.sub("", name).strip()
        href = _safe_attr(item.locator(config.REACTOR_PROFILE_LINK), "href") or ""
        profile_url = href.split("?")[0] if href else ""
        headline = _safe_text(item.locator(config.REACTOR_HEADLINE)) or ""
        if not (name or profile_url):
            continue
        key = profile_url or name
        if key in seen:
            continue
        seen.add(key)
        out.append({"name": name, "profile_url": profile_url, "headline": headline})
    return out


# ---------------------------------------------------------------------------
# Subtraction-based text fallbacks.
# Used ONLY when the selector-based extractors above return empty. They take
# the whole region's visible text and strip known UI chrome phrases.
# This is intentionally lossy -- it's a last resort, not the primary path.
# ---------------------------------------------------------------------------
def _subtract_chrome(raw: str) -> str:
    if not raw:
        return ""
    lines = [ln.strip() for ln in raw.splitlines()]
    chrome = {p.lower() for p in config.LINKEDIN_CHROME_PHRASES}
    kept: list[str] = []
    for ln in lines:
        if not ln:
            continue
        low = ln.lower()
        # Drop lines that are *just* chrome words (with maybe punctuation/counts).
        stripped_for_match = "".join(ch for ch in low if ch.isalpha() or ch.isspace()).strip()
        if stripped_for_match in chrome:
            continue
        # Drop bare reaction-count rows like "1,234" or "12 reactions"
        if low.replace(",", "").replace(".", "").strip().isdigit():
            continue
        kept.append(ln)
    return "\n".join(kept).strip()


def extract_text_by_subtraction(region: Locator) -> str:
    """Take `inner_text()` of the whole region and remove known UI chrome lines."""
    try:
        raw = region.inner_text(timeout=1000)
    except Exception:
        return ""
    return _subtract_chrome(raw or "")
