import asyncio
import stat
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import shelfsignal.auth as auth_module
from shelfsignal.auth import (
    AuthPolicy,
    authenticated_context,
    classify_shelf_probe,
    is_auth_required,
    prepare_profile,
)
from shelfsignal.errors import AuthRequired, ShelfUnavailable


class FakePlaywrightManager:
    def __init__(self, playwright):
        self.playwright = playwright
        self.enter = AsyncMock(return_value=playwright)
        self.exit = AsyncMock(return_value=False)
        self.create_connection_on_enter = True

    async def __aenter__(self):
        if self.create_connection_on_enter:
            self._connection = object()
        return await self.enter()

    async def __aexit__(self, exc_type, exc, traceback):
        return await self.exit(exc_type, exc, traceback)


def install_mock_playwright(monkeypatch, page):
    context = SimpleNamespace(
        pages=[page],
        new_page=AsyncMock(),
        close=AsyncMock(),
    )
    launch = AsyncMock(return_value=context)
    playwright = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch))
    manager = FakePlaywrightManager(playwright)
    context.playwright_manager = manager
    monkeypatch.setattr(
        auth_module,
        "async_playwright",
        lambda: manager,
    )
    return context, launch


def test_fresh_policy_uses_run_scoped_profile_without_deleting_prior_profiles(tmp_path: Path):
    browser_root = tmp_path / "browser"
    prior = browser_root / "runs" / "run-000"
    prior.mkdir(parents=True)
    marker = prior / "marker"
    marker.write_text("keep", encoding="utf-8")

    path = prepare_profile(browser_root, "run-001", AuthPolicy.FRESH)

    assert path == browser_root / "runs" / "run-001"
    assert path.is_dir()
    assert marker.read_text(encoding="utf-8") == "keep"


def test_reuse_policy_uses_persistent_profile(tmp_path: Path):
    path = prepare_profile(tmp_path / "browser", "run-001", AuthPolicy.REUSE)

    assert path == tmp_path / "browser" / "persistent"
    assert path.is_dir()


def test_string_fresh_policy_is_normalized_before_routing(tmp_path: Path):
    path = prepare_profile(tmp_path / "browser", "run-001", "fresh")

    assert path == tmp_path / "browser" / "runs" / "run-001"


def test_prepare_profile_rejects_relative_or_parent_ambiguous_browser_root(
    tmp_path: Path,
):
    base = tmp_path / "base"

    with pytest.raises(ValueError, match="absolute"):
        prepare_profile(Path("relative-browser"), "run-001", AuthPolicy.FRESH)
    with pytest.raises(ValueError, match="ambiguous"):
        prepare_profile(base / "child" / "..", "run-001", AuthPolicy.FRESH)

    assert not base.exists()


def test_prepare_profile_rejects_missing_ancestor_without_creating_it(tmp_path: Path):
    missing = tmp_path / "missing"

    with pytest.raises(ValueError, match="ancestor"):
        prepare_profile(missing / "browser", "run-001", AuthPolicy.FRESH)

    assert not missing.exists()


def test_prepare_profile_creates_only_browser_leaf_and_preserves_ancestor_mode(
    tmp_path: Path,
):
    parent = tmp_path / "existing-parent"
    parent.mkdir(mode=0o755)
    parent.chmod(0o755)

    profile = prepare_profile(parent / "browser", "run-001", AuthPolicy.FRESH)

    assert profile.is_dir()
    assert stat.S_IMODE(parent.stat().st_mode) == 0o755
    assert stat.S_IMODE((parent / "browser").stat().st_mode) == 0o700


@pytest.mark.parametrize("depth", [1, 2])
def test_prepare_profile_rejects_symlink_at_any_ancestor_depth(tmp_path: Path, depth: int):
    outside = tmp_path / "outside"
    outside.mkdir()
    if depth == 1:
        linked = tmp_path / "linked"
        linked.symlink_to(outside, target_is_directory=True)
        browser_root = linked / "browser"
    else:
        container = tmp_path / "container"
        container.mkdir()
        linked = container / "linked"
        linked.symlink_to(outside, target_is_directory=True)
        browser_root = linked / "nested" / "browser"

    with pytest.raises(ValueError, match="ancestor"):
        prepare_profile(browser_root, "run-001", AuthPolicy.FRESH)

    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("run_id", ["../escape", "/tmp/escape", "nested/run", ".", "run 001"])
def test_prepare_profile_rejects_unsafe_run_id(tmp_path: Path, run_id: str):
    with pytest.raises(ValueError, match="run ID"):
        prepare_profile(tmp_path / "browser", run_id, AuthPolicy.FRESH)


@pytest.mark.parametrize("target", ["root", "runs", "profile", "persistent"])
def test_prepare_profile_rejects_static_directory_symlinks(tmp_path: Path, target: str):
    outside = tmp_path / "outside"
    outside.mkdir()
    browser_root = tmp_path / "browser"
    if target == "root":
        browser_root.symlink_to(outside, target_is_directory=True)
        policy = AuthPolicy.FRESH
    else:
        browser_root.mkdir()
        if target == "runs":
            (browser_root / "runs").symlink_to(outside, target_is_directory=True)
            policy = AuthPolicy.FRESH
        elif target == "profile":
            (browser_root / "runs").mkdir()
            (browser_root / "runs" / "run-001").symlink_to(
                outside, target_is_directory=True
            )
            policy = AuthPolicy.FRESH
        else:
            (browser_root / "persistent").symlink_to(outside, target_is_directory=True)
            policy = AuthPolicy.REUSE

    with pytest.raises(ValueError, match="unsafe"):
        prepare_profile(browser_root, "run-001", policy)

    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("policy", [AuthPolicy.FRESH, AuthPolicy.REUSE])
def test_prepare_profile_tightens_all_managed_directories_to_0700(
    tmp_path: Path, policy: AuthPolicy
):
    browser_root = tmp_path / "browser"
    profile = prepare_profile(browser_root, "run-001", policy)
    managed = []
    for directory in (profile, *profile.parents):
        if directory == tmp_path:
            break
        managed.append(directory)
        directory.chmod(0o755)

    assert prepare_profile(browser_root, "run-001", policy) == profile

    for directory in managed:
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700


def test_login_redirect_or_auth_status_is_auth_required():
    assert is_auth_required("https://weread.qq.com/web/login", 200)
    assert is_auth_required("https://weread.qq.com/web/shelf", 401)
    assert is_auth_required("https://weread.qq.com/web/shelf", 403)
    assert not is_auth_required("https://weread.qq.com/web/shelf", 200)
    assert not is_auth_required("https://weread.qq.com/web/shelf/?tab=saved", 200)
    assert is_auth_required("https://weread.qq.com/web/login/?next=shelf", 200)


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/web/shelf",
        "http://weread.qq.com/web/shelf",
        "https://weread.qq.com.evil.example/web/shelf",
        "https://weread.qq.com/web/other",
    ],
)
def test_probe_classification_rejects_untrusted_origin_or_wrong_path(url: str):
    with pytest.raises(ShelfUnavailable, match="unexpected URL"):
        is_auth_required(url, 200)


def test_server_failure_is_shelf_unavailable():
    with pytest.raises(ShelfUnavailable, match="HTTP 503"):
        classify_shelf_probe(503)
    with pytest.raises(ShelfUnavailable, match="HTTP 503"):
        is_auth_required("https://weread.qq.com/web/login", 503)

    classify_shelf_probe(200)


@pytest.mark.asyncio
async def test_authenticated_context_uses_bounded_persistent_context_and_closes(tmp_path, monkeypatch):
    page = SimpleNamespace(
        url=auth_module.SHELF_URL,
        goto=AsyncMock(return_value=SimpleNamespace(status=200)),
        wait_for_url=AsyncMock(),
        wait_for_timeout=AsyncMock(),
    )
    context, launch = install_mock_playwright(monkeypatch, page)

    async with authenticated_context(tmp_path / "browser", "run-001", AuthPolicy.FRESH) as value:
        assert value is context

    launch.assert_awaited_once_with(
        user_data_dir=tmp_path / "browser" / "runs" / "run-001",
        headless=False,
    )
    page.goto.assert_awaited_once_with(auth_module.SHELF_URL, wait_until="domcontentloaded")
    page.wait_for_url.assert_not_awaited()
    context.close.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_authenticated_context_closes_when_consumer_fails(tmp_path, monkeypatch):
    page = SimpleNamespace(
        url=auth_module.SHELF_URL,
        goto=AsyncMock(return_value=SimpleNamespace(status=200)),
        wait_for_url=AsyncMock(),
        wait_for_timeout=AsyncMock(),
    )
    context, _ = install_mock_playwright(monkeypatch, page)

    with pytest.raises(RuntimeError, match="consumer failure"):
        async with authenticated_context(tmp_path / "browser", "run-001", AuthPolicy.REUSE):
            raise RuntimeError("consumer failure")

    context.close.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_authenticated_context_closes_and_names_navigation_failure(tmp_path, monkeypatch):
    page = SimpleNamespace(
        url=auth_module.SHELF_URL,
        goto=AsyncMock(side_effect=auth_module.PlaywrightError("navigation failure")),
        wait_for_url=AsyncMock(),
        wait_for_timeout=AsyncMock(),
    )
    context, _ = install_mock_playwright(monkeypatch, page)

    with pytest.raises(ShelfUnavailable, match="could not be reached"):
        async with authenticated_context(tmp_path / "browser", "run-001", AuthPolicy.REUSE):
            pass

    context.close.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_authenticated_context_times_out_auth_and_closes(tmp_path, monkeypatch):
    page = SimpleNamespace(
        url="https://weread.qq.com/web/login",
        goto=AsyncMock(return_value=SimpleNamespace(status=200)),
        wait_for_url=AsyncMock(side_effect=auth_module.PlaywrightTimeoutError("timed out")),
        wait_for_timeout=AsyncMock(),
    )
    context, _ = install_mock_playwright(monkeypatch, page)

    with pytest.raises(AuthRequired, match="QR authorization timed out"):
        async with authenticated_context(tmp_path / "browser", "run-001", AuthPolicy.FRESH):
            pass

    page.wait_for_url.assert_awaited_once_with("**/web/shelf**", timeout=180_000)
    context.close.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_authenticated_context_reprobes_after_successful_login(tmp_path, monkeypatch):
    page = SimpleNamespace(
        url="https://weread.qq.com/web/login",
        goto=AsyncMock(
            side_effect=[SimpleNamespace(status=200), SimpleNamespace(status=200)]
        ),
        wait_for_url=AsyncMock(),
        wait_for_timeout=AsyncMock(),
    )

    async def complete_login(*args, **kwargs):
        page.url = auth_module.SHELF_URL

    page.wait_for_url.side_effect = complete_login
    context, _ = install_mock_playwright(monkeypatch, page)

    async with authenticated_context(tmp_path / "browser", "run-001", AuthPolicy.FRESH):
        pass

    assert page.goto.await_count == 2
    page.wait_for_url.assert_awaited_once_with("**/web/shelf**", timeout=180_000)
    context.close.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_authenticated_context_same_url_403_never_passes_without_reprobe(
    tmp_path, monkeypatch
):
    page = SimpleNamespace(
        url=auth_module.SHELF_URL,
        goto=AsyncMock(return_value=SimpleNamespace(status=403)),
        wait_for_url=AsyncMock(),
        wait_for_timeout=AsyncMock(side_effect=asyncio.TimeoutError),
    )
    context, _ = install_mock_playwright(monkeypatch, page)

    with pytest.raises(AuthRequired, match="timed out"):
        async with authenticated_context(tmp_path / "browser", "run-001", AuthPolicy.FRESH):
            pass

    assert page.goto.await_count == 2
    context.close.assert_awaited_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [None, SimpleNamespace(status=404)])
async def test_authenticated_context_rejects_unreadable_shelf_response(
    tmp_path, monkeypatch, response
):
    page = SimpleNamespace(
        url=auth_module.SHELF_URL,
        goto=AsyncMock(return_value=response),
        wait_for_url=AsyncMock(),
        wait_for_timeout=AsyncMock(),
    )
    context, _ = install_mock_playwright(monkeypatch, page)

    with pytest.raises(ShelfUnavailable):
        async with authenticated_context(tmp_path / "browser", "run-001", AuthPolicy.REUSE):
            pass

    context.close.assert_awaited_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url", ["https://evil.example/web/shelf", "https://weread.qq.com/web/other"]
)
async def test_authenticated_context_rejects_unexpected_success_url(tmp_path, monkeypatch, url):
    page = SimpleNamespace(
        url=url,
        goto=AsyncMock(return_value=SimpleNamespace(status=200)),
        wait_for_url=AsyncMock(),
        wait_for_timeout=AsyncMock(),
    )
    context, _ = install_mock_playwright(monkeypatch, page)

    with pytest.raises(ShelfUnavailable, match="unexpected URL"):
        async with authenticated_context(tmp_path / "browser", "run-001", AuthPolicy.REUSE):
            pass

    context.close.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_close_failure_after_success_is_named_shelf_failure(tmp_path, monkeypatch):
    page = SimpleNamespace(
        url=auth_module.SHELF_URL,
        goto=AsyncMock(return_value=SimpleNamespace(status=200)),
        wait_for_url=AsyncMock(),
        wait_for_timeout=AsyncMock(),
    )
    context, _ = install_mock_playwright(monkeypatch, page)
    context.close.side_effect = auth_module.PlaywrightError("close failed")

    with pytest.raises(ShelfUnavailable, match="could not be closed"):
        async with authenticated_context(tmp_path / "browser", "run-001", AuthPolicy.REUSE):
            pass


@pytest.mark.asyncio
async def test_close_failure_does_not_mask_primary_body_failure(tmp_path, monkeypatch):
    page = SimpleNamespace(
        url=auth_module.SHELF_URL,
        goto=AsyncMock(return_value=SimpleNamespace(status=200)),
        wait_for_url=AsyncMock(),
        wait_for_timeout=AsyncMock(),
    )
    context, _ = install_mock_playwright(monkeypatch, page)
    context.close.side_effect = auth_module.PlaywrightError("close failed")
    context.playwright_manager.exit.side_effect = auth_module.PlaywrightError(
        "manager exit failed"
    )

    with pytest.raises(RuntimeError, match="primary failure") as captured:
        async with authenticated_context(tmp_path / "browser", "run-001", AuthPolicy.REUSE):
            raise RuntimeError("primary failure")

    assert any("close failed" in note for note in captured.value.__notes__)
    assert any("manager exit failed" in note for note in captured.value.__notes__)


@pytest.mark.asyncio
async def test_close_failure_does_not_mask_primary_cancellation(tmp_path, monkeypatch):
    page = SimpleNamespace(
        url=auth_module.SHELF_URL,
        goto=AsyncMock(return_value=SimpleNamespace(status=200)),
        wait_for_url=AsyncMock(),
        wait_for_timeout=AsyncMock(),
    )
    context, _ = install_mock_playwright(monkeypatch, page)
    context.close.side_effect = auth_module.PlaywrightError("close failed")

    with pytest.raises(asyncio.CancelledError):
        async with authenticated_context(tmp_path / "browser", "run-001", AuthPolicy.REUSE):
            raise asyncio.CancelledError


@pytest.mark.asyncio
async def test_cancellation_during_close_is_not_reclassified(tmp_path, monkeypatch):
    page = SimpleNamespace(
        url=auth_module.SHELF_URL,
        goto=AsyncMock(return_value=SimpleNamespace(status=200)),
        wait_for_url=AsyncMock(),
        wait_for_timeout=AsyncMock(),
    )
    context, _ = install_mock_playwright(monkeypatch, page)
    context.close.side_effect = asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        async with authenticated_context(tmp_path / "browser", "run-001", AuthPolicy.REUSE):
            pass


@pytest.mark.asyncio
async def test_manager_exit_failure_after_success_is_named_shelf_failure(
    tmp_path, monkeypatch
):
    page = SimpleNamespace(
        url=auth_module.SHELF_URL,
        goto=AsyncMock(return_value=SimpleNamespace(status=200)),
        wait_for_url=AsyncMock(),
        wait_for_timeout=AsyncMock(),
    )
    context, _ = install_mock_playwright(monkeypatch, page)
    context.playwright_manager.exit.side_effect = auth_module.PlaywrightError(
        "manager exit failed"
    )

    with pytest.raises(ShelfUnavailable, match="Playwright manager could not be stopped"):
        async with authenticated_context(tmp_path / "browser", "run-001", AuthPolicy.REUSE):
            pass


@pytest.mark.asyncio
async def test_manager_exit_failure_does_not_mask_primary_cancellation(
    tmp_path, monkeypatch
):
    page = SimpleNamespace(
        url=auth_module.SHELF_URL,
        goto=AsyncMock(return_value=SimpleNamespace(status=200)),
        wait_for_url=AsyncMock(),
        wait_for_timeout=AsyncMock(),
    )
    context, _ = install_mock_playwright(monkeypatch, page)
    context.playwright_manager.exit.side_effect = auth_module.PlaywrightError(
        "manager exit failed"
    )

    with pytest.raises(asyncio.CancelledError):
        async with authenticated_context(tmp_path / "browser", "run-001", AuthPolicy.REUSE):
            raise asyncio.CancelledError


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("enter_error", "expected_error"),
    [
        (asyncio.CancelledError(), asyncio.CancelledError),
        (auth_module.PlaywrightError("enter failed"), ShelfUnavailable),
        (RuntimeError("generic enter failed"), RuntimeError),
    ],
)
async def test_partial_manager_enter_is_cleaned_without_masking_primary(
    tmp_path, monkeypatch, enter_error, expected_error
):
    page = SimpleNamespace(url=auth_module.SHELF_URL)
    context, _ = install_mock_playwright(monkeypatch, page)
    manager = context.playwright_manager
    manager.enter.side_effect = enter_error
    manager.exit.side_effect = auth_module.PlaywrightError("partial cleanup failed")

    with pytest.raises(expected_error) as captured:
        async with authenticated_context(tmp_path / "browser", "run-001", AuthPolicy.REUSE):
            pass

    manager.exit.assert_awaited_once()
    primary = captured.value.__cause__ or captured.value
    assert any("partial cleanup failed" in note for note in primary.__notes__)


@pytest.mark.asyncio
async def test_manager_enter_failure_before_connection_does_not_call_exit(
    tmp_path, monkeypatch
):
    page = SimpleNamespace(url=auth_module.SHELF_URL)
    context, _ = install_mock_playwright(monkeypatch, page)
    manager = context.playwright_manager
    manager.create_connection_on_enter = False
    manager.enter.side_effect = RuntimeError("failed before connection")

    with pytest.raises(RuntimeError, match="failed before connection"):
        async with authenticated_context(tmp_path / "browser", "run-001", AuthPolicy.REUSE):
            pass

    manager.exit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phase",
    [
        "launch",
        "new_page",
        "goto",
        "wait_for_url",
        "wait_for_timeout",
        "context_close",
        "manager_exit",
    ],
)
@pytest.mark.parametrize(
    ("failure_kind", "expected_error"),
    [("playwright", ShelfUnavailable), ("cancel", asyncio.CancelledError)],
)
async def test_playwright_phase_failure_matrix_preserves_domain_and_cleanup(
    tmp_path, monkeypatch, phase, failure_kind, expected_error
):
    failure = (
        auth_module.PlaywrightError(f"{phase} failed")
        if failure_kind == "playwright"
        else asyncio.CancelledError()
    )
    page = SimpleNamespace(
        url=auth_module.SHELF_URL,
        goto=AsyncMock(return_value=SimpleNamespace(status=200)),
        wait_for_url=AsyncMock(),
        wait_for_timeout=AsyncMock(),
    )
    context, launch = install_mock_playwright(monkeypatch, page)
    manager = context.playwright_manager

    if phase == "launch":
        launch.side_effect = failure
    elif phase == "new_page":
        context.pages = []
        context.new_page.side_effect = failure
    elif phase == "goto":
        page.goto.side_effect = failure
    elif phase == "wait_for_url":
        page.url = "https://weread.qq.com/web/login"
        page.wait_for_url.side_effect = failure
    elif phase == "wait_for_timeout":
        page.goto.return_value = SimpleNamespace(status=403)
        page.wait_for_timeout.side_effect = failure
    elif phase == "context_close":
        context.close.side_effect = failure
    else:
        manager.exit.side_effect = failure

    with pytest.raises(expected_error) as captured:
        async with authenticated_context(tmp_path / "browser", "run-001", AuthPolicy.REUSE):
            pass

    assert type(captured.value) is expected_error
    assert context.close.await_count == (0 if phase == "launch" else 1)
    manager.exit.assert_awaited_once()


@pytest.mark.parametrize(
    ("stage", "failure_type"),
    [
        ("anchor", KeyboardInterrupt),
        ("anchor", SystemExit),
        ("ancestor", KeyboardInterrupt),
        ("ancestor", SystemExit),
        ("private", KeyboardInterrupt),
        ("private", SystemExit),
    ],
)
def test_post_open_baseexception_closes_every_owned_descriptor(
    tmp_path: Path, monkeypatch, stage: str, failure_type: type[BaseException]
):
    opened: list[int] = []
    closed: list[int] = []
    original_open = auth_module.os.open
    original_close = auth_module.os.close
    original_fstat = auth_module.os.fstat
    original_fchmod = auth_module.os.fchmod
    fstat_calls = 0

    def tracking_open(path, flags, mode=0o777, *, dir_fd=None):
        descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
        opened.append(descriptor)
        return descriptor

    def tracking_close(descriptor):
        closed.append(descriptor)
        return original_close(descriptor)

    def injected_fstat(descriptor):
        nonlocal fstat_calls
        fstat_calls += 1
        if stage == "anchor" and fstat_calls == 1:
            raise failure_type("anchor inspection interrupted")
        if stage == "ancestor" and fstat_calls == 2:
            raise failure_type("ancestor inspection interrupted")
        return original_fstat(descriptor)

    def injected_fchmod(descriptor, mode):
        if stage == "private":
            raise failure_type("private inspection interrupted")
        return original_fchmod(descriptor, mode)

    monkeypatch.setattr(auth_module.os, "open", tracking_open)
    monkeypatch.setattr(auth_module.os, "close", tracking_close)
    monkeypatch.setattr(auth_module.os, "fstat", injected_fstat)
    monkeypatch.setattr(auth_module.os, "fchmod", injected_fchmod)

    with pytest.raises(failure_type):
        prepare_profile(tmp_path / "browser", "run-001", AuthPolicy.FRESH)

    assert opened
    assert set(opened) <= set(closed)


@pytest.mark.parametrize("failure_type", [KeyboardInterrupt, SystemExit])
def test_descriptor_list_cleanup_continues_after_baseexception(
    tmp_path: Path, monkeypatch, failure_type: type[BaseException]
):
    opened: list[int] = []
    closed: list[int] = []
    original_open = auth_module.os.open
    original_close = auth_module.os.close
    injected = False

    def tracking_open(path, flags, mode=0o777, *, dir_fd=None):
        descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
        opened.append(descriptor)
        return descriptor

    def close_then_interrupt_once(descriptor):
        nonlocal injected
        closed.append(descriptor)
        original_close(descriptor)
        if not injected:
            injected = True
            raise failure_type("descriptor cleanup interrupted")

    monkeypatch.setattr(auth_module.os, "open", tracking_open)
    monkeypatch.setattr(auth_module.os, "close", close_then_interrupt_once)

    with pytest.raises(failure_type):
        prepare_profile(tmp_path / "browser", "run-001", AuthPolicy.FRESH)

    assert set(opened) <= set(closed)
