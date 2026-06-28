# Architecture

How the scraper is organized and how data flows through it.

---

## 1. Module layout

```
run_scraper.py  (CLI)
        │
        ▼
scraper/main.py            ── orchestrator
        │
        ├─ scraper/browser.py        launch persistent Chromium + safe_goto + auth hook
        ├─ scraper/auth.py           is_auth_wall, auto_login, manual fallback
        ├─ scraper/interceptor.py    context.on("response") → bucket Voyager payloads
        ├─ scraper/api_extractor.py  parse buckets → records
        ├─ scraper/dom_extractor.py  per-card DOM extraction + 5-tier author resolve
        └─ scraper/config.py         all selectors, URL signatures, timeouts
```

---

## 2. Per-tab data flow

```
 ┌────────────┐                ┌────────────────┐
 │  Chromium  │── navigates ──▶│ LinkedIn SPA   │
 └─────┬──────┘                └────────┬───────┘
       │  context.on("response")         │ XHR / fetch
       ▼                                 ▼
 ┌──────────────────────┐         ┌───────────────────┐
 │ CapturedPayloads     │◀────────│ Voyager GraphQL   │
 │   updates / comments │         │  (company,        │
 │   reactions / jobs   │         │   updates, etc.)  │
 │   profile_lookups …  │         └───────────────────┘
 └─────────┬────────────┘
           │
           ▼
 api_extractor.py        ◀── PRIMARY: structured fields from JSON
           │
           ▼
 dom_extractor.py        ◀── PATCH: DOM fills gaps the API missed
           │
           ▼
 _merge() in main.py     ── single record per post, dedup'd by URN
```

### Two-phase, not simultaneous

The interceptor runs **continuously** in the background from the moment
the page is created. Browser interactions (scroll, click toggle, open
reactor modal) cause both more DOM to render *and* more Voyager calls
to fire. After interactions finish, a `MEDIUM_WAIT_S` settle lets
trailing responses arrive, then `_merge` runs once.

```
Time ──────────────────────────────────────────────────▶

[interceptor]  ████████████████████████████████████  always on
                           captures Voyager into buckets

[main loop]    goto──scroll──click──scroll──click──sleep
                  ↑          ↑          ↑
                  cause Voyager calls + DOM updates

                                            ┌─ _merge ─┐
                                            │ per field│
                                            │  api?    │
                                            │  else dom│
                                            └──────────┘
                                                  │
                                                  ▼
                                            scraped_*.json
```

### Field-by-field merge

`_merge` is a **per-field** decision, not per-source. Pattern in
[scraper/main.py](../scraper/main.py):

```python
# 1. Build records from API first
posts_by_urn = {p["urn"]: p for p in api_extractor.extract_posts(updates)}

# 2. Walk DOM cards; fill in what API didn't have, add posts API missed
for post_card in page.locator(config.POST_CARD).all():
    urn = post_card.get_attribute("data-urn")
    posts_by_urn.setdefault(urn, {...empty skeleton...})
    existing = posts_by_urn[urn]
    if not existing.get("text"):
        existing["text"] = dom_extractor.extract_post_text_fallback(post_card)
    if not existing.get("reactions_count"):
        existing["reactions_count"] = dom_extractor.extract_reactions_from_card(post_card)
    ...
```

So each field follows: **use API value if non-empty, else use DOM
value, else leave blank.** This is why a run with `updates: 0` (no
Voyager feed payloads captured) still produces complete records — the
DOM loop fills every gap.

| Field | Primary | Fallback |
|---|---|---|
| `company.{name, tagline, …}` | API `company_overview` | DOM `extract_company_header` via `setdefault` |
| `urn` | DOM (`data-urn`) | — (always present) |
| `text` | API `updates` | DOM `extract_post_text_fallback` → subtraction |
| `author_name` / `author_profile` | API `updates` (actor lockup: `actor.name.text`, `actor.navigationContext.actionTarget`) | DOM 5-tier author resolve |
| `reactions_count` / `reactions_label` | API `updates` (+ `reactor_totals` from `reactions` bucket) | DOM `extract_reactions_from_card` |
| `comments_count` | API `updates` | DOM `extract_comments_count_from_card` |
| `comments[]` | API `comments` bucket (commenter lockup, see § 6) | DOM comment-card scrape, dedup'd by URN/text |
| `reactors[]` | API `reactions` bucket (`extract_reactors_by_post`, see § 5) | DOM modal scrape (`--reactors`), only for posts API missed |
| `products[]` | API `products` bucket (`extract_products`) | — |

---

## 3. Modules

### `scraper/config.py`
Single source of truth. Every selector, URL signature, timeout, and
bucket name lives here. When LinkedIn ships a DOM change, fix it here once.

Key sections:
- `TAB_PATHS`, `DEFAULT_TABS` — which tabs to visit
- `COMPANY_URL_SIGNATURES`, `API_ROUTES` — fragments used to bucket Voyager responses
- `POST_CARD`, `COMMENT_CARD`, `REACTOR_ITEM` — DOM hooks
- `POST_REACTIONS` (read) vs `POST_REACTIONS_BUTTON` (click) — split so
  we don't try to click a non-clickable counter span
- `AUTH_URL_FRAGMENTS`, `AUTH_DOM_HOOKS`, `MFA_TEXT_PATTERNS` —
  auth-wall detection
- `SCRAPED_DATA_DIR` (`scraped_data/`) — default output folder
- `EXHAUSTIVE_POSTS` / `EXHAUSTIVE_COMMENT_PAGES` /
  `EXHAUSTIVE_REACTOR_SCROLLS` — safety caps that bound the
  `--all-posts` / `--all-comments` / `--all-reactors` plateau loops

### `scraper/browser.py`
`launch()` context manager. Opens Chromium with:
- Persistent profile at `user-data-dir-chrome/` (reuses the login cookie)
- `--disable-blink-features=AutomationControlled`
- Init script that hides `navigator.webdriver`, sets `languages`, `plugins`

`safe_goto()` wraps `page.goto` and calls `handle_auth_wall` after
every navigation so a stale cookie auto-recovers mid-run.

### `scraper/interceptor.py`
Attached to the **BrowserContext** (not the Page) **before** the first
navigation, so popups and service-worker fetches are caught too.
Every capture is stored as a `(url, payload)` tuple; `bucket(name)`
returns just the payloads for backward-compat, and `bucket_with_urls(name)`
exposes the URL when an extractor needs queryId / threadUrn etc.

Responses are classified by URL substring into these buckets:

| Bucket | Triggered by |
|---|---|
| `company_overview` | Company root / about queries |
| `updates` | Activity feed -- `voyagerFeedDashUpdates*` and `OrganizationalPageUpdates` |
| `comments` | `voyagerSocialDashComments` |
| `reactions` | `voyagerSocialDashReactions` |
| `profile_lookups` | Identity-dash member queries (URN → name mapping) |
| `products` | `OrganizationDashViewWrapper` umbrella query (products + photos) |
| `jobs` | `voyagerJobsDashJobCards` |
| `graphql_other` | Everything else (kept for inspection; some extractors also scan it) |

`CapturedPayloads.reset()` clears the buckets between tabs so we don't
cross-contaminate.

### `scraper/api_extractor.py`
Pure transformers: each function takes a bucket of payloads and returns
records. **No Playwright dependency** — callable on JSON files saved in
`api_dumps/` for offline experimentation.

| Function | Reads | Returns |
|---|---|---|
| `extract_company` | `company_overview` + `graphql_other` + `comments` + `updates` | one company dict (name, tagline, industry, hq, founded, logo, CTA url…) |
| `extract_posts` | `updates` bucket | list of post dicts (urn, author, text, counts) |
| `extract_comments` | `comments` bucket | list of comment dicts (commenter, body, post_urn) |
| `extract_reactors_by_post` | `reactions` bucket | `{activity_urn: [reactor, …]}` |
| `reactor_totals` | `reactions` bucket | `{activity_urn: paging.total}` |
| `extract_products` | `products` + `graphql_other` | list of product dicts |
| `build_profile_lookup` | `profile_lookups` bucket | `{profile_urn: {name, headline, url}}` |

**Modern lockup pattern.** Reactor, comment, and post records all share
the same shape: a *lockup* dict with `title.text` (name),
`subtitle.text` (headline), `navigationUrl` (profile URL), `image`
(avatar). Author URNs are read directly from the lockup
(`commenterProfileId`, `actor.backendUrn`, `actorUrn`) rather than
following graph references. This is resilient to the periodic Voyager
field renames we have already absorbed (e.g. `urn:li:fsd_comment`
replacing `urn:li:comment`, `commentary` replacing `commentV2`,
`actor.navigationContext.actionTarget` replacing the
`profile_lookups`-based URL resolve). `_post_ref_id` further normalizes
`urn:li:ugcPost:N` → `urn:li:activity:N` so comments on UGC-shaped
posts (videos, polls, articles) still attach to the right post.

### `scraper/dom_extractor.py`
Per-card DOM extractors and the **5-tier author resolution**:

1. URN → look up in `profile_lookups` bucket
2. `aria-label="View Jane Doe's profile"` — strips suffixes
   (`'s profile`, `'s graphic link`, `'s page`)
3. `a[href*='/in/{slug}']` text content
4. `span[aria-hidden='true']` inside the actor block
5. Last-ditch: URN string itself (never "Unknown User")

Also handles: reactions count (read-path), comments count from the
counter button, reactor row extraction from the modal.

### `scraper/auth.py`
Three-layer recovery — see § 4.

### `scraper/main.py`
Orchestrator. For each tab:

1. `_pinned_goto` — navigate and verify URL contains expected fragment
   (retries up to 3× if LinkedIn drifts to a post-detail page)
2. `payloads.reset()` — this tab starts clean
3. `_scroll_feed` — alternating `scrollBy` + `scrollTo(scrollHeight)`
   to trigger both lazy-loader callbacks and IntersectionObservers.
   With `--all-posts`, loops until plateau or `EXHAUSTIVE_POSTS` cap
4. `_expand_post_comments` — click toggle, then `Load more comments`,
   `Show more replies`, comment-body `see more`. With `--all-comments`,
   loops until plateau or `EXHAUSTIVE_COMMENT_PAGES` cap
5. `_collect_reactors_for_post` (optional, `--reactors` /
   `--all-reactors`) — click the reactions button, scroll the modal
   until plateau, harvest rows; capped at `--max-reactor-scrolls`
   (default 25) or `EXHAUSTIVE_REACTOR_SCROLLS` under `--all-reactors`
6. `_merge()` — field-by-field union:
   - posts: API base + DOM patch (text / counts / author)
   - comments: API base + DOM patch, dedup by `(post_urn, comment_urn)`
   - reactors: API primary (`reactors_source: "api"`); DOM modal kept
     only for posts the API didn't cover (`reactors_source: "dom_modal"`)
   - products: API only
7. Write `scraped_data/scraped_<slug>_<tab>.json` (one file per tab)
   plus a stats line: `posts=N comments=N reactors_api=N products=N`

---

## 4. Auth flow

```
                    safe_goto(url)
                          │
                          ▼
                  is_auth_wall(page)?
                ┌─────────┴─────────┐
               no                  yes
                │                   │
                │           _looks_like_mfa(page)?
                │            ┌──────┴──────┐
                │           no            yes
                │            │             │
                │       auto_login(page)   │
                │            │             │
                │  settle MEDIUM_WAIT_S    │
                │            │             │
                │  still walled?           │
                │     ┌──────┴──────┐      │
                │    no            yes─────┤
                │     │                    │
                │     │              _manual_pause(reason)
                │     │                    │
                │  return            still walled?
                │                    ┌─────┴─────┐
                │                   no          yes
                │                    │           │
                │                    │     raise RuntimeError
                │                    │
                └─────── return ─────┘
```

`_looks_like_mfa` is **URL-guarded**: on `/login` it returns False even
if the page body mentions "verify" (footer copy is a common
false-positive). MFA text is only treated as a real signal after we've
moved past the login page.

`auto_login` uses permissive selector lists so it handles both the full
email+password form and LinkedIn's "Welcome back, `{name}`" password-only
screen. Email is best-effort; password is mandatory. If the submit
button can't be found it falls back to pressing Enter on the password
field.

---

## 5. Reactor harvesting

Reactor lists come from **two sources**, with the API as the primary
and the DOM modal as a fallback.

### Primary: `reactions` bucket (API-derived)

Every time the reactors modal is opened, LinkedIn fires
`voyagerSocialDashReactions` requests that `interceptor.py` captures into
the `reactions` bucket. `api_extractor.extract_reactors_by_post` walks
each payload's `included[]`, filters `$type` containing
`voyager.dash.social.Reaction`, and pulls per-reactor fields from
`reactorLockup`:

| Output field | Source path on the Reaction record |
|---|---|
| `name`         | `reactorLockup.title.text` |
| `profile_url`  | `reactorLockup.navigationUrl` |
| `headline`     | `reactorLockup.subtitle.text` |
| `degree`       | `reactorLockup.label.text` (`"1st"`, `"3rd+"`) |
| `reaction_type`| `reactionType` (`LIKE` / `PRAISE` / `EMPATHY` / …) |
| `actor_urn`    | `actorUrn` (or `actor.*profileUrn` / `actor.*companyUrn`) |

The parent activity URN is extracted by regex from the Reaction's own
`entityUrn` (it's embedded as `urn:li:activity:{id}`). Each payload also
carries `paging.total` — surfaced on the post as `reactions_total`, so
the output records the true reactor count even when we only captured a
slice. Dedup is by `(activity_urn, actor_urn)` so paginated payloads
collapse cleanly.

Posts populated from the API are tagged `reactors_source: "api"`.

### Fallback: DOM modal scrape

`_collect_reactors_for_post` runs only when `--reactors` is set, and the
post-merge step in `_scrape_one_tab` keeps its output **only** for posts
the API didn't cover (tagged `reactors_source: "dom_modal"`). Flow:

1. Click the post's reactions button (`POST_REACTIONS_BUTTON =
   button[data-reaction-details]`).
2. Wait for `REACTORS_MODAL` to be visible.
3. Loop until plateau (2 consecutive scrolls with no new items) **or**
   `max_scrolls` reached:
   - Set the modal's `scrollTop = scrollHeight`
   - Sleep `SHORT_WAIT_S`
4. Run `extract_reactors_from_modal` to harvest rows.
5. Close the modal (dismiss button → Escape fallback).

Default cap is 10 scrolls. Each scroll fires another Voyager page (10
reactors), so even when the DOM scrape is the visible fallback, the API
extractor will usually have already pulled those rows from the bucket.
Override with `--max-reactor-scrolls`.

---

## 6. Comment harvesting

Comments now follow the same API-primary pattern as reactors. The DOM
comment-card scrape exists only as a defense-in-depth fallback.

### Primary: `comments` bucket (API-derived)

`_expand_post_comments` causes LinkedIn to fire
`voyagerSocialDashComments` requests, which `interceptor.py` captures
into the `comments` bucket. `api_extractor.extract_comments` walks each
payload's `included[]`, filters records whose `$type` is
`com.linkedin.voyager.dash.social.Comment`, and reads each field from
the modern Voyager schema:

| Output field | Source path on the Comment record |
|---|---|
| `comment_urn`   | `entityUrn` (`urn:li:fsd_comment:({id},urn:li:activity:{N})`) |
| `post_urn`      | `threadUrn` (normalized to `urn:li:activity:{N}` via `_post_ref_id`) |
| `text`          | `commentary.text` (TextViewModel) |
| `author_name`   | `commenter.title.text` |
| `author_headline` | `commenter.subtitle.text` |
| `author_profile`  | `commenter.navigationUrl` |
| `author_urn`    | `commenter.commenterProfileId` (flattened from `*companyUrn` / `profileUrn` graph-refs); fallback: `commenter.image.attributes[*].detailData.*profile` / `*company` |

Dedup is by `comment_urn`. UGC-shaped posts (videos, polls, articles)
emit `urn:li:ugcPost:N` in `threadUrn` instead of
`urn:li:activity:N`; `_post_ref_id` extracts the numeric ID and
rewrites it to the activity form so the comment attaches to the right
post in the merge step.

Comments populated from the API are tagged `name_resolution: "api"`.

### Fallback: DOM comment-card scrape

When the API misses a comment (rare since the schema rewrite), the DOM
scrape inside [scraper/dom_extractor.py](../scraper/dom_extractor.py)
harvests visible comment cards using the 5-tier author resolve. Each
DOM-derived comment is tagged `name_resolution: "dom_tier_<X>_*"` so
telemetry can spot when the API path regresses (a sudden spike of
`dom_tier_B_aria` resolutions caught the recent `urn:li:fsd_comment`
field rename).

---

## 7. Extension points

| Want to add… | Touch |
|---|---|
| A new tab (e.g. `/people/`) | `config.TAB_PATHS`, `config.DEFAULT_TABS`, optionally a new bucket in `interceptor.py` + a parser in `api_extractor.py` |
| Multi-company batch | Small driver above `scraper/main.py:scrape_company` |
| Crawler that discovers slugs | New module (not part of this scraper) feeding URLs into the batch driver |
| Headless production runs | Switch engine to Camoufox in `browser.py`, or wrap headed Chromium in Xvfb (Linux). See [CONSIDERATIONS.md](CONSIDERATIONS.md) |
| Parallel scraping | Multiple persistent profile dirs, one process per profile |
| Database output instead of JSON | Replace the `json.dump` calls in `scrape_company` with your sink |
