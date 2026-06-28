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


def is_auth_wall(page: Page) -> bool:
    """True if the current page looks like login / authwall / checkpoint."""
    url = (page.url or "").lower()
    if any(frag in url for frag in config.AUTH_URL_FRAGMENTS):
        return True
    for sel in config.AUTH_DOM_HOOKS:
        try:
            if page.locator(sel).first.is_visible(timeout=500):
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
_EMAIL_SELECTORS: tuple[str, ...] = (
    "input#username:visible",
    "input[name='session_key']:visible",
    "input[type='email'][autocomplete*='username']:visible",
    "input[autocomplete='username']:visible",
    "input[type='email']:visible",
)
_PASSWORD_SELECTORS: tuple[str, ...] = (
    "input#password:visible",
    "input[name='session_password']:visible",
    "input[autocomplete='current-password']:visible",
    "input[type='password']:visible",
)
_SIGN_IN_SELECTORS: tuple[str, ...] = (
    "button[type='submit'][aria-label*='sign in' i]",
    "button[data-litms-control-urn*='login-submit']",
    "button.btn__primary--large[type='submit']",
    "button[type='submit']:has-text('Sign in')",
)


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
        # Password is mandatory; email is best-effort (LinkedIn may pre-fill it).
        password_input = _find_first_visible(page, _PASSWORD_SELECTORS, config.SELECTOR_TIMEOUT_MS)
        if password_input is None:
            print("[auth] auto_login: no password field visible -- aborting.")
            return False

        email_input = _find_first_visible(page, _EMAIL_SELECTORS, 1500)
        if email_input is not None:
            print("[auth] auto_login: filling email.")
            try:
                email_input.click()
                email_input.fill(user)
                email_input.dispatch_event("input")
                email_input.dispatch_event("change")
            except Exception as e:
                print(f"[auth] auto_login: email fill failed: {e}")
            time.sleep(random.uniform(0.6, 1.2))
        else:
            print("[auth] auto_login: no email field (password-only form). Submitting password only.")

        print("[auth] auto_login: filling password.")
        password_input.click()
        password_input.fill(pwd)
        password_input.dispatch_event("input")
        password_input.dispatch_event("change")
        time.sleep(random.uniform(0.5, 1.0))

        # Find sign-in button. Permissive list + role/text fallback.
        sign_in = None
        for sel in _SIGN_IN_SELECTORS:
            try:
                loc = page.locator(sel).first
                if loc.count() > 0 and loc.is_visible(timeout=300):
                    sign_in = loc
                    break
            except Exception:
                continue
        if sign_in is None:
            try:
                sign_in = page.get_by_role("button", name=re.compile(r"sign in", re.I)).first
            except Exception:
                sign_in = None

        if sign_in is None:
            print("[auth] auto_login: pressing Enter on password field (no submit button found).")
            password_input.press("Enter")
        else:
            print("[auth] auto_login: clicking Sign in.")
            try:
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
    banner = "\n" + "=" * 70
    print(banner)
    print(f"[auth] Manual action required: {reason}")
    print(f"[auth] Current URL: {page.url}")
    print("[auth] Complete the challenge in the browser window, then return here.")
    input("[auth] Press ENTER to resume scraping... ")
    print("=" * 70 + "\n")


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
        reason = "MFA / captcha / extra verification" if _looks_like_mfa(page) else "login still required"
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
