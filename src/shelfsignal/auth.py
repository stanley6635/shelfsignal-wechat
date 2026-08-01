from __future__ import annotations

import asyncio
import os
import re
import stat
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import BrowserContext, async_playwright
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from .errors import AuthRequired, ShelfUnavailable

SHELF_URL = "https://weread.qq.com/web/shelf"
AUTHORIZATION_TIMEOUT_MS = 180_000
AUTH_POLL_INTERVAL_MS = 1_000
_SAFE_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


class AuthPolicy(StrEnum):
    FRESH = "fresh"
    REUSE = "reuse"


def prepare_profile(browser_root: Path, run_id: str, policy: AuthPolicy | str) -> Path:
    try:
        policy = AuthPolicy(policy)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"unsupported authentication policy: {policy}") from exc
    if _SAFE_RUN_ID.fullmatch(run_id) is None:
        raise ValueError("run ID must be a safe single path component")

    browser_root = _safe_absolute_browser_root(browser_root)
    names = ("runs", run_id) if policy == AuthPolicy.FRESH else ("persistent",)
    profile = browser_root.joinpath(*names)
    if profile == browser_root or browser_root not in profile.parents:
        raise ValueError("browser profile escapes the browser root")

    descriptors: list[int] = []
    root_fd = _open_private_root(browser_root)
    descriptors.append(root_fd)
    parent_fd = root_fd
    try:
        for index, name in enumerate(names):
            display_path = browser_root.joinpath(*names[: index + 1])
            descriptor = _open_private_directory_at(parent_fd, name, display_path)
            descriptors.append(descriptor)
            _verify_opened_directory(parent_fd, name, descriptor, display_path)
            parent_fd = descriptor
        _verify_root_identity(browser_root, root_fd)
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
    return profile


def _safe_absolute_browser_root(path: Path) -> Path:
    try:
        expanded = path.expanduser()
    except RuntimeError as exc:
        raise ValueError("browser root must be an unambiguous absolute path") from exc
    if not expanded.is_absolute():
        raise ValueError("browser root must be an absolute path")
    if ".." in expanded.parts:
        raise ValueError("browser root contains ambiguous parent traversal")
    normalized = Path(os.path.normpath(os.fspath(expanded)))
    if normalized != expanded or normalized == Path(normalized.anchor):
        raise ValueError("browser root must be an unambiguous absolute path")
    return normalized


def _open_private_root(path: Path) -> int:
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        raise ValueError(f"browser profile directory is unsafe: {path}") from exc
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError(f"browser profile directory is unsafe: {path}") from exc
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise ValueError(f"browser profile path is not a directory: {path}")
        os.fchmod(descriptor, 0o700)
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def _open_private_directory_at(parent_fd: int, name: str, display_path: Path) -> int:
    try:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
    except FileExistsError:
        pass
    except OSError as exc:
        raise ValueError(f"browser profile directory is unsafe: {display_path}") from exc
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        raise ValueError(f"browser profile directory is unsafe: {display_path}") from exc
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise ValueError(f"browser profile path is not a directory: {display_path}")
        os.fchmod(descriptor, 0o700)
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def _verify_opened_directory(
    parent_fd: int, name: str, descriptor: int, display_path: Path
) -> None:
    try:
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        opened = os.fstat(descriptor)
    except OSError as exc:
        raise ValueError(f"browser profile path changed during setup: {display_path}") from exc
    if (
        not stat.S_ISDIR(current.st_mode)
        or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
    ):
        raise ValueError(f"browser profile directory is unsafe: {display_path}")


def _verify_root_identity(path: Path, descriptor: int) -> None:
    try:
        current = os.stat(path, follow_symlinks=False)
        opened = os.fstat(descriptor)
    except OSError as exc:
        raise ValueError(f"browser profile path changed during setup: {path}") from exc
    if (
        not stat.S_ISDIR(current.st_mode)
        or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
    ):
        raise ValueError(f"browser profile directory is unsafe: {path}")


def is_auth_required(url: str, status: int) -> bool:
    path = _trusted_probe_path(url)
    if status >= 400 and status not in {401, 403}:
        classify_shelf_probe(status)
    return path == "/web/login" or status in {401, 403}


def _trusted_probe_path(url: str) -> str:
    parsed = urlsplit(url)
    path = parsed.path.rstrip("/")
    if (
        parsed.scheme != "https"
        or parsed.netloc != "weread.qq.com"
        or path not in {"/web/login", "/web/shelf"}
    ):
        raise ShelfUnavailable("WeRead shelf returned an unexpected URL origin or path")
    return path


def classify_shelf_probe(status: int) -> None:
    if status >= 400:
        raise ShelfUnavailable(f"WeRead shelf preflight returned HTTP {status}")


async def _probe_shelf(page):
    try:
        response = await page.goto(SHELF_URL, wait_until="domcontentloaded")
    except PlaywrightError as exc:
        raise ShelfUnavailable("WeRead shelf could not be reached") from exc
    if response is None:
        raise ShelfUnavailable("WeRead shelf returned no HTTP response")
    return page.url, response.status


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
        primary_error: BaseException | None = None
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            url, status = await _probe_shelf(page)
            if is_auth_required(url, status):
                try:
                    async with asyncio.timeout(AUTHORIZATION_TIMEOUT_MS / 1_000):
                        if _trusted_probe_path(url) == "/web/login":
                            try:
                                await page.wait_for_url(
                                    "**/web/shelf**",
                                    timeout=AUTHORIZATION_TIMEOUT_MS,
                                )
                            except PlaywrightTimeoutError as exc:
                                raise AuthRequired(
                                    "WeRead QR authorization timed out"
                                ) from exc
                        while True:
                            url, status = await _probe_shelf(page)
                            if not is_auth_required(url, status):
                                break
                            await page.wait_for_timeout(AUTH_POLL_INTERVAL_MS)
                except TimeoutError as exc:
                    raise AuthRequired("WeRead QR authorization timed out") from exc
            classify_shelf_probe(status)
            yield context
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            try:
                await context.close()
            except BaseException as close_exc:
                if primary_error is not None:
                    primary_error.add_note(
                        f"WeRead browser context also failed to close: {close_exc}"
                    )
                elif isinstance(close_exc, Exception):
                    raise ShelfUnavailable(
                        "WeRead browser context could not be closed"
                    ) from close_exc
                else:
                    raise
