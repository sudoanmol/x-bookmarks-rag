"""Browser session handling.

One interactive login saves a Playwright storage state. Every later run reuses
it headlessly. Chrome's own cookie database is deliberately not read: it is
Keychain-encrypted, its format shifts between Chrome releases, and a running
Chrome locks the profile.
"""

from __future__ import annotations

import time

from playwright.sync_api import Browser, BrowserContext, Playwright

from . import config

UA_MACOS_CHROME = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)
LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled"]
LOGIN_TIMEOUT_S = 300


class SessionExpired(RuntimeError):
    """Raised when X no longer accepts the saved session."""


def session_exists() -> bool:
    return config.STATE_PATH.is_file()


def _launch(pw: Playwright, headless: bool) -> Browser:
    """Prefer the real Chrome install; fall back to bundled Chromium."""
    try:
        return pw.chromium.launch(channel="chrome", headless=headless, args=LAUNCH_ARGS)
    except Exception:
        return pw.chromium.launch(headless=headless, args=LAUNCH_ARGS)


def _new_context(browser: Browser, *, with_state: bool) -> BrowserContext:
    return browser.new_context(
        storage_state=str(config.STATE_PATH) if with_state else None,
        user_agent=UA_MACOS_CHROME,
        viewport={"width": 1440, "height": 900},
        locale="en-US",
        timezone_id="America/Phoenix",
    )


def save_state(context: BrowserContext) -> None:
    config.ensure_dirs()
    context.storage_state(path=str(config.STATE_PATH))
    config.STATE_PATH.chmod(0o600)


def login(pw: Playwright) -> None:
    """Open a visible browser and wait for the user to sign in."""
    browser = _launch(pw, headless=False)
    context = _new_context(browser, with_state=False)
    page = context.new_page()
    page.goto(config.LOGIN_URL, wait_until="domcontentloaded")

    deadline = time.monotonic() + LOGIN_TIMEOUT_S
    while time.monotonic() < deadline:
        if any(c["name"] == "auth_token" for c in context.cookies()):
            page.goto(config.BOOKMARKS_URL, wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
            save_state(context)
            browser.close()
            return
        page.wait_for_timeout(1000)

    browser.close()
    raise TimeoutError(f"No login detected within {LOGIN_TIMEOUT_S} seconds.")


def open_session(pw: Playwright, headless: bool = True) -> tuple[Browser, BrowserContext]:
    """Open a browser using the saved session."""
    if not session_exists():
        raise SessionExpired("No saved session. Run `xbm login` first.")
    browser = _launch(pw, headless=headless)
    return browser, _new_context(browser, with_state=True)


def assert_logged_in(page) -> None:
    """X redirects signed-out visitors to the login flow. Fail loudly rather
    than capturing zero bookmarks and calling it success."""
    url = page.url
    if "/login" in url or "/i/flow/login" in url or "/account/access" in url:
        raise SessionExpired("The saved session expired. Run `xbm login` again.")
