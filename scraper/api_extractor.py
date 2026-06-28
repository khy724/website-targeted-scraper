"""Extract structured data from raw Voyager/GraphQL payloads.

LinkedIn payloads use an `included[]` array of typed records cross-referenced
by `entityUrn`. The pattern: build a URN->record index once, then walk the
records we care about and resolve their references via that index.

These functions have **no Playwright dependency** -- you can call them on
JSON files saved in `api_dumps/` for offline experimentation.
"""
from __future__ import annotations

import re
from typing import Any, Iterable


# Activity ID pulled out of any wrapper URN, e.g.
#   urn:li:fsd_update:(urn:li:activity:7476265993242148864,FEED_DETAIL,...)
#   urn:li:fsd_updateActions:(urn:li:activity:7476265993242148864,...)
#   urn:li:activity:7476265993242148864
# all collapse to "7476265993242148864".
_ACTIVITY_ID_RE = re.compile(r"urn:li:activity:(\d+)")


def _activity_id(urn: str | None) -> str | None:
    if not urn:
        return None
    m = _ACTIVITY_ID_RE.search(urn)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _entity_urn(rec: dict[str, Any]) -> str | None:
    return rec.get("entityUrn") or rec.get("*entityUrn") or rec.get("dashEntityUrn")


def _type(rec: dict[str, Any]) -> str:
    return str(rec.get("$type") or rec.get("_type") or "")


def build_index(included: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """URN -> record lookup."""
    idx: dict[str, dict[str, Any]] = {}
    for rec in included:
        urn = _entity_urn(rec)
        if urn:
            idx[urn] = rec
    return idx


def _follow(value: Any, idx: dict[str, dict[str, Any]]) -> Any:
    """If `value` is a URN string present in the index, return the referenced record."""
    if isinstance(value, str) and value in idx:
        return idx[value]
    return value


def _text_of(field: Any) -> str | None:
    """LinkedIn often wraps strings as {text: "...", attributes: [...]}."""
    if field is None:
        return None
    if isinstance(field, str):
        return field.strip() or None
    if isinstance(field, dict):
        # common shapes
        for k in ("text", "value", "string", "accessibilityText"):
            v = field.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        # nested {text: {text: "..."}}
        for k in ("text",):
            v = field.get(k)
            if isinstance(v, dict):
                inner = _text_of(v)
                if inner:
                    return inner
    return None


# ---------------------------------------------------------------------------
# Profile resolution -- the canonical Unknown-User fix
# ---------------------------------------------------------------------------
def build_profile_lookup(payloads: Iterable[dict[str, Any]]) -> dict[str, dict[str, str]]:
    """Walk every payload's `included[]` and produce: profile_urn -> {name, headline, url}.

    Matches both Profile records and the embedded "actor" sub-records on comments.
    """
    out: dict[str, dict[str, str]] = {}
    for payload in payloads:
        for rec in payload.get("included", []) or []:
            t = _type(rec).lower()
            if "profile" not in t and "miniprofile" not in t and "actor" not in t:
                # also handle bare records that have firstName/lastName
                if not (rec.get("firstName") or rec.get("lastName") or rec.get("name")):
                    continue
            urn = _entity_urn(rec)
            if not urn:
                continue
            name = _compose_name(rec)
            if not name:
                continue
            headline = _text_of(rec.get("headline")) or _text_of(rec.get("occupation")) or ""
            slug = rec.get("publicIdentifier") or rec.get("publicId")
            url = f"https://www.linkedin.com/in/{slug}/" if slug else ""
            out[urn] = {"name": name, "headline": headline, "url": url}
    return out


def _compose_name(rec: dict[str, Any]) -> str:
    first = _text_of(rec.get("firstName")) or ""
    last = _text_of(rec.get("lastName")) or ""
    if first or last:
        return f"{first} {last}".strip()
    # actor records typically carry the rendered name under `name` or `title`
    return _text_of(rec.get("name")) or _text_of(rec.get("title")) or ""


# ---------------------------------------------------------------------------
# Company overview
# ---------------------------------------------------------------------------
def extract_company(
    payloads: list[dict[str, Any]],
    slug: str | None = None,
) -> dict[str, Any]:
    """Pull headline fields from the captured Company / Organization record.

    When `slug` is provided (recommended), only records whose `universalName`
    matches the slug are considered -- this prevents sidebar / "similar pages"
    companies (e.g. Anthropic recommended under ElevenLabs) from polluting the
    result. If no slug-matching record is found, falls back to first match.
    """
    target = (slug or "").strip().lower()
    candidates: list[dict[str, Any]] = []
    fallback: list[dict[str, Any]] = []
    for payload in payloads:
        for rec in payload.get("included", []) or []:
            t = _type(rec).lower()
            if "company" not in t and "organization" not in t:
                continue
            rec_universal = (rec.get("universalName") or "").lower()
            if target and rec_universal == target:
                candidates.append(rec)
            elif not target:
                candidates.append(rec)
            else:
                fallback.append(rec)
    chosen = candidates if candidates else fallback

    result: dict[str, Any] = {}
    for rec in chosen:
        for src, dst in (
            ("name", "name"),
            ("universalName", "universal_name"),
            ("tagline", "tagline"),
            ("description", "description"),
            ("websiteUrl", "website"),
            ("industry", "industry"),
            ("staffCount", "employee_count"),
            ("followerCount", "followers"),
        ):
            if dst not in result:
                val = _text_of(rec.get(src)) if isinstance(rec.get(src), (str, dict)) else rec.get(src)
                if val:
                    result[dst] = val
        hq = rec.get("headquarter") or rec.get("headquarters")
        if hq and "headquarters" not in result:
            if isinstance(hq, dict):
                parts = [
                    _text_of(hq.get("city")),
                    _text_of(hq.get("geographicArea")),
                    _text_of(hq.get("country")),
                ]
                result["headquarters"] = ", ".join(p for p in parts if p) or None
            else:
                result["headquarters"] = _text_of(hq)
    return result


# ---------------------------------------------------------------------------
# Posts / updates
# ---------------------------------------------------------------------------
def extract_posts(payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """List of {urn, author_name, text, reactions_count, comments_count, ...}.

    Dedup is by the inner activity ID so that wrapper URNs around the same
    activity (e.g. `fsd_update:(activity:X,FEED_DETAIL,...)` and bare
    `activity:X`) collapse into a single post.
    """
    profile_lookup = build_profile_lookup(payloads)
    seen_activity_ids: set[str] = set()
    posts: list[dict[str, Any]] = []
    for payload in payloads:
        included = payload.get("included", []) or []
        idx = build_index(included)
        for rec in included:
            t = _type(rec).lower()
            if "update" not in t and "activity" not in t:
                continue
            urn = _entity_urn(rec) or ""
            act_id = _activity_id(urn)
            if not act_id or act_id in seen_activity_ids:
                continue

            actor_ref = rec.get("actor") or rec.get("*actor")
            actor = _follow(actor_ref, idx) if isinstance(actor_ref, str) else actor_ref
            author = ""
            if isinstance(actor, dict):
                name_field = actor.get("name") or actor.get("title")
                author = _text_of(name_field) or ""

            commentary = rec.get("commentary") or rec.get("text")
            text = _text_of(commentary) or ""

            social = rec.get("socialDetail") or rec.get("*socialDetail")
            social_rec = _follow(social, idx) if isinstance(social, str) else social
            reactions_count = 0
            comments_count = 0
            if isinstance(social_rec, dict):
                tc = social_rec.get("totalSocialActivityCounts") or social_rec.get("socialCounts") or {}
                if isinstance(tc, str):
                    tc = _follow(tc, idx) or {}
                if isinstance(tc, dict):
                    reactions_count = tc.get("numLikes") or tc.get("reactionCount") or 0
                    comments_count = tc.get("numComments") or tc.get("commentCount") or 0

            # Only commit the record once we actually have *something* useful,
            # so wrapper records with empty text don't shadow the real one.
            if not (text or author or reactions_count or comments_count):
                continue
            seen_activity_ids.add(act_id)
            posts.append({
                "urn": f"urn:li:activity:{act_id}",
                "author_name": author,
                "author_profile": profile_lookup.get(_entity_urn(actor) or "", {}).get("url", "") if isinstance(actor, dict) else "",
                "text": text,
                "reactions_count": reactions_count,
                "comments_count": comments_count,
            })
    return posts


# ---------------------------------------------------------------------------
# Comments  -- the high-value path
# ---------------------------------------------------------------------------
def extract_comments(payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """List of comment dicts with author resolved against the profile lookup."""
    profile_lookup = build_profile_lookup(payloads)
    seen: set[str] = set()
    comments: list[dict[str, Any]] = []
    for payload in payloads:
        included = payload.get("included", []) or []
        idx = build_index(included)
        for rec in included:
            t = _type(rec).lower()
            if "comment" not in t:
                continue
            urn = _entity_urn(rec) or ""
            if not urn or urn in seen or "urn:li:comment" not in urn:
                continue
            seen.add(urn)

            # author: may be a sub-record, a URN string, or a `commenter` field
            commenter = (
                rec.get("commenter")
                or rec.get("*commenter")
                or rec.get("commenterProfileId")
                or rec.get("actor")
            )
            commenter_rec = _follow(commenter, idx) if isinstance(commenter, str) else commenter
            author_urn = _entity_urn(commenter_rec) if isinstance(commenter_rec, dict) else (
                commenter if isinstance(commenter, str) else None
            )

            author_name = ""
            author_headline = ""
            author_url = ""
            if author_urn and author_urn in profile_lookup:
                p = profile_lookup[author_urn]
                author_name = p.get("name", "")
                author_headline = p.get("headline", "")
                author_url = p.get("url", "")
            elif isinstance(commenter_rec, dict):
                author_name = _compose_name(commenter_rec)
                author_headline = _text_of(commenter_rec.get("headline")) or _text_of(commenter_rec.get("occupation")) or ""

            body = (
                _text_of(rec.get("commentV2"))
                or _text_of(rec.get("comment"))
                or _text_of(rec.get("body"))
                or _text_of(rec.get("text"))
                or ""
            )

            post_urn = rec.get("threadId") or rec.get("parentUrn") or ""

            comments.append({
                "comment_urn": urn,
                "post_urn": post_urn,
                "author_urn": author_urn or "",
                "author_name": author_name or None,
                "author_headline": author_headline,
                "author_profile": author_url,
                "text": body,
                "name_resolution": "api" if author_name else "failed",
            })
    return comments
