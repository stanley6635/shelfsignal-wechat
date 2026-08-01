from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from enum import StrEnum
from pathlib import Path

from playwright.async_api import BrowserContext, async_playwright
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from .errors import AuthRequired, ShelfUnavailable

SHELF_URL = "https://weread.qq.com/web/shelf"
AUTHORIZATION_TIMEOUT_MS = 180_000


class AuthPolicy(StrEnum):
    FRESH = "fresh"
    REUSE = "reuse"


def prepare_profile(browser_root: Path, run_id: str, policy: AuthPolicy) -> Path:
    profile = (
        browser_root / "runs" / run_id
        if policy is AuthPolicy.FRESH
        else browser_root / "persistent"
    )
    profile.mkdir(parents=True, exist_ok=True)
    return profile


def is_auth_required(url: str, status: int) -> bool:
    return "/login" in url or status in {401, 403}


def classify_shelf_probe(status: int) -> None:
    if status >= 500:
        raise ShelfUnavailable(f"WeRead shelf preflight returned HTTP {status}")


@asynccontextmanager
async def authenticated_context(
    browser_root: Path,
    run_id: str,
    policy: AuthPolicy,
) -> AsyncIterator[BrowserContext]:
    profile = prepare_profile(browser_root, run_id, policy)
    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=profile,
            headless=False,
        )
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            response = await page.goto(SHELF_URL, wait_until="domcontentloaded")
            status = 0 if response is None else response.status
            if is_auth_required(page.url, status):
                try:
                    await page.wait_for_url(
                        "**/web/shelf**",
                        timeout=AUTHORIZATION_TIMEOUT_MS,
                    )
                except PlaywrightTimeoutError as exc:
                    raise AuthRequired("WeRead QR authorization timed out") from exc
            classify_shelf_probe(status)
            yield context
        finally:
            await context.close()
