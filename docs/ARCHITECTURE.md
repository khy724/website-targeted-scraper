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
        ├─ scraper/interceptor.py    page.on("response") → bucket Voyager payloads
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
       │  page.on("response")            │ XHR / fetch
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
| `author_name` / `author_profile` | API `updates` + `profile_lookups` | DOM 5-tier author resolve |
| `reactions_count` / `reactions_label` | API `updates` | DOM `extract_reactions_from_card` |
| `comments_count` | API `updates` | DOM `extract_comments_count_from_card` |
| `comments[]` | API `comments` bucket | DOM comment-card scrape, dedup'd by URN/text |
| `reactors[]` | DOM (modal scrape) | — (Voyager `reactions` bucket exists but unused) |

---

## 3. Modules

### `scraper/config.py`
Single source of truth. Every selector, URL signature, timeout, and
bucket name lives here. When LinkedIn ships a DOM change, fix it here once.

Key sections:
- `TAB_PATHS`, `DEFAULT_TABS` — which tabs to visit
- `COMPANY_URL_SIGNATURES` — fragments used to bucket Voyager responses
- `POST_CARD`, `COMMENT_CARD`, `REACTOR_ITEM` — DOM hooks
- `POST_REACTIONS` (read) vs `POST_REACTIONS_BUTTON` (click) — split so
  we don't try to click a non-clickable counter `<span>`
- `AUTH_URL_FRAGMENTS`, `AUTH_DOM_HOOKS`, `MFA_TEXT_PATTERNS` —
  auth-wall detection

### `scraper/browser.py`
`launch()` context manager. Opens Chromium with:
- Persistent profile at `user-data-dir-chrome/` (reuses the login cookie)
- `--disable-blink-features=AutomationControlled`
- Init script that hides `navigator.webdriver`, sets `languages`, `plugins`

`safe_goto()` wraps `page.goto` and calls `handle_auth_wall` after
every navigation so a stale cookie auto-recovers mid-run.

### `scraper/interceptor.py`
Attached **before** the first navigation. Classifies every Voyager
response into one of these buckets by URL substring:

| Bucket | Triggered by |
|---|---|
| `company_overview` | Company root / about queries |
| `updates` | The feed of activity cards |
| `comments` | `voyagerSocialDashSocialDetails` etc. |
| `reactions` | Reaction counts and reactor lists |
| `profile_lookups` | Member-dash queries — URN → name mapping |
| `jobs`, `products` | Tab-specific |
| `graphql_other` | Everything else (kept for inspection) |

`CapturedPayloads.reset()` clears the buckets between tabs so we don't
cross-contaminate.

### `scraper/api_extractor.py`
Pure transformers: each function takes a bucket of payloads and returns
records (posts, comments, jobs, …). Knows the shape of Voyager output.

### `scraper/dom_extractor.py`
Per-card DOM extractors and the **5-tier author resolution**:

1. URN → look up in `profile_lookups` bucket
2. `aria-label="View Jane Doe's profile"` — strips suffixes
   (`'s profile`, `'s graphic link`, `'s page`)
3. `a[href*='/in/<slug>']` text content
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
   to trigger both lazy-loader callbacks and IntersectionObservers
4. `_expand_post_comments` — click toggle, then `Load more comments`,
   `Show more replies`, comment-body `see more`
5. `_collect_reactors_for_post` (optional, `--reactors`) — click the
   reactions button, scroll the modal until plateau, harvest rows
6. `_merge()` — API records as base, DOM patches where API was empty

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
email+password form and LinkedIn's "Welcome back, <name>" password-only
screen. Email is best-effort; password is mandatory. If the submit
button can't be found it falls back to pressing Enter on the password
field.

---

## 5. Reactor harvesting

The reactor list is collected via DOM modal interaction (Voyager's
`reactions` bucket is captured but not currently parsed). Flow in
`_collect_reactors_for_post`:

1. Click the post's reactions button (`POST_REACTIONS_BUTTON =
   button[data-reaction-details]`).
2. Wait for `REACTORS_MODAL` to be visible.
3. Loop until plateau (2 consecutive scrolls with no new items) **or**
   `max_scrolls` reached:
   - Set the modal's `scrollTop = scrollHeight`
   - Sleep `SHORT_WAIT_S`
4. Run `extract_reactors_from_modal` to harvest rows.
5. Close the modal (dismiss button → Escape fallback).

Default cap is 10 scrolls (~100 reactors). Override with
`--max-reactor-scrolls`.

---

## 6. Extension points

| Want to add… | Touch |
|---|---|
| A new tab (e.g. `/people/`) | `config.TAB_PATHS`, `config.DEFAULT_TABS`, optionally a new bucket in `interceptor.py` + a parser in `api_extractor.py` |
| Multi-company batch | Small driver above `scraper/main.py:scrape_company` |
| Crawler that discovers slugs | New module (not part of this scraper) feeding URLs into the batch driver |
| Headless production runs | Switch engine to Camoufox in `browser.py`, or wrap headed Chromium in Xvfb (Linux). See [CONSIDERATIONS.md](CONSIDERATIONS.md) |
| Parallel scraping | Multiple persistent profile dirs, one process per profile |
| Database output instead of JSON | Replace the `json.dump` calls in `scrape_company` with your sink |
