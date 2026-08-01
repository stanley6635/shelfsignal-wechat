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

    async def __aenter__(self):
        return self.playwright

    async def __aexit__(self, exc_type, exc, traceback):
        return False


def install_mock_playwright(monkeypatch, page):
    context = SimpleNamespace(
        pages=[page],
        new_page=AsyncMock(),
        close=AsyncMock(),
    )
    launch = AsyncMock(return_value=context)
    playwright = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch))
    monkeypatch.setattr(
        auth_module,
        "async_playwright",
        lambda: FakePlaywrightManager(playwright),
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


def test_server_failure_is_shelf_unavailable():
    with pytest.raises(ShelfUnavailable, match="HTTP 503"):
        classify_shelf_probe(503)

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
