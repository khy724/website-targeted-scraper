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

# Posts also appear as `ugcPost` or `share` URNs (videos, polls, reposts).
# Comments on those carry `urn:li:ugcPost:N` in their entityUrn / threadUrn
# instead of `urn:li:activity:N`. LinkedIn uses the same numeric ID across
# both forms for organic company-page posts, so we extract the ID and let
# the caller normalize to whichever shape its post list uses.
_POST_REF_RE = re.compile(r"urn:li:(?:activity|ugcPost|share|fsd_ugcPost):(\d+)")


def _activity_id(urn: str | None) -> str | None:
    if not urn:
        return None
    m = _ACTIVITY_ID_RE.search(urn)
    return m.group(1) if m else None


def _post_ref_id(urn: str | None) -> str | None:
    """Extract a post's numeric ID from any of its URN shapes (activity / ugcPost / share)."""
    if not urn:
        return None
    m = _POST_REF_RE.search(urn)
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
                joined = ", ".join(p for p in parts if p)
                if joined:
                    result["headquarters"] = joined
            else:
                txt = _text_of(hq)
                if txt:
                    result["headquarters"] = txt

        # callToAction.url is often the canonical website when websiteUrl is missing
        if "website" not in result:
            cta = rec.get("callToAction")
            if isinstance(cta, dict) and cta.get("type") in ("VIEW_WEBSITE", None):
                u = cta.get("url")
                if isinstance(u, str) and u.startswith("http"):
                    result["website"] = u

        # Founded year (foundedOn.year is the rich form; createdAt is page creation, weaker)
        if "founded_year" not in result:
            fo = rec.get("foundedOn")
            if isinstance(fo, dict):
                y = fo.get("year")
                if isinstance(y, int) and 1800 < y < 2100:
                    result["founded_year"] = y

        # Logo URL (built from rootUrl + first artifact's fileIdentifyingUrlPathSegment)
        if "logo_url" not in result:
            logo = rec.get("logo") or rec.get("logoResolutionResult")
            url = _vector_image_url(logo)
            if url:
                result["logo_url"] = url
    return result


def _vector_image_url(field: Any, prefer_size: int = 200) -> str | None:
    """Build a usable image URL from LinkedIn's vectorImage shape:
        {"vectorImage": {"rootUrl": "...", "artifacts": [{"fileIdentifyingUrlPathSegment": "..."}]}}
    Returns the artifact closest to `prefer_size` width.
    """
    if not isinstance(field, dict):
        return None
    vi = field.get("vectorImage") or field
    if not isinstance(vi, dict):
        return None
    root = vi.get("rootUrl") or ""
    arts = vi.get("artifacts") or []
    if not (root and isinstance(arts, list) and arts):
        return None
    best = min(
        (a for a in arts if isinstance(a, dict) and a.get("fileIdentifyingUrlPathSegment")),
        key=lambda a: abs(int(a.get("width") or 0) - prefer_size),
        default=None,
    )
    if not best:
        return None
    return f"{root}{best['fileIdentifyingUrlPathSegment']}"


# ---------------------------------------------------------------------------
# Products -- parsed from the OrganizationDashViewWrapper umbrella query
# ---------------------------------------------------------------------------
def extract_products(payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """List of {urn, name, tagline, description, logo_url, page_url, category}.

    Walks `included[]` for `OrganizationProduct` records. Dedup is by entityUrn.
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for payload in payloads:
        for rec in payload.get("included", []) or []:
            t = _type(rec)
            if "OrganizationProduct" not in t:
                continue
            urn = _entity_urn(rec) or ""
            if not urn or urn in seen:
                continue

            name = (
                _text_of(rec.get("localizedName"))
                or _text_of(rec.get("name"))
                or ""
            )
            if not name:
                # entirely empty stub
                continue
            seen.add(urn)

            product: dict[str, Any] = {"urn": urn, "name": name}

            tag = _text_of(rec.get("tagline"))
            if tag:
                product["tagline"] = tag

            desc = _text_of(rec.get("description"))
            if desc:
                product["description"] = desc

            cat = _text_of(rec.get("category")) or _text_of(rec.get("primaryCategory"))
            if cat:
                product["category"] = cat

            logo_url = (
                _vector_image_url(rec.get("logo"))
                or _vector_image_url(rec.get("logoResolutionResult"))
            )
            if logo_url:
                product["logo_url"] = logo_url

            page_url = rec.get("url") or rec.get("productPageUrl")
            if isinstance(page_url, str):
                product["page_url"] = page_url

            out.append(product)
    return out


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
            author_profile_url = ""
            author_urn = ""
            if isinstance(actor, dict):
                name_field = actor.get("name") or actor.get("title")
                author = _text_of(name_field) or ""
                # Modern actor lockup: profile URL lives on navigationContext.
                nav_ctx = actor.get("navigationContext") or {}
                if isinstance(nav_ctx, dict):
                    author_profile_url = nav_ctx.get("actionTarget") or ""
                if not author_profile_url:
                    img = actor.get("image") or {}
                    if isinstance(img, dict):
                        author_profile_url = img.get("actionTarget") or ""
                # Author URN: backendUrn on the lockup, with profile_lookup as a fallback.
                author_urn = actor.get("backendUrn") or _entity_urn(actor) or ""
                if not author_profile_url and author_urn in profile_lookup:
                    author_profile_url = profile_lookup[author_urn].get("url", "")

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
                "author_profile": author_profile_url,
                "text": text,
                "reactions_count": reactions_count,
                "comments_count": comments_count,
            })
    return posts


# ---------------------------------------------------------------------------
# Comments  -- the high-value path
# ---------------------------------------------------------------------------
# Modern Comment records ($type=com.linkedin.voyager.dash.social.Comment) look like:
#   entityUrn: "urn:li:fsd_comment:(<commentId>,urn:li:activity:<actId>)"
#   threadUrn: "urn:li:activity:<actId>"               (parent post)
#   commentary: { text: "...", attributesV2: [...] }   (TextViewModel)
#   commenter:                                          (lockup, same shape as reactorLockup)
#       title.text       = "Jane Doe"
#       subtitle.text    = "Engineer at Foo"
#       navigationUrl    = "https://www.linkedin.com/in/<public-id>"
#       commenterProfileId = "urn:li:fsd_profile:..." or company id
#       image.attributes[*].detailData.nonEntityProfilePicture.*profile = backup profile URN
# We previously expected legacy field names (`commentV2` / `urn:li:comment`)
# which 0% of modern responses produce -- silently dropping every API comment.
def extract_comments(payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """List of comment dicts with author + body resolved from the API."""
    profile_lookup = build_profile_lookup(payloads)
    seen: set[str] = set()
    comments: list[dict[str, Any]] = []
    for payload in payloads:
        included = payload.get("included", []) or []
        idx = build_index(included)
        for rec in included:
            t = _type(rec).lower()
            # Match social.Comment but not e.g. SocialPermissions / HideCommentAction.
            if not t.endswith(".comment") and "social.comment" not in t:
                continue
            urn = _entity_urn(rec) or ""
            if not urn or urn in seen:
                continue
            # Accept both modern (urn:li:fsd_comment:...) and legacy (urn:li:comment:...)
            if "comment:" not in urn:
                continue
            seen.add(urn)

            # Parent activity URN -- prefer the explicit threadUrn, fall back to
            # the activity ID embedded in the comment's own entityUrn. For
            # ugcPost-shaped posts we synthesize the activity form using the
            # shared numeric ID (matches what extract_posts emits).
            post_urn = (
                rec.get("threadUrn")
                or rec.get("threadId")
                or rec.get("parentUrn")
                or ""
            )
            pid = _post_ref_id(post_urn) or _post_ref_id(urn)
            if pid:
                post_urn = f"urn:li:activity:{pid}"
            elif not post_urn:
                post_urn = ""

            # Body -- modern records wrap text under `commentary` (TextViewModel).
            body = (
                _text_of(rec.get("commentary"))
                or _text_of(rec.get("content"))
                or _text_of(rec.get("commentV2"))
                or _text_of(rec.get("comment"))
                or _text_of(rec.get("body"))
                or _text_of(rec.get("text"))
                or ""
            )

            # Commenter -- modern shape is a rich lockup dict (same as reactorLockup).
            commenter = rec.get("commenter")
            author_name = ""
            author_headline = ""
            author_url = ""
            author_urn = ""

            if isinstance(commenter, dict):
                author_name = _text_of(commenter.get("title")) or ""
                author_headline = _text_of(commenter.get("subtitle")) or ""
                nav = commenter.get("navigationUrl")
                if isinstance(nav, str):
                    author_url = nav

                # Profile URN: try direct fields first, then dig through the
                # image lockup's nonEntityProfilePicture / companyLogo refs.
                # Some fields are raw graph-ref dicts like
                #   {"*companyUrn": "...", "profileUrn": null}
                # so we flatten any dict to its first non-null URN-shaped value.
                def _flatten_urn_ref(v):
                    if isinstance(v, str):
                        return v
                    if isinstance(v, dict):
                        for k in (
                            "*profile", "profileUrn",
                            "*company", "*companyUrn", "companyUrn",
                            "*actor", "actor",
                            "*author", "author",
                        ):
                            sub = v.get(k)
                            if isinstance(sub, str) and sub:
                                return sub
                    return ""

                for candidate in (
                    commenter.get("commenterProfileId"),
                    commenter.get("*author"),
                    commenter.get("author"),
                    commenter.get("*actor"),
                    commenter.get("actor"),
                ):
                    author_urn = _flatten_urn_ref(candidate)
                    if author_urn:
                        break

                if not author_urn:
                    img = commenter.get("image") or {}
                    for attr in (img.get("attributes") or []):
                        dd = (attr or {}).get("detailData") or {}
                        for key in (
                            "nonEntityProfilePicture",
                            "profilePicture",
                            "profilePictureWithoutFrame",
                            "nonEntityCompanyLogo",
                            "companyLogo",
                        ):
                            ref = dd.get(key) or {}
                            if isinstance(ref, dict):
                                pu = ref.get("*profile") or ref.get("*company")
                                if pu:
                                    author_urn = pu
                                    break
                        if author_urn:
                            break
            elif isinstance(commenter, str):
                author_urn = commenter

            # Fall back to legacy resolution via profile_lookup if direct mine failed.
            if not author_name and author_urn and author_urn in profile_lookup:
                p = profile_lookup[author_urn]
                author_name = p.get("name", "")
                author_headline = author_headline or p.get("headline", "")
                author_url = author_url or p.get("url", "")
            elif not author_name and isinstance(commenter, dict):
                # Last resort: composed name from any first/last fields present.
                composed = _compose_name(commenter)
                if composed:
                    author_name = composed

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


# ---------------------------------------------------------------------------
# Reactors -- parsed from the SocialDashReactions endpoint
# ---------------------------------------------------------------------------
# Each Reaction record in `included[]` looks like:
#   {
#     "$type": "com.linkedin.voyager.dash.social.Reaction",
#     "entityUrn": "urn:li:fsd_reaction:(urn:li:fsd_profile:...,urn:li:activity:NNN,0)",
#     "actorUrn": "urn:li:fsd_profile:...",
#     "actor": {"*profileUrn": "..."} or {"*companyUrn": "..."},
#     "reactionType": "LIKE" | "PRAISE" | "APPRECIATION" | "EMPATHY" | ...,
#     "reactorLockup": {
#         "title":        {"text": "Jane Doe"},
#         "subtitle":     {"text": "Engineer at Foo"},
#         "navigationUrl": "https://www.linkedin.com/in/<public-id>",
#         "label":        {"text": "1st" | "3rd+" | ...}
#     }
#   }
# The parent activity URN is embedded in `entityUrn` -- we extract it so each
# reactor knows which post it belongs to.
_REACTOR_TYPE_FRAGMENTS = ("voyager.dash.social.reaction",)


def extract_reactors_by_post(
    payloads: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Return {activity_urn: [reactor_dict, ...]} parsed from `reactions` bucket payloads.

    Reactor dict shape (matches dom_extractor.extract_reactors_from_modal plus extras):
        name, profile_url, headline, reaction_type, degree, actor_urn

    Dedup is by (activity_urn, actor_urn) so multiple paginated payloads collapse.
    """
    by_post: dict[str, list[dict[str, Any]]] = {}
    seen: set[tuple[str, str]] = set()

    for payload in payloads:
        for rec in payload.get("included", []) or []:
            t = _type(rec).lower()
            if not any(frag in t for frag in _REACTOR_TYPE_FRAGMENTS):
                continue

            # Parent activity URN comes from the reaction's own entityUrn.
            ent = _entity_urn(rec) or ""
            act_id = _activity_id(ent)
            if not act_id:
                continue
            activity_urn = f"urn:li:activity:{act_id}"

            actor_urn = rec.get("actorUrn") or ""
            # Some payloads only have actor.*profileUrn / actor.*companyUrn.
            if not actor_urn:
                actor = rec.get("actor") or {}
                if isinstance(actor, dict):
                    actor_urn = (
                        actor.get("*profileUrn")
                        or actor.get("*companyUrn")
                        or actor.get("profileUrn")
                        or actor.get("companyUrn")
                        or ""
                    )

            key = (activity_urn, actor_urn)
            if key in seen:
                continue

            lockup = rec.get("reactorLockup") or {}
            name = _text_of(lockup.get("title")) or ""
            headline = _text_of(lockup.get("subtitle")) or ""
            profile_url = lockup.get("navigationUrl") or ""
            degree = _text_of(lockup.get("label")) or ""

            if not (name or profile_url):
                # An empty lockup means LinkedIn redacted this row (private, etc.)
                continue
            seen.add(key)

            reactor: dict[str, Any] = {
                "name": name,
                "profile_url": profile_url,
                "headline": headline,
            }
            rtype = rec.get("reactionType")
            if rtype:
                reactor["reaction_type"] = rtype
            if degree:
                reactor["degree"] = degree
            if actor_urn:
                reactor["actor_urn"] = actor_urn

            by_post.setdefault(activity_urn, []).append(reactor)

    return by_post


def reactor_totals(payloads: list[dict[str, Any]]) -> dict[str, int]:
    """Return {activity_urn: total_reactions} from the response's `paging.total` field.

    Each SocialDashReactions response carries
        data.data.socialDashReactionsByReactionType.paging.total
    and the thread URN is in the request URL (variables=...threadUrn:...).
    For now we use the parent URN embedded in any reactor's entityUrn from
    the same payload's `included[]`.
    """
    out: dict[str, int] = {}
    for payload in payloads:
        # Walk the wrapper to find any paging.total
        try:
            data = payload.get("data") or {}
            inner = data.get("data") if isinstance(data, dict) else None
            holder = (
                (inner or {}).get("socialDashReactionsByReactionType")
                if isinstance(inner, dict)
                else None
            )
            total = (holder or {}).get("paging", {}).get("total") if holder else None
        except Exception:
            total = None
        if not total:
            continue

        # Find the activity URN by inspecting any Reaction record in `included`.
        activity_urn = None
        for rec in payload.get("included", []) or []:
            t = _type(rec).lower()
            if not any(frag in t for frag in _REACTOR_TYPE_FRAGMENTS):
                continue
            ent = _entity_urn(rec) or ""
            aid = _activity_id(ent)
            if aid:
                activity_urn = f"urn:li:activity:{aid}"
                break
        if activity_urn:
            # Keep the max if multiple payloads target the same post.
            out[activity_urn] = max(out.get(activity_urn, 0), int(total))
    return out
