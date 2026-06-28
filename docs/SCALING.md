# Scaling to a Multi-Tenant Automation Platform

How to take this scraper from "one analyst running it on a laptop"
to a hosted, multi-tenant service that runs unattended for many
customers in parallel, with durable workflows and graceful recovery
from every common failure mode.

This document is self-contained: it explains the concepts (browser
fingerprinting, WebRTC leaks, behavioral pacing, durable workflows)
and then shows exactly where each one plugs into the current code.

---

## 0. Where we are today

```
analyst's laptop
  └─ python run_scraper.py --url <linkedin-company-url> --tabs posts --all-*
        │
        ├─ scraper/browser.py        launches headed Chromium against
        │                            ./user-data-dir-chrome/  (one cookie jar)
        ├─ scraper/interceptor.py    passive capture from BrowserContext
        ├─ scraper/api_extractor.py  Voyager JSON → records
        ├─ scraper/dom_extractor.py  DOM patch for fields API missed
        └─ scraped_data/scraped_<slug>_<tab>.json
```

Properties of the current setup:

- **Single account.** One cookie jar at `user-data-dir-chrome/`.
- **Single machine, headed.** A visible Chromium window. No
  scheduling; the analyst presses Enter.
- **No retry semantics.** If the laptop sleeps mid-scroll, the run
  is lost.
- **No tenant boundary.** Output goes to one folder on disk.

Each of these becomes a hard requirement to dismantle when we scale.

---

## 1. The core insight: detection is risk-scoring, not rule-matching

LinkedIn (and every mature platform) does **not** look for a single
"bot signature". It accumulates a continuous risk score from
independent signals:

$$\text{Risk} = \underbrace{\text{Network}}_{\text{IP, ASN, geo}} + \underbrace{\text{Browser}}_{\text{UA, canvas, WebGL}} + \underbrace{\text{Behavioral}}_{\text{timing, volume}} + \underbrace{\text{Historical}}_{\text{drift from baseline}}$$

A score below a soft threshold → nothing happens. Cross the soft
threshold → CAPTCHA / email verification. Cross the hard threshold
→ temporary restriction. Sustained breach → permanent ban.

**Implication for the architecture.** We do not try to "evade
detection forever". We design for **identity stability** (the
account looks like the same person every session) plus
**conservative behavioral pacing** (volume grows gradually from the
account's own history), and we **monitor the score predictively**
so we can pause before LinkedIn does.

---

## 2. Target architecture (three layers)

```
                ┌──────────────────────────────────────┐
                │       Application Layer              │
                │  • Tenant + account registry         │
                │  • Encrypted credential vault        │
                │  • Risk engine & operator dashboard  │
                └──────────────────┬───────────────────┘
                                   │
                                   ▼
                ┌──────────────────────────────────────┐
                │       Orchestration Layer            │
                │  • Durable workflows (one per acct)  │
                │  • Schedule + behavioral pacing      │
                │  • Retry, backoff, manual-review     │
                └──────────────────┬───────────────────┘
                                   │
                                   ▼
                ┌──────────────────────────────────────┐
                │       Browser Runtime Layer          │
                │  • Persistent profile per account    │
                │  • Pinned proxy / static IP          │
                │  • Existing scraper/* modules        │
                └──────────────────┬───────────────────┘
                                   │
                                   ▼
                               LinkedIn
```

| Layer | Responsibility | Today's analogue |
|---|---|---|
| Application | Multi-tenant CRUD, secrets, risk policy | None — manual |
| Orchestration | Durable, schedulable, retryable execution | `run_scraper.py` invoked by hand |
| Browser runtime | Real browser + identity + scrape logic | `scraper/browser.py` + the rest of `scraper/` |

The browser runtime layer is where today's code lives almost
unchanged. The two new layers wrap around it.

---

## 3. Tenant and account data model

```
Tenant (1) ──< Account (N) ──1:1── BrowserProfile ──1:1── NetworkIdentity
                  │
                  └─< RunHistory (every workflow execution)
                  └─< RiskTimeline (rolling risk-score samples)
                  └─< CredentialVersion (encrypted, rotatable)
```

The strict **1:1 binding** between `Account ↔ BrowserProfile ↔
NetworkIdentity` is the most important invariant in the system.
Breaking it is the single largest detection trigger: an account
that appears with a new fingerprint, or from a new ISP, looks
exactly like a compromised account or a bot.

### Minimum schema

```json
{
  "tenant_id":         "tnt_<uuid>",
  "account_id":        "acc_<uuid>",
  "linkedin_handle":   "<email>",
  "credential_ref":    "vault://tenant/<id>/li-password#v3",
  "profile_dir":       "s3://profiles/<account_id>/chrome/",
  "network": {
    "mode":            "static_ip" | "residential_pool",
    "anchor":          "104.x.x.x" | "ASN-15169:US-CA-SanFrancisco"
  },
  "fingerprint": {
    "user_agent":      "Mozilla/5.0 ...",
    "viewport":        {"width": 1920, "height": 1080},
    "timezone":        "America/New_York",
    "locale":          "en-US",
    "platform":        "Win32"
  },
  "persona":           "recruiter" | "engineer" | "sales_exec",
  "warmup_stage":      1 | 2 | 3 | 4,
  "status":            "ACTIVE" | "RISK_DETECTED" | "PAUSED" | "BLOCKED",
  "baseline": {
    "avg_searches_per_day":     8,
    "avg_profile_views_per_day": 12,
    "peak_hours":               [9,10,11,14,15,16],
    "timezone_offset":          -300
  }
}
```

### Tenant isolation rules

- **No shared cookie jar, ever.** Each `Account` writes to its own
  `profile_dir` (today: `user-data-dir-chrome/`). A worker that
  picks up Account A must mount only A's profile.
- **No shared proxy.** Each account is pinned to one network
  anchor; rotating it between accounts is the first thing a
  detection system would correlate on.
- **No shared output bucket.** Scraped data lands in
  `s3://output/<tenant_id>/<account_id>/...` with object-level ACLs.

---

## 4. Browser identity: consistency beats uniqueness

The single biggest mistake teams make: **randomizing the
fingerprint between sessions**. Real users do not change User-Agent
on Tuesday. An account that appears with a fresh fingerprint every
login is more suspicious than one that has used the same one for a
year.

```
LinkedIn Account ──► Persistent Browser Profile ──► Stable Fingerprint
                          (saved to disk)              (re-injected every launch)
```

| Day | Consistent (credible) | Randomized (high risk) |
|---|---|---|
| Mon | Chrome 137, Win 11, 1920×1080, en-US, NY tz | Win10 / Chrome 136 |
| Tue | Chrome 137, Win 11, 1920×1080, en-US, NY tz | macOS / Safari 17 |
| Wed | Chrome 137, Win 11, 1920×1080, en-US, NY tz | Linux / Chrome 138 |

### What we pin per account

Already supported by Playwright's `launch_persistent_context`
(see [scraper/browser.py](../scraper/browser.py)):

| Signal | API | Today | Multi-tenant |
|---|---|---|---|
| Profile dir | `user_data_dir=` | one shared dir | one per `account_id` |
| User-Agent | `user_agent=` | `config.USER_AGENT` (global) | from `account.fingerprint` |
| Viewport / screen | `viewport=`, `screen=` | global | per account |
| Locale | `locale=` | not set | per account |
| Timezone | `timezone_id=` | not set | per account |
| `navigator.webdriver`, plugins, languages | `add_init_script` | global stealth | per-account stealth pack |

### What Playwright **cannot** convincingly fake

These leak the underlying machine no matter how much JS you patch:

- **Canvas / WebGL output** — real GPU vs software rasterizer
- **TLS JA3 / JA4 fingerprint** — Chromium's TLS stack is identifiable
- **Audio context fingerprint**
- **`navigator.webdriver` under deep introspection**
  (`Object.getOwnPropertyDescriptor`)

For headless multi-tenant operation this matters. Two options:

1. **Camoufox** (patched Firefox): C++-level masking that survives
   introspection. Already used for `login.py` in this repo.
   Headless Camoufox is fingerprint-indistinguishable from headed.
2. **Headed Chromium under Xvfb** on a Linux worker: the browser
   is genuinely headed (real GPU canvas via SwiftShader/llvmpipe is
   still a tell — use a real GPU if possible, or run inside a VM
   with virtio-gpu).

| Engine | Best for | Avoid when |
|---|---|---|
| Headed Chromium on workstation | First login, manual review, dev | Scaling beyond a handful of accounts |
| Camoufox headless | Bulk unattended cold logins | You need Chrome DevTools Protocol features |
| Headed Chromium + Xvfb (Linux worker) | Most production runs once warm cookie exists | Cold logins from a fresh fingerprint |

---

## 5. Network identity: pinning the path to the internet

Network drift is the fastest path to a flagged account. Logging in
from US-east on Monday, Germany on Tuesday, and Brazil on
Wednesday triggers verification within hours.

```
Account ──► one stable network anchor for life
              │
              ├─ Option 1: dedicated static IP        (simple, costly)
              └─ Option 2: residential pool pinned to ASN + city
                                                       (organic, complex)
```

### Option 1: dedicated static IP

- **Topology:** 1 account → 1 profile → 1 static IPv4.
- **Pros:** Trivial to debug. Network is provably stable. No
  vendor-side IP rotation surprises.
- **Cons:** Datacenter ASNs are often pre-flagged. Cost grows
  linearly with account count.
- **Use when:** You have ≤ 100 accounts and want operational
  simplicity.

### Option 2: residential pool pinned to ISP+city

- **Topology:** 1 account → 1 profile → rotating residential IPs
  that all advertise the **same ASN and city** (e.g. Comcast,
  Atlanta).
- **Pros:** Mimics a real consumer whose IP changes when their
  router reboots, but never leaves their neighborhood.
- **Cons:** Higher bandwidth cost. Vendor liveness varies — need
  health-check + fallback.
- **Use when:** Scale > 100 accounts, or a pilot shows static-IP
  challenge rates are too high.

### WebRTC and DNS leaks

The proxy can be flawless and the account still gets flagged
because of a **WebRTC leak**: the browser advertises its real
public IP via ICE candidates even when HTTP traffic goes through
the proxy.

```js
const pc = new RTCPeerConnection();
pc.createDataChannel("probe");
pc.onicecandidate = e => { if (e.candidate) console.log(e.candidate.candidate); };
pc.createOffer().then(o => pc.setLocalDescription(o));
```

Compare each candidate against the expected proxy IP:

| Candidate | Verdict | Action |
|---|---|---|
| Matches proxy public IP (e.g. `104.x.x.x`) | PASS | Continue |
| No public candidates emitted | PASS | Continue (WebRTC properly masked) |
| Private (`192.168.x.x`, `10.x.x.x`) | WARN | Log, continue |
| **Different public IP** (e.g. host ISP `49.x.x.x`) | **FAIL** | **Terminate session, mark profile broken** |

This is an automated activity that must run at every profile
provisioning and as a periodic health-check. Output goes into the
session telemetry record.

---

## 6. Durable workflows for orchestration

A multi-account scraper running on a schedule has dozens of
half-finished states at any moment. Without durable execution you
get duplicate actions, silent data loss, and zombie sessions on
crashes.

Use a workflow engine (Temporal is the canonical choice; AWS Step
Functions or a homegrown Postgres-backed state machine also work).
Model **each account as a long-running workflow** that survives
worker crashes, deployments, and infra failures **without
re-executing actions that already happened**.

```
Account workflow (per account, never dies)
  │
  ├─ activity: provision_profile           ─┐
  ├─ activity: validate_network             │   each activity is
  ├─ activity: warmup_session (gated)       │   idempotent and
  ├─ activity: scrape_company(url)          │   recorded once
  ├─ activity: cooldown(distribution)       │   in the workflow
  ├─ activity: scrape_company(url)          │   event history
  └─ ... (loops forever, paced by behavioral engine) ─┘
```

### Mapping current code into activities

| Current function | Becomes activity |
|---|---|
| `scraper.main.scrape_company` | `scrape_company(account_id, url, tabs, flags)` |
| `scraper.browser.launch` | `provision_browser(account_id)` (mounts profile, attaches proxy) |
| `scraper.auth.auto_login` | `recover_session(account_id, reason)` |
| `scraper.dom_extractor.extract_reactors_from_modal` | stays inside `scrape_company` activity |
| WebRTC probe (new) | `validate_network(account_id)` |
| Risk-score recompute (new) | `recompute_risk(account_id)` |

**Activity contract.** Each activity must:

1. **Be idempotent.** Re-running `scrape_company` with the same
   inputs must not produce double-counted records. Use a content
   hash on the output and let the worker dedupe.
2. **Heartbeat.** Long-running activities (`scrape_company` can
   take 30+ minutes for `--all-*`) must heartbeat so the
   orchestrator knows the worker is alive.
3. **Bounded retries.** Auth-wall failures retry up to N with
   exponential backoff. Hard failures (BLOCKED, BANNED) escalate
   to manual review without retrying.

---

## 7. Behavioral pacing: four layers of "humanness"

Even with perfect network and fingerprint identity, an account
that performs 200 profile views in 5 minutes is gone. Behavior is
where most automation fails. Layer it explicitly:

```
┌────────────────────────────────────────────────────────────────┐
│ 4. Long-term (weeks-months) — drift metrics, warm-up           │
├────────────────────────────────────────────────────────────────┤
│ 3. Daily        — working hours, daily action budgets          │
├────────────────────────────────────────────────────────────────┤
│ 2. Session      — composition (feed/search/view mix), bursts   │
├────────────────────────────────────────────────────────────────┤
│ 1. Action       — variable dwell time, content-aware pauses    │
└────────────────────────────────────────────────────────────────┘
```

### Level 1 — action-level

Replace `time.sleep(3.0)` between clicks with an asymmetric
distribution. Real users wait 4s, then 13s, then 8s, then 22s, not
3.0s every time. For dwell time on a profile, scale to the content
length: tiny stub profile → ~10s; rich profile with deep work
history and many connections → 30–60s.

### Level 2 — session-level

Sessions blend tasks. A scraping session that does
**only profile views** for 40 minutes is a flag. Mix in feed
scrolling and idle:

```json
{
  "feed_scroll": 0.40,
  "search":       0.20,
  "profile_view": 0.30,
  "idle":         0.10
}
```

Today's `scraper/main.py` already does some of this on the company
posts tab (scroll, expand comments, open reactor modal). For
account-level scaling we extend that pattern: visit the home feed
first, do a vanity search, then reach the company page.

### Level 3 — daily

Two rules:

- **Temporal anchoring.** Only run during the account's historical
  active hours (`peak_hours: [9,10,11,14,15,16]` in the account's
  timezone). A 03:00 local-time session is a 100% bot signal.
- **Dynamic daily budget.** Don't hard-code "max 50 profiles/day".
  Compute it from the account's own baseline:

  $$\text{Daily Budget} = \text{Historical Average} \times \text{Growth Factor}$$

  where `Growth Factor` is 1.0 at warmup stage 1 and rises to
  ~2.5 by warmup stage 4. Never let it spike past ~3×.

### Level 4 — long-term drift

Monitor the ratio:

$$\text{Drift Ratio} = \frac{\text{Current 7-day Volume}}{\text{Trailing 90-day Average}}$$

- Drift ≤ 2× → normal.
- 2× < Drift ≤ 5× → soft pause: skip the next scheduled session.
- Drift > 5× → hard pause: set `status = RISK_DETECTED`,
  notify operator.

### Warm-up gating

New or dormant accounts walk a gated ladder. Workflow refuses to
advance to stage N+1 until stage N has completed cleanly for the
required dwell window.

```
[Stage 1: Cold]     ──► [Stage 2: View Gate] ──► [Stage 3: Search Scale] ──► [Stage 4: Production]
 login + feed only       + light profile views    + targeted searches          full --all-* runs
 (7 days)                (7 days)                  (14 days)                    unconstrained
```

---

## 8. Failure modes and recovery

Every failure mode below maps to existing code, an existing
escape hatch, or a gap that this scaling design must fill.

### 8.1 Failure-mode catalogue

| # | Mode | Detection signal | Severity | Recovery |
|---|---|---|---|---|
| 1 | **Auth wall mid-run** (stale cookie) | `is_auth_wall(page)` in [scraper/auth.py](../scraper/auth.py) returns True after `safe_goto` | Low | Call `auto_login`; if still walled, escalate to manual review queue |
| 2 | **MFA / email verification challenge** | URL contains `checkpoint/challenge` OR `MFA_TEXT_PATTERNS` match after login submit | Medium | Pause workflow, surface to operator with screenshot, wait for human |
| 3 | **CAPTCHA** (puzzle/funCaptcha) | iframe origin matches `*.arkoselabs.com` OR `recaptcha` selector visible | Medium | Pause; in pilot, solve manually; production option: solver vendor (LinkedIn does not officially permit this) |
| 4 | **Restriction screen** ("Your account has been restricted") | Specific URL `/restricted` or banner text | High | `status = BLOCKED`. Stop workflow. Operator must contact LinkedIn support. |
| 5 | **Hard ban** | Login itself fails with permanent-suspension copy | Terminal | `status = BANNED`. Archive profile dir. Account is dead. |
| 6 | **Proxy down** | Connection timeout to LinkedIn; WebRTC probe shows no public candidate | Medium | Failover to backup IP in the same ASN/city if available; otherwise pause workflow with `network_unavailable` reason |
| 7 | **WebRTC leak detected** | Probe returns mismatched public IP | High | Terminate session immediately; mark profile `broken_fingerprint`; do not retry on the same profile |
| 8 | **Worker crash mid-scrape** | Activity heartbeat lapses | Low | Workflow engine retries the activity. Output is content-hashed → no duplicates |
| 9 | **DOM schema change** | Selector returns 0 elements when it normally returns ≥1 | Medium | Existing five-tier author resolver + API-first merge already handles most of this. Telemetry alert when one tier resolves > 90% of records (means upstream tier broke) |
| 10 | **Voyager field rename** | API extractor returns 0 records while DOM still works | Medium | Telemetry alarm on `api_records / dom_records < 0.1` over rolling window. Already learned: see the recent `urn:li:fsd_comment` and `actor.navigationContext` regressions |
| 11 | **Rate-limited by Voyager** (HTTP 429) | Interceptor sees 429 responses | Medium | Cooldown the account for 6–24h; recompute drift |
| 12 | **Risk threshold exceeded** (internal) | `risk_score > soft_threshold` | Medium | Auto-pause; recompute baseline; re-enter warm-up if needed |
| 13 | **Tenant credential rotated** | LinkedIn rejects password at login | Low | Mark `credential_version` stale, pull latest from vault, retry once |
| 14 | **Disk full** (profile + dumps grow) | Worker disk usage > 85% | Low | Rotate `api_dumps/` to cold storage on a schedule |
| 15 | **Output corruption** (partial JSON) | Validation step at end of activity | Low | Workflow rolls back the activity; retry once |

### 8.2 Recovery decision flow

```
                       activity raises
                             │
                             ▼
                  classify_error(exception)
              ┌──────────────┼──────────────┐
              ▼              ▼              ▼
      TRANSIENT         CHALLENGE         TERMINAL
   (network, 5xx)     (auth, MFA,        (banned,
              │       CAPTCHA, 429)      restricted)
              ▼              │              │
       exponential          ▼              ▼
       backoff retry  pause workflow   status = BLOCKED
       (max N)        +  notify        stop workflow
              │       operator with    notify operator
              ▼       screenshot       archive profile
        success?           │
              ▼            ▼
        resume       human resolves
                     →  resume workflow
                     OR  escalate to TERMINAL
```

### 8.3 The three-layer auth recovery (already in this repo)

The auth module today implements the bottom rung of this design.
Keep it; just feed its outcomes into the workflow engine.

```
safe_goto(url)
   │
   ▼
is_auth_wall(page) ?
  no                      yes
   │                       │
   │              _looks_like_mfa(page) ?
   │              no                yes
   │              │                  │
   │         auto_login              │
   │              │                  │
   │       still walled ?            │
   │       no      yes──────────────►├─►  _manual_pause (interactive)
   │       │                         │       in CI: raise → workflow
   │       │                         │       engine catches → MFA flow
   │       │                         ▼
   │       │                  still walled ?
   │       │                  no       yes
   │       │                   │        │
   │       │                   │        └─► raise RuntimeError
   │       └───────────────────┘            (status = RISK_DETECTED)
   ▼
proceed
```

In multi-tenant mode, `_manual_pause` is replaced by an activity
that **enqueues a review task** and returns control to the
workflow engine. A human operator picks the task up in the
dashboard, solves the challenge in a remote-control browser
session, and resumes.

### 8.4 Predictive risk scoring

The whole point of layering pacing, warm-up, and drift checks is
that we can compute an internal risk score and act on it **before**
LinkedIn does:

$$\text{Risk Score} = w_1 \cdot \text{VelocityDeviation} + w_2 \cdot \text{SessionLengthDeviation} + w_3 \cdot \text{GeoDrift} + w_4 \cdot \text{FingerprintDrift} + w_5 \cdot \text{RestrictionHistory}$$

Compute it after every session. Transitions:

| Score | Status | Effect |
|---|---|---|
| `< 0.5` | `ACTIVE` | Normal scheduling |
| `0.5 – 1.0` | `ACTIVE` | Reduce daily budget by 30% |
| `1.0 – 2.0` | `RISK_DETECTED` | Pause workflow for 24h |
| `> 2.0` | `PAUSED` | Operator review required |

---

## 9. Telemetry

Every session writes one record:

```json
{
  "session_id":        "sess_<ulid>",
  "tenant_id":         "tnt_<id>",
  "account_id":        "acc_<id>",
  "started_at":        "2026-06-28T14:02:11Z",
  "duration_seconds":  2143,
  "outcome":           "COMPLETED" | "AUTH_CHALLENGE" | "BLOCKED" | "ERROR",
  "network": {
    "proxy_ip":      "104.x.x.x",
    "webrtc_ips":    ["104.x.x.x"],
    "dns_leak":      false,
    "webrtc_leak":   false
  },
  "fingerprint_hash":  "sha256:...",
  "activity": {
    "posts_scraped":   47,
    "comments":        163,
    "reactors_api":    412,
    "reactors_dom":    0,
    "api_buckets":     {"updates": 5, "comments": 71, "reactions": 18},
    "dom_fallbacks": {
      "author_resolve_tier_B_aria": 0,
      "author_resolve_tier_E_urn":  0
    }
  },
  "risk_score":        0.7,
  "warmup_stage":      4
}
```

Two of these fields are early-warning beacons:

- **`dom_fallbacks.*` rising** = the API extractor is missing
  something. This is what caught the `urn:li:fsd_comment` field
  regression — `dom_tier_B_aria` was resolving 100% of comments
  because the API path was returning 0.
- **`api_buckets.updates == 0` while DOM posts > 0** = Voyager
  feed query was renamed.

A simple Prometheus alert on
`rate(dom_fallbacks_tier_B_total) / rate(comments_total) > 0.5`
would have caught the recent regression within one scheduled run.

---

## 10. Phased rollout from today's code

```
┌─────────────────┐   ┌─────────────────┐   ┌─────────────────┐
│ Phase 1         │──►│ Phase 2         │──►│ Phase 3         │
│ Per-account     │   │ Workflow engine │   │ Behavioral      │
│ profile + proxy │   │ + retries       │   │ pacing engine   │
└─────────────────┘   └─────────────────┘   └─────────────────┘
                                                    │
┌─────────────────┐   ┌─────────────────┐           ▼
│ Phase 5         │◄──│ Phase 4         │◄── Core platform
│ Pilot (5-10     │   │ Risk scoring +  │
│ real accounts)  │   │ WebRTC probes   │
└─────────────────┘   └─────────────────┘
```

### Phase 1 — Per-account profile + proxy (smallest viable change)

- Parameterise `config.USER_DATA_DIR` to take an `account_id`.
- Parameterise `config.USER_AGENT`, `VIEWPORT`, and stealth init
  script per account; read from the account record at launch.
- Add a `--proxy` flag that maps to Playwright's
  `proxy={"server": "...", "username": "...", "password": "..."}`
  in `launch_persistent_context`.
- Output goes to `scraped_data/<account_id>/...`.

After this phase, the scraper can be invoked once per account from
a cron loop. No durability yet, but tenant isolation holds.

### Phase 2 — Workflow engine + retries

- Wrap `scrape_company` as a Temporal activity (or equivalent).
- Convert the in-process auth recovery to surface its outcomes to
  the workflow (`AuthChallengeException`, `RestrictedException`,
  `TerminalBanException`).
- Add the `enqueue_manual_review` activity backed by a simple
  Postgres queue + operator UI.

After this phase, runs survive worker restarts. Failures route to
a human queue instead of silently crashing.

### Phase 3 — Behavioral pacing engine

- Introduce the `Behavioral Engine` service. Given an account and
  goal, it returns a session plan: a sequence of
  `(action, dwell_ms)` tuples.
- Replace fixed `time.sleep` calls in `scraper/main.py` with the
  generated dwell times.
- Add warm-up state machine and reject session plans that violate
  the current warmup_stage.

### Phase 4 — Risk scoring + WebRTC probes

- Add `validate_network` activity that runs before every session.
- Score every session at completion; persist into `RiskTimeline`.
- Wire the `risk_score → status` transitions in §8.4.

### Phase 5 — Pilot

- 5–10 real accounts, two weeks. Measure:
  - challenge rate (CAPTCHAs + MFA / session)
  - restriction rate (`status = BLOCKED` / month)
  - data yield (`comments + reactors_api` per session)
  - cost per 1000 records
- If static-IP challenge rate is acceptable, stay on Option 1.
  Otherwise migrate the network layer to residential pools.

---

## 11. Engineering trade-offs

1. **Throughput vs. account longevity.** Conservative pacing
   caps daily yield but is the only way accounts survive past a
   few months. Volume targets must be specified per-account
   per-day, not platform-wide.
2. **Static IP vs. residential pool.** Static IPs are operationally
   trivial but live on flagged datacenter ASNs. Residential pools
   are organic but require vendor management and bandwidth budget.
   Start static, migrate per pilot evidence.
3. **Fingerprint spoofing vs. behavioral control.** Returns
   diminish fast on fingerprint engineering past "consistent". The
   incremental risk reduction from advanced spoofing is small
   compared to getting pacing and history right.
4. **Headed Chromium vs. headless Camoufox.** Headed is dev-time
   friendly and survives the first login best; headless is the
   only path to dense per-worker concurrency. The repo already
   uses Camoufox in `login.py` precisely for this reason — extend
   that pattern to all unattended runs.
5. **API-first vs. DOM-first extraction.** Already settled in this
   codebase: API primary, DOM as patch. Multi-tenant only
   amplifies the value — DOM runs are slower and a worse
   detection signal than passive Voyager interception.

---

## 12. What the existing scraper gives you for free

Most of the per-account work is already done. The repo already
has:

- **Persistent profile** that holds a warm cookie across sessions
  → just needs to be parameterised per account.
- **Stealth init script** in [scraper/browser.py](../scraper/browser.py)
  → already hides `navigator.webdriver`, fakes `languages`,
  `plugins`.
- **Three-layer auth recovery** in [scraper/auth.py](../scraper/auth.py)
  → already classifies auth wall vs MFA vs total failure.
- **Passive Voyager capture** in [scraper/interceptor.py](../scraper/interceptor.py)
  → already attached to `BrowserContext` (not Page) so popups and
  service workers are caught.
- **API-first extraction with DOM fallback** in [scraper/api_extractor.py](../scraper/api_extractor.py)
  and [scraper/dom_extractor.py](../scraper/dom_extractor.py)
  → the field-by-field merge in `_merge` means individual API
  schema regressions degrade gracefully instead of breaking the run.
- **Plateau-stop scraping loops** with safety caps
  (`EXHAUSTIVE_POSTS`, `EXHAUSTIVE_COMMENT_PAGES`,
  `EXHAUSTIVE_REACTOR_SCROLLS` in [scraper/config.py](../scraper/config.py))
  → already bounded; just expose them per-account.
- **Camoufox login** in `login.py` for high-scrutiny cold starts
  → already proven.

The scaling work above is wrapping these primitives in a tenant
model, a durable workflow runtime, and a behavioral pacing
engine — **not** rewriting the scraper.
