"""Fixtures for the Playwright journeys: a real server per test, one headless Chromium per session.

`app` is the fixture journeys use: an `App` (see `tests/e2e/pages.py`) bound to a fresh live
server with its own throwaway data directory and a fresh browser context. Any JavaScript error,
uncaught page exception or error-level console message fails the test at teardown unless it was
declared with `app.console.allow(...)`. The browser comes from the project-local
`.playwright-browsers` (set by `tests/conftest.py`); tests skip when it is not installed.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.e2e.pages import App, ConsoleCollector
from tests.support.live_server import run_live_server

VIEWPORT = {"width": 1300, "height": 1000}
DEFAULT_TIMEOUT_MS = 20_000


def _browsers_installed() -> bool:
    browsers = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", ""))
    return browsers.is_dir() and any(browsers.glob("chromium*"))


@pytest.fixture(scope="session")
def _browser() -> Iterator[object]:
    """One headless Chromium shared by every test of the session."""
    if not _browsers_installed():
        pytest.skip("Playwright Chromium is not installed (run scripts/bootstrap.sh)")
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            yield browser
        finally:
            browser.close()


@pytest.fixture
def server() -> Iterator[object]:
    """A real `python -m rga` server on a free loopback port with a throwaway data directory."""
    with run_live_server() as live:
        yield live


@pytest.fixture
def app(server, _browser, tmp_path) -> Iterator[App]:
    """An `App` on a fresh context; fails the test if the page logged a JavaScript error."""
    context = _browser.new_context(viewport=VIEWPORT)
    context.set_default_timeout(DEFAULT_TIMEOUT_MS)
    page = context.new_page()
    console = ConsoleCollector(page)
    application = App(page=page, context=context, server=server, tmp_path=tmp_path, console=console)
    try:
        yield application
    finally:
        context.close()
    console.assert_clean()
