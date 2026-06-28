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
| `page.goto(/company/<slug>/posts/)` | Idempotent, deterministic, easy to retry, easy to URL-pin | Slightly less natural traffic pattern; no `Referer` from previous LinkedIn page |
| Click the tab in the side nav | Matches real-user flow, sends a LinkedIn `Referer`, keeps SPA state warm | Depends on the nav element existing/being clickable; layout changes break it |

**Current choice**: `goto` everywhere. The Voyager calls fire the same
way either way, and we haven't seen any tab gated on Referer. If we
ever see soft blocks (empty responses, "page not available"), the
mitigation is: land on `/company/<slug>/` once, then **click** through
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

## 6. API-first vs DOM-first extraction

API-first because:
- Voyager fields are typed and don't drift selector-by-selector
- DOM has multiple shapes (graphic-link posts, polls, articles,
  reposts, sponsored)

DOM as second pass because:
- Some posts have no Voyager payload in the captured window (timing)
- Comments and reactors are user-interaction-gated
- Post body is often truncated in the API response (`…more`)

The actual extraction is a **field-by-field merge** (see
[ARCHITECTURE.md](ARCHITECTURE.md#field-by-field-merge)), not a hard
partition. "API-first, DOM-second" describes preference order, not
which pipeline you commit to.

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
- **Reactor list completeness.** Capped by `--max-reactor-scrolls`
  (default 10 ≈ ~100 reactors). For posts with thousands of reactions
  we only get the first N pages. Tune via the flag.
- **`updates` bucket sometimes empty.** Voyager's feed-pagination call
  doesn't always fire during scroll. DOM fallback handles the gap so
  output is still complete, but post `text` will come from the
  truncated visible HTML.
- **Reactors bucket is captured but unused.** We re-extract reactor
  rows from the rendered modal. Future work: parse the `reactions`
  bucket to skip the modal interaction entirely.
- **No URL discovery.** You provide the company slug; we don't crawl
  for it.
- **Sequential.** One company at a time. Parallelize by running
  multiple persistent profiles concurrently.
