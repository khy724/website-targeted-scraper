"""Single source of truth for selectors, URL signatures, paths, and timeouts.

Reliability ranking (from experiments.md), reflected by what's used below:
    HIGHEST : API JSON payloads
    HIGH    : data-test-*, data-urn          (stable, designed for QA)
    MED-HI  : aria-label (View ..., headline ...)
    MED     : role/text locators, /in/<slug> hrefs
    VERY LOW: CSS class names, deep div/span hierarchy  -- AVOID
"""
from __future__ import annotations

from pathlib import Path

# ---------------------------------------------------------------------------
# Filesystem
# ---------------------------------------------------------------------------
REPO_ROOT: Path = Path(__file__).resolve().parent.parent
USER_DATA_DIR: Path = REPO_ROOT / "user-data-dir-chrome"
API_DUMP_DIR: Path = REPO_ROOT / "api_dumps"
SCRAPED_DATA_DIR: Path = REPO_ROOT / "scraped_data"
OUTPUT_FILE: Path = SCRAPED_DATA_DIR / "scraped_company.json"

# ---------------------------------------------------------------------------
# Browser
# ---------------------------------------------------------------------------
USER_AGENT: str = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
VIEWPORT = {"width": 1920, "height": 1080}
LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--start-maximized",
]
STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 12});
window.chrome = window.chrome || { runtime: {} };
"""

# ---------------------------------------------------------------------------
# Timeouts (ms unless noted)
# ---------------------------------------------------------------------------
NAV_TIMEOUT_MS = 45_000
SELECTOR_TIMEOUT_MS = 15_000
SHORT_WAIT_S = 1.2
MEDIUM_WAIT_S = 2.5
LONG_WAIT_S = 4.0

# ---------------------------------------------------------------------------
# Auth-wall signatures
# ---------------------------------------------------------------------------
AUTH_URL_FRAGMENTS = (
    "/login",
    "/authwall",
    "/checkpoint",
    "/uas/login",
    "linkedin.com/signup",
)
AUTH_DOM_HOOKS = (
    "input[name='session_key']",
    "form.login__form",
    "input#username",
    "input[autocomplete='username']",
    "[data-test-id='captcha-internal']",
    "iframe[title*='captcha' i]",
    # Contextual sign-in modal shown to guests on /company/<slug>/. The
    # form fields inside are CSS-hidden until 'Sign in with Email' is
    # clicked, so we match the modal container instead of the inputs.
    "div.contextual-sign-in-modal",
    "div#base-contextual-sign-in-modal",
)
# Buttons that reveal a hidden login form inside an in-page modal. Clicking
# one of these turns a contextual auth-wall into a normal login form that
# auto_login can fill.
AUTH_REVEAL_BUTTONS = (
    "button[data-tracking-control-name='sign-in-with-email-cta']",
    "button.contextual-sign-in-modal__sign-in-with-email-cta",
    # /authwall page renders a JOIN form by default; this toggle swaps to
    # the sign-in form (otherwise we'd POST creds to /signup/api/createAccount).
    "button[data-tracking-control-name='auth_wall_desktop-login-toggle']",
    "button.authwall-join-form__form-toggle--bottom",
)
# Things that only appear on MFA / extra-challenge screens (not first-time login).
MFA_TEXT_PATTERNS = (
    r"two-?step verification",
    r"enter the code",
    r"security check",
    r"verify it's you",
    r"unusual activity",
)

# ---------------------------------------------------------------------------
# Voyager / GraphQL URL signatures -> logical buckets
# ---------------------------------------------------------------------------
# We match by substring. Order matters: most-specific first.
API_ROUTES: tuple[tuple[str, str], ...] = (
    ("voyagerOrganizationDashCompaniesByUniversalName", "company_overview"),
    ("OrganizationDashCompanies", "company_overview"),
    # Feed updates. The current query is `voyagerFeedDashUpdates` (no V2 suffix);
    # the V2 variant exists on some pages. Substring match catches both.
    ("voyagerFeedDashOrganizationalPageUpdates", "updates"),
    ("voyagerFeedDashUpdates", "updates"),
    ("FeedDashUpdates", "updates"),
    ("voyagerSocialDashComments", "comments"),
    ("SocialDashComments", "comments"),
    ("voyagerSocialDashReactions", "reactions"),
    ("SocialDashReactions", "reactions"),
    ("voyagerIdentityDashProfiles", "profile_lookups"),
    ("IdentityDashProfiles", "profile_lookups"),
    ("voyagerJobsDashJobCards", "jobs"),
    ("JobsDashJobCards", "jobs"),
    # Products / photos / targeted content all come back via the
    # OrganizationDashViewWrapper umbrella query.
    ("voyagerOrganizationDashViewWrapper", "products"),
    ("voyagerOrganizationDashProducts", "products"),
    ("OrganizationDashProducts", "products"),
    # Generic fallbacks -- caught last so they don't shadow the specific ones
    ("/voyager/api/graphql", "graphql_other"),
)

# ---------------------------------------------------------------------------
# Sub-tabs to visit when scraping a company. Each entry is (name, path_suffix).
# Path is appended to the canonical company URL.
# ---------------------------------------------------------------------------
TAB_PATHS: tuple[tuple[str, str], ...] = (
    ("home", ""),
    ("about", "about/"),
    ("posts", "posts/"),
    ("jobs", "jobs/"),
    ("products", "products/"),
)
DEFAULT_TABS: tuple[str, ...] = ("home", "about", "posts", "jobs", "products")

# ---------------------------------------------------------------------------
# DOM selectors (stable hooks only)
# ---------------------------------------------------------------------------
# Company overview / header
COMPANY_HEADER_HOOKS = (
    "section[data-test-id='org-top-card']",
    "section.org-top-card",
    "h1[aria-label]",
)

# Feed / update cards
POST_CARD = "div[data-urn^='urn:li:activity:'], article[data-urn^='urn:li:activity:']"

# Comments
COMMENT_TOGGLE = "button[aria-label*='comment' i]"
# Modern markup uses <article class="comments-comment-entity" data-id="urn:li:comment:...">.
# Legacy markup used data-urn / comments-comment-item. We accept both.
COMMENT_CARD = (
    "article.comments-comment-entity, "
    "[data-id^='urn:li:comment:'], "
    "[data-urn^='urn:li:comment:'], "
    "article[class*='comments-comment-item']"
)
COMMENT_LOAD_MORE = (
    # Class-based (most reliable -- works even when LinkedIn omits the text label)
    "button[class*='comments-comments-list__load-more-comments-button'], "
    "button[class*='comments-comments-list__load-more-comments-arrows'], "
    # Text-based fallbacks
    "button:has-text('Load more comments'), "
    "button:has-text('Load previous comments'), "
    "button:has-text('Load more')"
)
# Nested replies under a comment (clicked separately, in its own loop).
COMMENT_REPLY_LOAD_MORE = (
    "button[class*='show-prev-replies'], "
    "button[class*='show-more-replies'], "
    "button:has-text('Show more replies'), "
    "button:has-text('Show previous replies'), "
    "button:has-text('more replies'), "
    "button:has-text('more reply')"
)
# Body text lives in `.comments-comment-item__main-content` (with a nested
# `.update-components-text` and `span[dir='ltr']`). Order: most-specific first.
COMMENT_TEXT = (
    "[data-test-comment-text-view], "
    ".comments-comment-item__main-content, "
    ".feed-shared-main-content--comment, "
    "div.update-components-text, "
    "span[dir='ltr']"
)

# Per-comment author hooks (used in tier order by dom_extractor.resolve_author).
# Comment URN lives in `data-id` on modern markup, `data-urn` on legacy.
COMMENT_URN_ATTRS: tuple[str, ...] = ("data-id", "data-urn")
COMMENT_AUTHOR_URN_ATTR = "data-id"  # back-compat (legacy uses data-urn)
# Catches all three shapes:
#   View <Name>'s profile
#   View <Name>'s graphic link
#   View: <Name>. <Headline>
COMMENT_AUTHOR_ARIA_VIEW = "a[aria-label^='View']"
COMMENT_AUTHOR_PROFILE_LINK = "a[href*='/in/']"
COMMENT_AUTHOR_HIDDEN_SPAN = "span[aria-hidden='true']"

# Post body / metadata
POST_TEXT = "[data-test-id='main-feed-activity-card__commentary'], div.feed-shared-update-v2__description, div.update-components-text"
POST_AUTHOR_LINK = "a[href*='/in/'], a[href*='/company/']"

# Reactions summary on a post card.
# `POST_REACTIONS` is broad -- used for *reading* the count/label, may match a
# non-clickable counter <span>.
# `POST_REACTIONS_BUTTON` is the CLICKABLE button only -- used for opening the
# reactors modal. Only `data-reaction-details` is guaranteed to be a button.
POST_REACTIONS = (
    "button[data-reaction-details], "
    "button[class*='social-details-social-counts__reactions-count'], "
    "[class*='social-details-social-counts__reactions']"
)
POST_REACTIONS_BUTTON = "button[data-reaction-details]"
# Comments count summary button (used for fallback comment-count when API missing).
POST_COMMENTS_COUNT = (
    "button[class*='social-details-social-counts__comments'], "
    "button[aria-label*='comment' i][aria-label*=' on '], "
    "li.social-details-social-counts__comments"
)

# ---------------------------------------------------------------------------
# Reactors modal (opens when you click the reactions button on a post)
# ---------------------------------------------------------------------------
# The modal renders a wrapper dialog + an inner content div. We accept either.
REACTORS_MODAL = (
    "div[role='dialog'][class*='social-details-reactors-modal'], "
    "div[class*='social-details-reactors-modal__content'], "
    "div[class*='social-details-reactors-modal']"
)
REACTORS_MODAL_SCROLLER = (
    "div[class*='social-details-reactors-modal'] div.scaffold-finite-scroll__content, "
    "div[class*='social-details-reactors-modal__content']"
)
REACTOR_ITEM = "li.social-details-reactors-tab-body-list-item"
# Inside each item:
REACTOR_NAME = "span.text-view-model, span[aria-hidden='true'] span"
REACTOR_PROFILE_LINK = "a[href*='/in/']"
REACTOR_HEADLINE = "div.artdeco-entity-lockup__caption"
REACTORS_MODAL_DISMISS = "button[aria-label*='Dismiss' i], button[aria-label='Close' i]"

# "See more" inline expander on post body
POST_SEE_MORE = (
    "button.feed-shared-inline-show-more-text__see-more-less-toggle, "
    "button[class*='inline-show-more-text__see-more'], "
    "button[aria-label*='see more' i], "
    "button:has-text('see more'), "
    "button:has-text('See more')"
)
# "See more" inline expander inside a comment body
COMMENT_SEE_MORE = (
    "button[class*='inline-show-more-text__see-more'], "
    "button[aria-label*='see more' i], "
    "button:has-text('see more'), "
    "button:has-text('See more')"
)

# ---------------------------------------------------------------------------
# Subtraction-fallback chrome phrases (used ONLY when selectors above return empty).
# Substrings of UI chrome text we want stripped out when we're forced to take
# `inner_text()` of a whole card. Lowercase; matched case-insensitively.
# ---------------------------------------------------------------------------
LINKEDIN_CHROME_PHRASES: tuple[str, ...] = (
    "like", "celebrate", "support", "love", "insightful", "funny",
    "comment", "comments", "repost", "send", "share",
    "reply", "replies", "load more", "load previous",
    "see more", "see less", "show more replies", "show previous replies",
    "follow", "following", "connect",
    "•", "·",
)

# ---------------------------------------------------------------------------
# Scroll / scrape limits (overridable from CLI)
# ---------------------------------------------------------------------------
DEFAULT_MAX_POSTS = 10
# Each "Load more comments" click yields ~10 comments. 5 gets us 50/post,
# enough for the long tail without dragging out the run.
DEFAULT_MAX_COMMENT_PAGES = 5

# Safety caps used when the user requests an exhaustive scrape
# (--all-posts / --all-comments / --all-reactors). These are upper bounds;
# the loops stop earlier on plateau (no new content for 2 consecutive
# cycles) or when LinkedIn removes the "Load more" button.
EXHAUSTIVE_POSTS = 5000             # ≈ 5000 posts hard cap per tab
EXHAUSTIVE_COMMENT_PAGES = 200      # ≈ 2000 comments per post hard cap
EXHAUSTIVE_REACTOR_SCROLLS = 500    # ≈ 5000 reactors per post hard cap
SCROLL_STEP_PX = 1500
SCROLL_PLATEAU_CYCLES = 3

# ---------------------------------------------------------------------------
# Env-var names (kept consistent with existing scripts in this repo)
# ---------------------------------------------------------------------------
ENV_USER = ("USER_NAME1", "LINKEDIN_EMAIL")
ENV_PASS = ("PASSWORD1", "LINKEDIN_PASSWORD")
