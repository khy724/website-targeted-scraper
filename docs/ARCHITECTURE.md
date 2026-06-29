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

#### Tab navigation: direct URL vs. nav-bar click

Every tab transition (`home`, `about`, `posts`, `jobs`, `products`) is a
**direct `page.goto(url)`** call — we never click the in-page nav-tab
bar. We considered the alternative; here's the tradeoff:

| | Direct `page.goto(url)` (current) | In-page nav-bar click |
|---|---|---|
| Bot-likeness | Slightly higher — full page reload from cold, no `Referer` chain | Lower — looks like an authenticated SPA route change, sends `Referer`, reuses cached JS bundles |
| Speed per tab | Slower — full bundle + image reload | Faster — small SPA XHR |
| Reliability | Higher — bypasses any DOM-tab discoverability issues; URLs are documented in `TAB_PATHS` and stable for years | Lower — the nav-tab DOM uses LinkedIn's obfuscated class soup; selectors rot every few releases |
| Auth-wall coverage | `safe_goto` re-runs `handle_auth_wall` after each transition | One auth check at the start; mid-flow cookie expiry would slip through |
| Failure mode | "Navigation interrupted" if a prior nav is still in flight — handled by the settle + single retry in `safe_goto` | Click silently no-ops if selector breaks; we'd extract the *previous* tab's content under the new tab's label |

**We stay direct.** If LinkedIn ever escalates fingerprinting against
full-reload patterns, the cleanest hybrid is: `page.goto(home_url)`
once per company, then click subsequent tabs via stable ARIA roles
(`page.get_by_role("link", name="About")`, etc.). Don't bind to CSS
classes — they rotate.

#### Demo recording (`--record`)

When `--record` is set, `launch()` passes `record_video_dir`,
`record_video_size=1440x900`, and `slow_mo=120` to
`launch_persistent_context`. Playwright writes one `.webm` per `Page`
object into `demo_videos/<UTC-timestamp>/`. Because we drive everything
through a single `Page`, every navigation in the run — including the
pre-flight `/login` flow when the persistent profile has no cookie —
lands in the same file.

Key properties:

- **Recording is virtual frame capture from the renderer**, not OS-level
  screen capture. DRM / secure-surface flags don't apply, and Chromium
  has no "do not record" hint for login pages.
- **Videos finalize only on clean `context.close()`.** A Ctrl-C mid-run
  loses the file. Let the run complete (or hit Enter at any MFA pause).
- **Smooth scrolling is required for visibility.** Instant
  `scrollTop = scrollHeight` jumps complete in one frame and are
  invisible at 30 fps. Both `_scroll_until_plateau` and the reactor
  modal loop use `behavior: 'smooth'` for this reason.
- **`slow_mo=120` ms** adds a small delay to every Playwright action so
  clicks and field-fills are legible without making the run unbearable.

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
Orchestrator. Top-level `scrape_company` runs a **pre-flight auth check**
before any navigation:

```python
if not auth.profile_seems_authenticated():
    # open /login in the same recorded context, drive credentials,
    # pause for MFA if needed, then carry on
```

`profile_seems_authenticated` is a SQLite read of
`user-data-dir-chrome/Default/Cookies` looking for a non-expired `li_at`
row on `.linkedin.com`. The cookie value itself is encrypted by the OS
keychain and we don't decrypt it — a present, unexpired row is enough
signal. If LinkedIn rotated the session server-side, the regular
auth-wall recovery on the first navigation still catches it.

Then, for each tab:

1. `_pinned_goto` — navigate and verify URL contains expected fragment
   (retries up to 3× if LinkedIn drifts to a post-detail page)
2. URL-drift skip — if LinkedIn redirects us off the requested tab
   (e.g. company has no `/products/` → JS jumps to `/jobs/`), record
   `skipped: True` with the destination URL instead of mislabeling
   the destination tab's content as the source tab
3. `payloads.reset()` — this tab starts clean
4. **Tab-shape branch:**
   - `home` / `posts`: `_scroll_feed` — alternating `scrollBy` +
     `scrollTo(scrollHeight)` to trigger both lazy-loader callbacks and
     IntersectionObservers, plateau on `POST_CARD` count. With
     `--all-posts`, safety cap is `EXHAUSTIVE_POSTS`.
   - everything else (`about` / `jobs` / `products`): `_scroll_until_plateau`
     — generic plateau scroll keyed off `document.body.scrollHeight`
     growth, so it works even when there's no `POST_CARD` selector to
     count. Smooth scrolling so it stays visible on demo recordings.
5. **URN-stable per-post loop** (home/posts only) — snapshot `data-urn`
   values for the first N cards *upfront* in a single pass, then
   re-locate each card via `page.locator(f"[data-urn='{urn}']").first`
   before each operation. This is the only reliable iteration pattern
   because:
   - `_expand_post_comments` injects hundreds of comment nodes that
     shift sibling indices, breaking any cached `.nth(i)` locator
   - LinkedIn's feed virtualization detaches off-screen cards entirely
   Both effects make positional locators time out on `.get_attribute()`.
   Re-locating by URN survives virtualization (LinkedIn keeps the same
   `data-urn` even when re-rendering the card). Per-post try/except
   isolates failures so one bad card doesn't kill the whole tab.
6. `_expand_post_comments` — click toggle, then `Load more comments`,
   `Show more replies`, comment-body `see more`. With `--all-comments`,
   loops until plateau or `EXHAUSTIVE_COMMENT_PAGES` cap
7. `_collect_reactors_for_post` (optional, `--reactors` /
   `--all-reactors`) — see § 5. Capped at `--max-reactor-scrolls`
   (default 25) or `EXHAUSTIVE_REACTOR_SCROLLS` under `--all-reactors`
8. `_merge()` — field-by-field union:
   - posts: API base + DOM patch (text / counts / author)
   - comments: API base + DOM patch, dedup by `(post_urn, comment_urn)`
   - reactors: API primary (`reactors_source: "api"`); DOM modal kept
     only for posts the API didn't cover (`reactors_source: "dom_modal"`)
   - products: API only
9. Write `scraped_data/scraped_<slug>_<tab>.json` (one file per tab)
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

### Pre-flight gate: `profile_seems_authenticated`

Before any navigation, `scrape_company` runs a cheap SQLite check on
`user-data-dir-chrome/Default/Cookies` for a non-expired `li_at` row
scoped to `.linkedin.com`. Two outcomes:

- **Cookie present + unexpired** → skip the explicit login goto. The
  first `safe_goto` to the company page is normal navigation. If
  LinkedIn rotated the session server-side, `handle_auth_wall` on that
  same navigation catches it (the diagram above).
- **Cookie missing or expired** → drive `page.goto("/login")` *inside*
  the recorded context, so the login flow is on camera under `--record`.
  After the goto, if we haven't landed on `/feed/` we call
  `handle_auth_wall` to drive `auto_login` + the MFA pause if needed.

The check uses SQLite's `mode=ro&immutable=1` URI so it can read the
cookies file even when Chromium has it open from a prior run, and
returns False on any error (file missing, permission denied, etc.) so
the caller defaults to running login \u2014 the safe direction.

A user can also invoke the pre-flight path standalone with
`python run_scraper.py --login`; this is idempotent (no-op if already
authenticated) and useful for first-time setup.

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
3. **Runtime scroller discovery.** Walk every descendant of the modal
   in JS, pick the element with `overflowY: auto|scroll|overlay` and
   the largest `scrollHeight - clientHeight` (and `clientHeight > 100px`
   to skip thin spacers). Tag it with `data-reactor-scroller="1"`.
   This replaces the previous hard-coded CSS selector — LinkedIn rotates
   modal class names every few releases, and any element that overflows
   *is* the scrollable element by definition. If nothing overflows, the
   reactor list already fits in one screen (post has ≤10 reactors);
   short-circuit, harvest the visible rows, return.
4. Loop until plateau (2 consecutive cycles with no new items) **or**
   `max_scrolls` reached:
   - `scrollBy({top: 600, behavior: 'smooth'})` on the tagged element —
     incremental + smooth so each scroll is visible on demo recordings
     (an instant `scrollTop = scrollHeight` jump lands in a single frame
     and is invisible to a 30 fps camera)
   - Sleep `SHORT_WAIT_S`
5. On plateau, do one final smooth `scrollTo(scrollHeight)` and re-count;
   if that loaded more rows, reset the plateau counter and keep going.
6. Run `extract_reactors_from_modal` to harvest rows.
7. Close the modal (dismiss button → Escape fallback).

Default cap is 25 scrolls. Each scroll fires another Voyager page (10
reactors), so even when the DOM scrape is the visible fallback, the API
extractor will usually have already pulled those rows from the bucket.
Override with `--max-reactor-scrolls`, or use `--all-reactors` for
plateau-stop with a 500-scroll safety cap.

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
