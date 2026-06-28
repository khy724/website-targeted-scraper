from camoufox.sync_api import Camoufox
from dotenv import load_dotenv
import os
import time
import random

load_dotenv()
USER_NAME = os.getenv("USER_NAME1")
PASSWORD = os.getenv("PASSWORD1")

if not USER_NAME or not PASSWORD:
    raise SystemExit("Please set USER_NAME1 and PASSWORD1 in your .env file before running this script.")

config = {
    'window.outerHeight': 1056,
    'window.outerWidth': 1920,
    'window.innerHeight': 1008,
    'window.innerWidth': 1920,
    'window.history.length': 4,
    'navigator.userAgent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0',
    'navigator.appCodeName': 'Mozilla',
    'navigator.appName': 'Netscape',
    'navigator.appVersion': '5.0 (Windows)',
    'navigator.oscpu': 'Windows NT 10.0; Win64; x64',
    'navigator.language': 'en-US',
    'navigator.languages': ['en-US'],
    'navigator.platform': 'Win32',
    'navigator.hardwareConcurrency': 12,
    'navigator.product': 'Gecko',
    'navigator.productSub': '20030107',
    'navigator.maxTouchPoints': 10,
}


with Camoufox(
    headless=False,
    persistent_context=True,
    user_data_dir='user-data-dir',
    os='windows',
    config=config,
    i_know_what_im_doing=True,
) as browser:
    page = browser.new_page()
    page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")

    email_input = page.locator("input[type='email'][autocomplete*='username']:visible").first
    password_input = page.locator("input[type='password']:visible, input[autocomplete='current-password']:visible").first
    sign_in_button = page.get_by_role("button", name="Sign in", exact=True).first

    # --- 1. Fill Username via Atomic Fill + Event Fire ---
    email_input.wait_for(state="visible", timeout=30000)
    email_input.hover()
    email_input.click()
    
    # Use .fill() - this forces the browser to set the complete string instantly
    # so LinkedIn cannot cut it off halfway.
    email_input.fill(USER_NAME)
    
    # Instantly fire input events so LinkedIn registers the change as valid
    email_input.dispatch_event("input")
    email_input.dispatch_event("change")

    # Real human pause to simulate reading/moving eyes
    time.sleep(random.uniform(0.8, 1.5))

    # --- 2. Fill Password via Atomic Fill + Event Fire ---
    password_input.wait_for(state="visible", timeout=30000)
    password_input.hover()
    password_input.click()
    
    # Safely fill the password completely
    password_input.fill(PASSWORD)
    password_input.dispatch_event("input")
    password_input.dispatch_event("change")

    # Human pause before reacting to click submit
    time.sleep(random.uniform(0.7, 1.4))

    # --- 3. Click Submit ---
    sign_in_button.wait_for(state="visible", timeout=30000)
    sign_in_button.hover()
    time.sleep(random.uniform(0.1, 0.3)) 
    sign_in_button.click()

    try:
        page.wait_for_url("**/feed", timeout=60000)
        print("Login successful. Reached account feed.")
    except Exception:
        print("Login submitted. Please check for additional verification or a failed login.")
        print("Current URL:", page.url)

    page.wait_for_timeout(10000)
    page.close()