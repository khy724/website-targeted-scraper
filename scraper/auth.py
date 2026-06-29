"""Auth-wall detection + recovery.

Three layers, called in order by `handle_auth_wall`:
    1. `auto_login`         -- credentials from .env (atomic-fill pattern)
    2. manual ENTER pause   -- MFA / captcha / email-pin / any unknown challenge
    3. final re-check       -- if still walled, raise
"""
from __future__ import annotations

import os
import random
import re
import time

from dotenv import load_dotenv
from playwright.sync_api import Page, TimeoutError as PWTimeout

from . import config

load_dotenv()


def _first_env(names: tuple[str, ...]) -> str | None:
    for n in names:
        v = os.getenv(n)
        if v:
            return v
    return None


def profile_seems_authenticated() -> bool:
    """Best-effort check: does the persistent Chromium profile look logged-in?

    Inspects the profile's Cookies SQLite for a `li_at` row scoped to
    .linkedin.com. We do NOT decrypt the value (Chromium uses the OS
    keychain for that and it's not worth the dependency), so this only
    detects 'cookie exists and is not yet expired'. If LinkedIn rotated
    the session server-side, the scraper will discover that on first
    navigation and the regular auth-wall recovery kicks in.

    Returns False on any error -> caller should run the login flow.
    """
    import sqlite3

    cookies_db = config.USER_DATA_DIR / "Default" / "Cookies"
    if not cookies_db.exists():
        return False
    try:
        # immutable=1 + read-only avoids locking the file while Chromium
        # might still hold it open from a prior crashed run.
        uri = f"file:{cookies_db}?mode=ro&immutable=1"
        conn = sqlite3.connect(uri, uri=True, timeout=1.0)
        try:
            cur = conn.execute(
                "SELECT expires_utc FROM cookies "
                "WHERE name = 'li_at' AND host_key LIKE '%linkedin.com' "
                "LIMIT 1"
            )
            row = cur.fetchone()
        finally:
            conn.close()
        if row is None:
            return False
        expires_utc = row[0]
        # Chromium stores expires_utc as microseconds since 1601-01-01 UTC.
        # 0 means session cookie -> treat as 'present, trust it'.
        if expires_utc == 0:
            return True
        # Convert to unix epoch: subtract microseconds between 1601 and 1970.
        WINDOWS_EPOCH_OFFSET_US = 11_644_473_600_000_000
        unix_expiry = (expires_utc - WINDOWS_EPOCH_OFFSET_US) / 1_000_000
        return unix_expiry > time.time()
    except Exception:
        return False


def is_auth_wall(page: Page) -> bool:
    """True if the current page looks like login / authwall / checkpoint."""
    url = (page.url or "").lower()
    if any(frag in url for frag in config.AUTH_URL_FRAGMENTS):
        return True
    for sel in config.AUTH_DOM_HOOKS:
        try:
            loc = page.locator(sel).first
            if loc.count() == 0:
                continue
            # Some auth-wall markers (e.g. the contextual sign-in modal's input
            # fields) sit inside a hidden form; the modal container itself is
            # visible. count() catches the container case, is_visible() filters
            # out stale/off-screen leftovers.
            if loc.is_visible(timeout=500):
                return True
        except (PWTimeout, Exception):
            continue
    return False


def _looks_like_mfa(page: Page) -> bool:
    """True only if a clear MFA/captcha signal is present AND we're not on the
    plain /login page. Without the URL guard we get false positives from
    LinkedIn's footer wording.
    """
    url = (page.url or "").lower()
    # If we're on the standard login route, treat it as auto-loginable.
    if "/login" in url and "checkpoint" not in url:
        return False
    try:
        body_text = page.locator("body").inner_text(timeout=1000).lower()
    except Exception:
        return False
    return any(re.search(pat, body_text) for pat in config.MFA_TEXT_PATTERNS)


def _find_first_visible(page: Page, selectors: tuple[str, ...], timeout_ms: int):
    """Return the first locator (across `selectors`) that becomes visible within
    `timeout_ms`. Returns None if none appear in time.
    """
    deadline = time.time() + (timeout_ms / 1000.0)
    while time.time() < deadline:
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if loc.count() > 0 and loc.is_visible(timeout=200):
                    return loc
            except Exception:
                continue
        time.sleep(0.25)
    return None


# Permissive selector sets. Order: most-specific first.
# Sign-in fields use autocomplete='current-password' / 'username'; the
# /authwall page's *join* form uses 'new-password' and a different id, so
# the autocomplete-first ordering prevents us from filling the wrong form.
_EMAIL_SELECTORS: tuple[str, ...] = (
    # New (2026) login page uses autocomplete="username" OR "username webauthn";
    # ~= matches whitespace-separated tokens so both render the same hit.
    "input[autocomplete~='username']:visible",
    "input[autocomplete='username']:visible",
    "input[name='session_key']:visible",
    "input#csm-v2_session_key:visible",
    "input#username:visible",
    "input[type='email'][autocomplete*='username']:visible",
    "input[type='email']:visible",
)
_PASSWORD_SELECTORS: tuple[str, ...] = (
    "input[autocomplete='current-password']:visible",
    "input[name='session_password']:visible",
    "input#csm-v2_session_password:visible",
    "input#password:visible",
    "input[type='password']:visible",
)
# WARNING: do NOT add a plain text-contains selector for "Sign in" here --
# the page now has "Sign in with Microsoft", "Sign in with Apple", and
# "Sign in with Google" buttons whose text also contains "Sign in". The
# real submit is matched by exact text via Playwright role/text engines in
# auto_login() below; the selectors here are legacy fallbacks for the old
# /uas/login layout.
_SIGN_IN_SELECTORS: tuple[str, ...] = (
    "button[type='submit'][aria-label*='sign in' i]",
    "button[data-litms-control-urn*='login-submit']",
    "button[data-id='sign-in-form__submit-btn']",
    "button.btn__primary--large[type='submit']",
    "button[type='submit']:text-is('Sign in')",
)


def _reveal_contextual_form(page: Page) -> bool:
    """On guest /company pages LinkedIn opens a modal whose login form is
    CSS-hidden behind a 'Sign in with Email' CTA. Click it so auto_login can
    see the inputs. Returns True if a CTA was clicked.
    """
    for sel in config.AUTH_REVEAL_BUTTONS:
        try:
            btn = page.locator(sel).first
            if btn.count() == 0 or not btn.is_visible(timeout=300):
                continue
            print(f"[auth] revealing contextual login form via {sel}")
            btn.click(timeout=2000)
            time.sleep(config.SHORT_WAIT_S)
            return True
        except Exception:
            continue
    return False


def auto_login(page: Page) -> bool:
    """Fill credentials from .env. Returns False if creds missing or fields not found.
    Handles both the full email+password form and password-only "Welcome back" form.
    """
    user = _first_env(config.ENV_USER)
    pwd = _first_env(config.ENV_PASS)
    if not user or not pwd:
        print("[auth] No credentials in .env (USER_NAME1/PASSWORD1 or LINKEDIN_EMAIL/PASSWORD); skipping auto-login.")
        return False

    print(f"[auth] auto_login: starting at {page.url}")
    try:
        # If we're inside the contextual sign-in modal, the form is hidden
        # until 'Sign in with Email' is clicked. Reveal it first.
        _reveal_contextual_form(page)

        # A small initial scroll + pause makes the session look less robotic
        # to LinkedIn's bot-risk model. Cheap, harmless if it no-ops.
        try:
            page.mouse.move(random.randint(200, 600), random.randint(150, 400))
            page.evaluate("window.scrollBy(0, 60);")
        except Exception:
            pass
        time.sleep(random.uniform(0.6, 1.2))

        # Password is mandatory; email is best-effort (LinkedIn may pre-fill it).
        # The 30s timeout matches login.py — login forms occasionally re-render
        # after a brief loader, and 15s isn't always enough.
        password_input = _find_first_visible(page, _PASSWORD_SELECTORS, 30_000)
        if password_input is None:
            print("[auth] auto_login: no password field visible -- aborting.")
            return False

        email_input = _find_first_visible(page, _EMAIL_SELECTORS, 3_000)
        if email_input is not None:
            print("[auth] auto_login: filling email.")
            try:
                email_input.wait_for(state="visible", timeout=30_000)
                email_input.hover()
                time.sleep(random.uniform(0.15, 0.35))
                email_input.click()
                email_input.fill(user)
                email_input.dispatch_event("input")
                email_input.dispatch_event("change")
            except Exception as e:
                print(f"[auth] auto_login: email fill failed: {e}")
            time.sleep(random.uniform(0.8, 1.5))
        else:
            print("[auth] auto_login: no email field (password-only form). Submitting password only.")

        print("[auth] auto_login: filling password.")
        try:
            password_input.wait_for(state="visible", timeout=30_000)
            password_input.hover()
            time.sleep(random.uniform(0.15, 0.35))
            password_input.click()
            password_input.fill(pwd)
            password_input.dispatch_event("input")
            password_input.dispatch_event("change")
        except Exception as e:
            print(f"[auth] auto_login: password fill failed: {e}")
            return False
        time.sleep(random.uniform(0.7, 1.4))

        # Find sign-in button. Prefer exact-text/role matchers because the
        # 2026 login page renders "Sign in with Microsoft/Apple/Google"
        # buttons in the same DOM whose text also contains "Sign in" -- a
        # loose regex match would click the wrong one.
        sign_in = None
        # 1) exact accessible-name match -- skips all SSO buttons.
        try:
            candidates = page.get_by_role("button", name="Sign in", exact=True)
            count = candidates.count()
            for i in range(count):
                cand = candidates.nth(i)
                if cand.is_visible(timeout=300):
                    sign_in = cand
                    break
        except Exception:
            pass
        # 2) Playwright text-is engine on a <button> with a <span> child.
        if sign_in is None:
            try:
                loc = page.locator("button:has(span:text-is('Sign in'))").first
                if loc.count() > 0 and loc.is_visible(timeout=300):
                    sign_in = loc
            except Exception:
                pass
        # 3) legacy /uas/login selectors (old login layout).
        if sign_in is None:
            for sel in _SIGN_IN_SELECTORS:
                try:
                    loc = page.locator(sel).first
                    if loc.count() > 0 and loc.is_visible(timeout=300):
                        sign_in = loc
                        break
                except Exception:
                    continue

        if sign_in is None:
            print("[auth] auto_login: pressing Enter on password field (no submit button found).")
            password_input.press("Enter")
        else:
            print("[auth] auto_login: clicking Sign in.")
            try:
                sign_in.wait_for(state="visible", timeout=30_000)
                sign_in.hover()
                time.sleep(random.uniform(0.1, 0.3))
                sign_in.click()
            except Exception:
                password_input.press("Enter")

        # Wait for navigation away from login -- but don't fail if it just spins
        try:
            page.wait_for_url(re.compile(r"linkedin\.com/(?!.*(login|checkpoint|authwall))"), timeout=20_000)
            print(f"[auth] auto_login: success, now at {page.url}")
        except PWTimeout:
            print(f"[auth] auto_login: still at {page.url} after submit (may need MFA).")
        return True
    except Exception as e:
        print(f"[auth] auto_login encountered: {e}")
        return False


def _manual_pause(page: Page, reason: str) -> None:
    """Wait for the user to resolve the auth challenge.

    Polls the page state instead of blocking on stdin so the script can
    self-terminate when the user closes the tab (otherwise the process
    hangs on `input()` forever). Exit conditions:
        - tab closed             -> raise RuntimeError
        - navigated past the wall -> return
        - 5 minute hard timeout  -> raise RuntimeError
        - ENTER pressed in stdin  -> return (kept for muscle memory)

    PRODUCTION: this path requires a human in front of a visible browser.
    Before running in headless/CI/server contexts, replace this body with
    a notification hook (e.g. send the MFA challenge URL + a screenshot
    to Slack/PagerDuty/email and wait for an out-of-band code, or fail
    fast with a known sentinel exit code so a supervisor can re-queue
    the job). Detection of MFA vs. plain login is via the `reason` arg.
    """
    import sys
    import threading

    is_mfa = "mfa" in reason.lower() or "captcha" in reason.lower() or "verification" in reason.lower()

    banner = "!" * 78 if is_mfa else "=" * 78
    print("\n" + banner)
    if is_mfa:
        print("[auth] !!! MANUAL ACTION REQUIRED: MFA / CAPTCHA / VERIFICATION CODE !!!")
        print("[auth] LinkedIn is asking for a code or challenge that auto_login cannot solve.")
    else:
        print(f"[auth] Manual action required: {reason}")
    print(f"[auth] Current URL: {page.url}")
    print("[auth] Complete the challenge in the browser window (or close the tab to abort).")
    print("[auth] You may also press ENTER here once done.")
    print(banner)

    enter_pressed = threading.Event()

    def _wait_for_enter() -> None:
        try:
            sys.stdin.readline()
            enter_pressed.set()
        except Exception:
            pass

    t = threading.Thread(target=_wait_for_enter, daemon=True)
    t.start()

    deadline = time.time() + 300  # 5 min cap
    while time.time() < deadline:
        if enter_pressed.is_set():
            break
        if page.is_closed():
            raise RuntimeError("User closed the browser tab during manual auth resume; aborting.")
        try:
            url = (page.url or "").lower()
        except Exception:
            raise RuntimeError("Browser tab no longer reachable during manual auth resume; aborting.")
        if not any(frag in url for frag in config.AUTH_URL_FRAGMENTS) and not is_auth_wall(page):
            print(f"[auth] detected navigation away from auth wall ({page.url}); resuming.")
            break
        time.sleep(1.0)
    else:
        raise RuntimeError("Manual auth resume timed out after 5 minutes; aborting.")

    print("=" * 78 + "\n")


def handle_auth_wall(page: Page, return_to: str | None = None) -> None:
    """If the page is walled, try auto-login then fall back to manual ENTER pause.

    `return_to` is the URL we wanted; we navigate back to it after recovery.
    """
    if not is_auth_wall(page) and not _looks_like_mfa(page):
        return

    print(f"[auth] Auth wall detected at {page.url}")

    # --- Layer 1: auto-login. We attempt it whenever we're on a login-ish URL.
    # _looks_like_mfa is intentionally URL-guarded above so it does NOT short-
    # circuit us when we're actually on /login.
    if not _looks_like_mfa(page):
        ok = auto_login(page)
        # Give navigation a moment to settle.
        time.sleep(config.MEDIUM_WAIT_S)
        if ok and not is_auth_wall(page) and not _looks_like_mfa(page):
            print(f"[auth] auto-login cleared the wall, now at {page.url}")
            if return_to and return_to not in (page.url or ""):
                try:
                    page.goto(return_to, wait_until="domcontentloaded")
                except Exception as e:
                    print(f"[auth] post-recovery goto failed: {e}")
            return

    # --- Layer 2: manual fallback for MFA / captcha / failed auto-login ---
    if is_auth_wall(page) or _looks_like_mfa(page):
        if _looks_like_mfa(page):
            reason = "MFA / captcha / verification code"
        else:
            reason = "login still required (auto-login failed or credentials missing)"
        _manual_pause(page, reason)

    # --- Layer 3: re-verify; if still walled, raise so the caller can stop ---
    if is_auth_wall(page):
        raise RuntimeError(f"Auth wall persists after manual resume (url={page.url}).")

    # If LinkedIn redirected us, send the browser back where we wanted to be.
    if return_to and return_to not in (page.url or ""):
        try:
            page.goto(return_to, wait_until="domcontentloaded")
        except Exception as e:
            print(f"[auth] post-recovery goto failed: {e}")
