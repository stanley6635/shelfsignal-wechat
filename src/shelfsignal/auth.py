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
    anchor_fd = _open_filesystem_anchor(browser_root.anchor)
    descriptors.append(anchor_fd)
    parent_fd = anchor_fd
    primary_error: BaseException | None = None
    try:
        current_path = Path(browser_root.anchor)
        for name in browser_root.parent.parts[1:]:
            current_path /= name
            descriptor = _open_existing_ancestor_at(parent_fd, name, current_path)
            descriptors.append(descriptor)
            parent_fd = descriptor

        root_fd = _open_private_directory_at(
            parent_fd, browser_root.name, browser_root
        )
        descriptors.append(root_fd)
        _verify_opened_directory(
            parent_fd, browser_root.name, root_fd, browser_root
        )
        parent_fd = root_fd
        for index, name in enumerate(names):
            display_path = browser_root.joinpath(*names[: index + 1])
            descriptor = _open_private_directory_at(parent_fd, name, display_path)
            descriptors.append(descriptor)
            _verify_opened_directory(parent_fd, name, descriptor, display_path)
            parent_fd = descriptor
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        _close_descriptors(descriptors, primary_error)
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


def _directory_open_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


def _open_filesystem_anchor(anchor: str) -> int:
    try:
        descriptor = os.open(anchor, _directory_open_flags())
    except OSError as exc:
        raise ValueError("filesystem anchor is unavailable") from exc
    try:
        is_directory = stat.S_ISDIR(os.fstat(descriptor).st_mode)
    except BaseException as exc:
        _close_opened_descriptor(descriptor, exc)
        raise
    if not is_directory:
        error = ValueError("filesystem anchor is not a directory")
        _close_opened_descriptor(descriptor, error)
        raise error
    return descriptor


def _close_opened_descriptor(descriptor: int, primary_error: BaseException) -> None:
    try:
        os.close(descriptor)
    except BaseException as close_error:  # noqa: BLE001 - ownership boundary
        primary_error.add_note(f"descriptor cleanup also failed: {close_error}")


def _close_descriptors(
    descriptors: list[int], primary_error: BaseException | None
) -> None:
    close_error: BaseException | None = None
    for descriptor in reversed(descriptors):
        try:
            os.close(descriptor)
        except BaseException as exc:  # noqa: BLE001 - every owned fd must be attempted
            if close_error is None:
                close_error = exc
            else:
                close_error.add_note(f"another descriptor also failed to close: {exc}")
    if close_error is None:
        return
    if primary_error is not None:
        primary_error.add_note(f"browser profile descriptor cleanup also failed: {close_error}")
    else:
        raise close_error


def _open_existing_ancestor_at(parent_fd: int, name: str, display_path: Path) -> int:
    try:
        descriptor = os.open(name, _directory_open_flags(), dir_fd=parent_fd)
    except OSError as exc:
        raise ValueError(
            f"browser root ancestor is unsafe or missing: {display_path}"
        ) from exc
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise ValueError(f"browser root ancestor is not a directory: {display_path}")
        _verify_opened_directory(parent_fd, name, descriptor, display_path)
    except BaseException as exc:
        _close_opened_descriptor(descriptor, exc)
        raise
    return descriptor


def _open_private_directory_at(parent_fd: int, name: str, display_path: Path) -> int:
    try:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
    except FileExistsError:
        pass
    except OSError as exc:
        raise ValueError(f"browser profile directory is unsafe: {display_path}") from exc
    try:
        descriptor = os.open(name, _directory_open_flags(), dir_fd=parent_fd)
    except OSError as exc:
        raise ValueError(f"browser profile directory is unsafe: {display_path}") from exc
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise ValueError(f"browser profile path is not a directory: {display_path}")
        os.fchmod(descriptor, 0o700)
    except BaseException as exc:
        _close_opened_descriptor(descriptor, exc)
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


def _handle_cleanup_failure(
    primary_error: BaseException | None,
    cleanup_error: BaseException,
    *,
    note: str,
    standalone_message: str,
) -> None:
    if primary_error is not None:
        primary_error.add_note(f"{note}: {cleanup_error}")
    elif isinstance(cleanup_error, Exception):
        raise ShelfUnavailable(standalone_message) from cleanup_error
    else:
        raise cleanup_error


async def _stop_playwright_manager(manager, primary_error: BaseException | None) -> None:
    try:
        await manager.__aexit__(
            type(primary_error) if primary_error is not None else None,
            primary_error,
            primary_error.__traceback__ if primary_error is not None else None,
        )
    except asyncio.CancelledError as exit_error:
        _handle_cleanup_failure(
            primary_error,
            exit_error,
            note="Playwright manager also failed to stop",
            standalone_message="Playwright manager could not be stopped",
        )
    except Exception as exit_error:  # noqa: BLE001 - cleanup must preserve primary errors
        _handle_cleanup_failure(
            primary_error,
            exit_error,
            note="Playwright manager also failed to stop",
            standalone_message="Playwright manager could not be stopped",
        )


@asynccontextmanager
async def authenticated_context(
    browser_root: Path,
    run_id: str,
    policy: AuthPolicy,
) -> AsyncIterator[BrowserContext]:
    profile = prepare_profile(browser_root, run_id, policy)
    manager = async_playwright()
    try:
        playwright = await manager.__aenter__()
    except BaseException as enter_error:
        if hasattr(manager, "_connection"):
            await _stop_playwright_manager(manager, enter_error)
        if isinstance(enter_error, PlaywrightError):
            raise ShelfUnavailable("Playwright manager could not be started") from enter_error
        raise

    manager_primary_error: BaseException | None = None
    try:
        try:
            context = await playwright.chromium.launch_persistent_context(
                user_data_dir=profile,
                headless=False,
            )
        except PlaywrightError as exc:
            raise ShelfUnavailable("WeRead browser context could not be started") from exc

        context_primary_error: BaseException | None = None
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
            context_primary_error = exc
            raise
        finally:
            try:
                await context.close()
            except asyncio.CancelledError as close_exc:
                _handle_cleanup_failure(
                    context_primary_error,
                    close_exc,
                    note="WeRead browser context also failed to close",
                    standalone_message="WeRead browser context could not be closed",
                )
            except Exception as close_exc:  # noqa: BLE001 - cleanup must preserve primary errors
                _handle_cleanup_failure(
                    context_primary_error,
                    close_exc,
                    note="WeRead browser context also failed to close",
                    standalone_message="WeRead browser context could not be closed",
                )
    except BaseException as exc:
        manager_primary_error = exc
        raise
    finally:
        await _stop_playwright_manager(manager, manager_primary_error)
