# Considerations & Trade-offs

Design decisions, alternatives we rejected, and where Camoufox
specifically helps.

---

## 1. Engine: Playwright Chromium vs Camoufox vs `requests`

| Approach | Pros | Cons | Verdict |
|---|---|---|---|
| `requests` / `httpx` against Voyager | Fastest, lightest | Need to forge `x-li-pageInstance`, CSRF token, full cookie jar. Brittle; LinkedIn rotates anti-replay tokens. | ❌ Too brittle |
| **Playwright Chromium + persistent profile** | Real browser → real fingerprint. Same selectors as DevTools. Fast iteration. | Stronger bot-detection signals than Firefox (`navigator.webdriver`, CDP runtime, `window.chrome` quirks) | ✅ **Current choice** |
| Camoufox (patched Firefox) | C++-level fingerprint masking — no `navigator.webdriver`, real GPU canvas/WebGL in headless, Firefox UA matches real TLS JA3 | Slower, no CDP, separate profile from Chromium, smaller community | ✅ For `login.py` only |

**Why mixed**: `login.py` runs once to seed the cookie — high-scrutiny
moment, so Camoufox. The scraper just resumes the warm cookie, so
vanilla Chromium is fine.

### Where Camoufox specifically helps

- **Headless on a workstation.** Chromium headless leaks the
  `SwiftShader` WebGL renderer and CPU-rasterized canvas — easy
  detection. Camoufox headless is fingerprint-indistinguishable from
  Camoufox headed.
- **Cold start with no cookie.** First-time login on a fresh profile is
  when LinkedIn looks hardest at the fingerprint. Camoufox dramatically
  reduces the chance of an MFA challenge here.
- **Linux server with no display.** `Camoufox(headless="virtual")`
  spawns Xvfb internally and runs a real headed Firefox — saves you
  setting up Xvfb manually.
- **Rotating across many companies on one IP.** The more requests you
  make from one fingerprint, the more useful Camoufox's deep masking
  becomes.

### Where Camoufox is unnecessary overhead

- Warm cookie + headed Chromium on a personal machine, single-company
  runs (current usage).
- Anything where you need CDP-only features (e.g. `new_cdp_session`,
  request interception via CDP).

---

## 2. Headed vs headless

| | |
|---|---|
| Playwright Chromium **headless** | Multiple detectable tells: `webdriver=true`, `SwiftShader` WebGL, CPU-rasterized canvas, `0` outerHeight, missing `window.chrome` runtime. JS spoofing patches some but **not** WebGL/canvas/TLS. |
| Playwright Chromium **headed** | Looks like a real browser because it is one. Visible window. |
| Camoufox headless | Genuinely indistinguishable from headed because the spoofs are in C++, not JS. |
| Headed under Xvfb (Linux) | Best of both — real headed browser, no visible window. |

**Current choice**: `headless=False` for the scraper. Switch to
Camoufox headless or Xvfb-wrapped headed only when you need unattended
operation.

### What Playwright can spoof

`browser.new_context(...)` or `launch_persistent_context(...)`:

| Field | API |
|---|---|
| User agent | `user_agent="..."` |
| Viewport / screen size | `viewport={"width":1920,"height":1080}`, `screen={...}` |
| Locale / `navigator.language` | `locale="en-US"` |
| Timezone | `timezone_id="America/New_York"` |
| Geolocation | `geolocation={...}` + `permissions=["geolocation"]` |
| Touch / mobile flags | `is_mobile=True`, `has_touch=True` |
| HTTP headers | `extra_http_headers={...}` |

Plus `add_init_script` for JS-shimmable signals (`navigator.webdriver`,
`languages`, `plugins`, `window.chrome`, etc.).

### What Playwright **cannot** convincingly fake

| Signal | Why |
|---|---|
| `navigator.webdriver` consistency under introspection | `Object.getOwnPropertyDescriptor` can detect the override |
| Canvas / WebGL fingerprint | Real GPU output differs per machine; software rasterizer is uniform |
| Audio fingerprint | `AudioContext` output is deterministic per build |
| Font list | Enumerated via measurement; can't lie about OS fonts |
| TLS / JA3 fingerprint | Playwright's stack ≠ real Chrome's; visible at TLS handshake |
| CDP runtime artifacts | Playwright drives Chrome via CDP; some libs detect this |

These are exactly the gaps Camoufox closes.

---

## 3. Login: vanilla Playwright form-fill vs `login.py` (Camoufox)

Camoufox is **not required** to fill an email input and click "Sign in"
— vanilla Playwright does that fine, and `scraper/auth.py` already does.

What Camoufox gives you for login specifically:
- Lower chance of triggering MFA / captcha on the **first** login from
  a fresh profile.
- TLS JA3 that matches the UA string.

For routine re-auth (cookie expired mid-scrape), `auto_login()` in
`auth.py` runs under the same Chromium and works because the IP and
cookie history are already trusted.

---

## 4. Direct `page.goto(tab_url)` vs click-navigation

| | Pros | Cons |
|---|---|---|
| `page.goto(/company/{slug}/posts/)` | Idempotent, deterministic, easy to retry, easy to URL-pin | Slightly less natural traffic pattern; no `Referer` from previous LinkedIn page |
| Click the tab in the side nav | Matches real-user flow, sends a LinkedIn `Referer`, keeps SPA state warm | Depends on the nav element existing/being clickable; layout changes break it |

**Current choice**: `goto` everywhere. The Voyager calls fire the same
way either way, and we haven't seen any tab gated on Referer. If we
ever see soft blocks (empty responses, "page not available"), the
mitigation is: land on `/company/{slug}/` once, then **click** through
tabs, with `goto` as fallback.

Pagination buttons ("Load more comments", "Show more replies", "See
more reactions") have no URL form — they're always clicks.

---

## 5. Persistent profile vs fresh context per run

| | Pros | Cons |
|---|---|---|
| Persistent (`launch_persistent_context`, `user-data-dir-chrome/`) | Reuses login cookie → no MFA every run. Smaller surface for detection. | One profile = one identity; can't run in parallel. |
| Fresh context | Trivially parallelizable | Need to log in every time → MFA every time |

**Current choice**: persistent. For parallelization, run multiple
profiles each in their own directory (`user-data-dir-chrome-1/`, `-2/`,
…) seeded by separate `login.py` runs.

---

## 6. Data source: API fetch vs API intercept vs DOM extraction

Three fundamentally different ways to get LinkedIn data out of a
session. We use two of the three and explicitly reject the first.

### TL;DR

| Approach | What it is | Verdict |
|---|---|---|
| **API fetch** | Synthesise Voyager calls ourselves (`requests`, `httpx`, or `page.request.get`) using a captured cookie | ❌ Rejected — brittle and high-detection |
| **API intercept** | Passive `context.on("response")` listener that captures Voyager payloads the SPA already fires | ✅ **Primary** |
| **DOM extraction** | Selector-based scrape of the rendered HTML in the live page | ✅ **Fallback / patch** |

### Side-by-side

| Dimension | API fetch | API intercept | DOM extraction |
|---|---|---|---|
| **Detection surface** | High. Headers, anti-replay tokens, JA3, and pageInstance all have to be forged consistently per request | Lowest of the three. The browser fires the call; we just listen | Medium. The user-agent's behaviour (scrolling, modal opens) drives the events. Easy to spike (e.g. open every reactor modal in 5 seconds) |
| **Data shape** | Identical to intercept (it's the same endpoint), but you must keep the GraphQL query hashes / variables in sync yourself | Typed Voyager JSON, exactly as the SPA receives it. No selector drift | HTML — multiple shapes per post type (text/image/video/poll/repost/sponsored). Each shape needs a selector |
| **Auth coupling** | Tightly bound to `JSESSIONID`, `li_at`, `csrf-token`, `x-li-pageInstance`, `x-li-track`. Token rotation breaks runs | Just needs the cookie to be valid for the session — no per-request token plumbing | Just needs the page to render. Same cookie path as intercept |
| **Pagination handling** | Manually compute `start`, `count`, `paginationToken` and re-issue the call. Every endpoint paginates differently | Trigger pagination by **interacting with the page** (scroll, "Load more"). LinkedIn fires the next call; we capture it | Same trigger model as intercept; we then re-walk the DOM |
| **Schema drift cost** | High — query hashes change without notice; you discover it from HTTP 4xx | Low — payloads are typed (`$type`, `entityUrn`). Field renames degrade per-field, not catastrophically. Caught recently: `urn:li:comment` → `urn:li:fsd_comment`, `commentV2` → `commentary` |
| **Engineering effort** | Heavy reverse-engineering up front (one-time) + maintenance burden ongoing | None for the transport layer; effort moves into URL classification (`config.API_ROUTES`) and Voyager schema extractors | Per-selector effort + a fallback chain (see § 7) |
| **Coverage** | 100% of what the API knows | 100% of what the SPA *requests* during the session window (depends on how much the user/scraper interacts) | 100% of what the SPA *renders* (depends on scroll depth, modal opens) |
| **Latency / cost** | One HTTP call per resource you want | "Free" — we get the payloads as a side effect of normal browser usage | Free, but rendering is the slow part of the run anyway |

### Why we rejected API fetch

The forged-request path looks appealing — it's fast and headless-friendly —
but the cost compounds:

1. **Token plumbing is a moving target.** `x-li-pageInstance`,
   `x-li-track`, `csrf-token` all need to match a real
   browser-emitted set. A real session rotates these; forging
   consistent rotation is itself a sub-project.
2. **JA3/TLS fingerprint.** A `requests` call from a Python process
   has a different TLS handshake than the Chrome that issued the
   cookie. LinkedIn correlates these. The cookie can be valid and
   the request still rejected.
3. **GraphQL persisted-query hashes change without notice.** Voyager
   uses `queryId`/`includeWebMetadata` patterns whose hashes LinkedIn
   rotates server-side. A request that worked last week returns
   `{"errors":[...]}` this week, and you only know once your run
   silently produces empty buckets.
4. **No cushion for schema drift.** When LinkedIn renames a field,
   intercepted JSON still arrives — you just notice an empty bucket
   and patch the extractor (we did this twice in one session for
   `urn:li:fsd_comment` and `actor.navigationContext.actionTarget`).
   With forged calls, the whole call can 400-out and you have
   nothing.

### Why intercept is the primary

Passive capture inside `scraper/interceptor.py` is attached to the
`BrowserContext` (not the `Page`), **before** the first navigation, so
popups and service-worker fetches are also captured. Every Voyager
response that the SPA already fires lands in a bucket
(`updates`, `comments`, `reactions`, `profile_lookups`, `products`,
`jobs`, `graphql_other`), classified by URL substring against
`config.API_ROUTES`. This gives us:

- **The exact JSON the SPA sees.** No forging, no token theatre.
- **Zero added traffic.** We don't issue any request LinkedIn didn't
  already expect.
- **Resilience to schema drift.** The transport never breaks;
  individual `api_extractor` functions degrade per-field and are
  patched in isolation.

The cost is that coverage is bounded by *what we interact with*. If
we don't scroll far enough, we don't get the next Voyager page. That's
fixed by the `--all-*` plateau loops in `run_scraper.py`, capped by
the safety limits in `config.py` (`EXHAUSTIVE_POSTS`,
`EXHAUSTIVE_COMMENT_PAGES`, `EXHAUSTIVE_REACTOR_SCROLLS`).

### Why DOM is kept as the fallback patch

We considered killing the DOM path entirely once the API extractors
reached >95% coverage. Three reasons we keep it:

1. **Trailing-response races.** Voyager calls fired late in the
   session can be missed if `_merge` runs before the response lands.
   The `MEDIUM_WAIT_S` settle reduces this but doesn't eliminate it.
2. **Truncated bodies.** Long post bodies are often returned by the
   API with a `…more` suffix; the expanded full text only exists in
   the DOM after the user clicks "see more".
3. **Schema-rename early warning.** When the API extractor silently
   regresses (every `Comment` record rejected because the URN prefix
   changed), the DOM fallback keeps producing data, tagged
   `name_resolution: "dom_tier_*"`. A telemetry alert on
   `rate(dom_fallbacks) / rate(comments) > 0.5` catches the
   regression within one run. This exact signal caught the
   `urn:li:fsd_comment` rename in this codebase.

The DOM path is therefore a) a coverage patch for races and
truncation, and b) a canary that screams when the API path breaks.
It is **not** a primary extraction strategy.

### When you might reconsider API fetch

The only case for the rejected approach is a) pure server-side
batch scraping with no UI scroll budget, b) targeting endpoints that
the SPA *never* calls during normal browsing (e.g. some search
endpoints behind feature flags), and c) accepting that you'll
maintain a Voyager-query-hash database forever. None of those apply
to company-page scraping.

---

## 7. 5-tier author resolution

LinkedIn renders author identity in at least five different DOM shapes
across post types (text, image, video, document, graphic link, poll,
repost, sponsored). Picking one selector and accepting failures left
~30% of comments with "Unknown User". The 5-tier fallback chain brings
this to ~0% (last-tier returns the URN itself, never an empty string
or "Unknown").

---

## 8. Detection signals we don't bother spoofing

We could additionally shim:
- `Notification.permission` ↔ `navigator.permissions.query` consistency
- `navigator.plugins` length
- `WebGLRenderingContext.getParameter` for `UNMASKED_RENDERER_WEBGL`
- `window.chrome.runtime` shape

Skipped because:
- LinkedIn doesn't fail on these on a warm-cookie session
- They're trivial to add later if soft blocks appear
- Some (TLS JA3, canvas hash) **can't** be faked from JS anyway —
  that's a Camoufox problem, not an `add_init_script` problem

---

## 9. Known limitations / gaps

- **Author name on raw company posts.** Posts emitted from the company
  actor itself sometimes leave `author_name` empty because none of the
  5 tiers fires (the company is the author and is identified
  elsewhere). Cosmetic — the post body is still captured.
- **Comments on bottom-page posts.** `_expand_post_comments` opens the
  panel but if the post scrolls *out* of view during expansion, the
  toggle click fails silently. Visible when later posts have
  `comments_count > 0` but `comments: []`. Fix: pin the post into view
  (`scroll_into_view_if_needed`) before the toggle click.
- **Reactor list completeness.** API-derived reactors give us per-post
  `reactions_total` (the real number), but we only *parse* the pages
  the modal scroll fires. With `--max-reactor-scrolls 10` we capture
  the first ~100 reactors per post — every reactor field
  (name/profile/headline/`reaction_type`/degree) comes through cleanly.
  Increase the flag for more pages.
- **`updates` bucket sometimes empty on company timelines.** Mostly
  resolved -- we now route both `voyagerFeedDashUpdates` and
  `voyagerFeedDashOrganizationalPageUpdates` into the `updates` bucket.
  When neither fires (rare), the DOM fallback fills posts with
  truncated visible text.
- **No URL discovery.** You provide the company slug; we don't crawl
  for it.
- **Sequential.** One company at a time. Parallelize by running
  multiple persistent profiles concurrently.
- **No gap-aware re-scrape.** Each run starts from a blank slate; we
  don't read the previous `scraped_<slug>_<tab>.json` to figure out
  what's still missing. A useful follow-up is a *diff-driven* second
  pass: load the prior output, mark posts where
  `len(reactors) < reactions_total`, `len(comments) < comments_count`,
  `author_name` is empty, or `text` got truncated by the API+DOM
  merge, then re-visit *only those URNs* (deep-link via
  `linkedin.com/feed/update/{urn}/`) and run the targeted expansion.
  Cuts incremental refresh cost by 10–100×, makes
  `--max-reactor-scrolls` / `--max-comment-pages` per-post adaptive
  instead of global, and gives the user a clean way to "fill the
  blanks" without re-paying the cost of a full company scrape.
