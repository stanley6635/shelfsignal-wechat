from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

import shelfsignal.cli as cli_module
from shelfsignal.briefing import (
    initial_run_bindings,
    read_run_manifest,
    selected_ids,
    validate_briefing,
)
from shelfsignal.cli import (
    build_parser,
    main,
    prepare_run,
    process_client_run,
    write_omissions,
)
from shelfsignal.errors import (
    AuthRequired,
    ContentContractUnavailable,
    ShelfUnavailable,
)
from shelfsignal.models import CollectionOmission
from shelfsignal.ocr import ImageEvidence
from shelfsignal.state import StateStore
from shelfsignal.workspace import initialize_workspace


def test_version_command(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == "shelfsignal 0.1.0"


def test_public_command_surface() -> None:
    parser = build_parser()
    commands = parser._subparsers._group_actions[0].choices
    assert tuple(commands) == (
        "init",
        "doctor",
        "list-accounts",
        "seed",
        "collect",
        "prepare-briefing",
        "validate-briefing",
        "export",
    )


def test_init_reports_workspace_error_without_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)

    assert main(["init", str(root)]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "inside a Git repository" in captured.err
    assert "Traceback" not in captured.err


@pytest.mark.parametrize(
    ("error", "exit_code"),
    [
        (AuthRequired("authorization required"), 3),
        (ShelfUnavailable("shelf unavailable"), 4),
        (ContentContractUnavailable("content contract unavailable"), 5),
    ],
)
def test_named_global_failures_have_stable_exit_codes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: Exception,
    exit_code: int,
) -> None:
    def fail(_args: object) -> int:
        raise error

    monkeypatch.setattr(cli_module, "dispatch", fail)
    assert main(["doctor", "--workspace", "/tmp/example"]) == exit_code
    captured = capsys.readouterr()
    assert captured.out == ""
    assert type(error).__name__ in captured.err
    assert "Traceback" not in captured.err


def test_error_boundary_does_not_print_remote_payload(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload = '{"cookie":"private", "full_text":"private body"}'

    def fail(_args: object) -> int:
        raise ContentContractUnavailable("article response did not match the contract") from RuntimeError(
            payload
        )

    monkeypatch.setattr(cli_module, "dispatch", fail)
    assert main(["doctor", "--workspace", "/tmp/example"]) == 5
    captured = capsys.readouterr()
    assert payload not in captured.err
    assert "private body" not in captured.err


@pytest.mark.parametrize(
    "error",
    [
        ValueError("Authorization: Bearer stderr-secret\nsecond line"),
        AuthRequired("Cookie: session=stderr-cookie"),
    ],
)
def test_public_errors_are_redacted_bounded_and_single_line(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: Exception,
) -> None:
    monkeypatch.setattr(cli_module, "dispatch", lambda _args: (_ for _ in ()).throw(error))
    main(["doctor", "--workspace", "/tmp/example"])
    captured = capsys.readouterr()
    assert "stderr-secret" not in captured.err
    assert "stderr-cookie" not in captured.err
    assert "[REDACTED]" in captured.err
    assert len(captured.err.splitlines()) == 1


@pytest.mark.parametrize("run_id", ["../escape", "bad/id", " bad", "x" * 129])
def test_collect_rejects_unsafe_run_id_before_browser(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    run_id: str,
) -> None:
    paths = initialize_workspace(tmp_path / "runtime")
    called = False

    async def should_not_run(*_args: object, **_kwargs: object) -> Path:
        nonlocal called
        called = True
        return paths.briefings_dir / "unused.md"

    monkeypatch.setattr(cli_module, "collect_run", should_not_run)
    assert main(
        ["collect", "--workspace", str(paths.root), "--run-id", run_id]
    ) == 1
    assert not called


def test_validate_rejects_briefing_outside_workspace(tmp_path: Path) -> None:
    paths = initialize_workspace(tmp_path / "runtime")
    outside = tmp_path / "run-001.md"
    outside.write_text("# not a briefing\n", encoding="utf-8")

    assert main(
        ["validate-briefing", "--workspace", str(paths.root), str(outside)]
    ) == 1


def test_validate_rejects_symlinked_briefing(tmp_path: Path) -> None:
    paths = initialize_workspace(tmp_path / "runtime")
    outside = tmp_path / "outside.md"
    outside.write_text("private", encoding="utf-8")
    briefing = paths.briefings_dir / "run-001.md"
    briefing.symlink_to(outside)

    assert main(
        ["validate-briefing", "--workspace", str(paths.root), str(briefing)]
    ) == 1


@pytest.mark.asyncio
async def test_fake_end_to_end_rerun_digest_validation_and_checked_export(
    tmp_path: Path, fake_article_client: object
) -> None:
    paths = initialize_workspace(tmp_path / "runtime")
    store = StateStore(paths.state_db)
    store.initialize()
    store.start_run("run-001", "fresh")
    briefing = await process_client_run(
        paths,
        store,
        fake_article_client,
        7,
        "run-001",
        helper=tmp_path / "unused-helper",
        evidence_probe=lambda path: ImageEvidence(path, 1200, 8000),
        ocr_runner=lambda path: "recognized fictional text",
    )
    text = briefing.read_text(encoding="utf-8")
    manifest = paths.runs_dir / "run-001" / "manifest.md"
    bindings = read_run_manifest(manifest)
    assert bindings == initial_run_bindings(text)
    assert validate_briefing(text, bindings, require_unchecked=True) == (
        "article-image",
        "article-text",
    )
    assert text.count("- [ ] **Select**") == 2
    assert fake_article_client.content_calls == 2
    assert len(list(paths.library_dir.iterdir())) == 2
    assert (paths.runs_dir / "run-001" / "cards.md").exists()
    assert (paths.library_dir / "article-image" / "ocr.md").read_text(
        encoding="utf-8"
    ).endswith("recognized fictional text\n")

    await process_client_run(
        paths,
        store,
        fake_article_client,
        7,
        "run-001",
        helper=tmp_path / "unused-helper",
        evidence_probe=lambda path: ImageEvidence(path, 1200, 8000),
        ocr_runner=lambda path: "recognized fictional text",
    )
    assert fake_article_client.content_calls == 2

    checked = text.replace("- [ ] **Select**", "- [x] **Select**", 1)
    briefing.write_text(checked, encoding="utf-8")
    assert selected_ids(checked, bindings) == ("article-image",)
    assert main(
        ["validate-briefing", "--workspace", str(paths.root), str(briefing)]
    ) == 0
    assert main(
        ["export", "--workspace", str(paths.root), "--briefing", str(briefing)]
    ) == 0
    destination = paths.exports_dir / "run-001-selected"
    assert sorted(path.name for path in (destination / "articles").iterdir()) == [
        "article-image"
    ]

    wrong_header = checked.replace(
        "# WeChat briefing · run-001", "# WeChat briefing · run-other"
    )
    briefing.write_text(wrong_header, encoding="utf-8")
    assert main(
        ["validate-briefing", "--workspace", str(paths.root), str(briefing)]
    ) == 1
    assert main(
        ["export", "--workspace", str(paths.root), "--briefing", str(briefing)]
    ) == 1

    tampered = checked.replace('Title: "Image article"', 'Title: "Edited title"')
    briefing.write_text(tampered, encoding="utf-8")
    assert main(
        ["export", "--workspace", str(paths.root), "--briefing", str(briefing)]
    ) == 1


def test_collect_run_marks_cancelled_run_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = initialize_workspace(tmp_path / "runtime")
    enter_calls = 0

    class CancelContext:
        async def __aenter__(self) -> object:
            nonlocal enter_calls
            enter_calls += 1
            raise asyncio.CancelledError

        async def __aexit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(cli_module, "authenticated_context", lambda *_args: CancelContext())

    for _ in range(2):
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(
                cli_module.collect_run(
                    paths, cli_module.AuthPolicy.FRESH, 7, "run-001"
                )
            )
    assert enter_calls == 2
    assert StateStore(paths.state_db).run_status("run-001") == "failed"


def test_prepare_run_fails_closed_when_any_stored_article_is_corrupt(
    tmp_path: Path, fake_article_client: object
) -> None:
    paths = initialize_workspace(tmp_path / "runtime")
    store = StateStore(paths.state_db)
    store.initialize()
    store.start_run("run-001", "fresh")
    briefing = asyncio.run(
        process_client_run(
            paths,
            store,
            fake_article_client,
            7,
            "run-001",
            helper=tmp_path / "unused-helper",
            evidence_probe=lambda path: ImageEvidence(path, 1200, 8000),
            ocr_runner=lambda path: "recognized fictional text",
        )
    )
    manifest = paths.runs_dir / "run-001" / "manifest.md"
    briefing_before = briefing.read_bytes()
    manifest_before = manifest.read_bytes()
    (paths.library_dir / "article-text" / "source.md").write_text(
        "corrupt", encoding="utf-8"
    )

    with pytest.raises(ValueError, match="hash mismatch"):
        prepare_run(paths, store, "run-001")

    assert briefing.read_bytes() == briefing_before
    assert manifest.read_bytes() == manifest_before


def test_write_omissions_stays_bounded_and_reports_exact_remainder(tmp_path: Path) -> None:
    paths = initialize_workspace(tmp_path / "runtime")
    omissions = [
        CollectionOmission("s" * 500, f"item-{index}-" + "i" * 500, "r" * 500)
        for index in range(2_000)
    ]
    path = write_omissions(paths.runs_dir / "run-001", omissions)
    content = path.read_bytes()
    text = content.decode("utf-8")
    assert len(content) <= 512 * 1024
    summary = re.search(r"^- run `omissions`: (\d+) more omitted$", text, re.MULTILINE)
    assert summary is not None
    visible = sum(
        line.startswith("- ") and not line.startswith("- run `omissions`:")
        for line in text.splitlines()
    )
    assert int(summary.group(1)) == len(omissions) - visible
    store = StateStore(paths.state_db)
    store.initialize()
    store.start_run("run-001", "fresh")
    briefing = prepare_run(paths, store, "run-001")
    assert briefing.exists()


class _DummyContextManager:
    def __init__(self) -> None:
        self.context = type("Context", (), {"pages": [object()]})()

    async def __aenter__(self) -> object:
        return self.context

    async def __aexit__(self, *_args: object) -> None:
        return None


def _prevent_auth(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    called: list[bool] = []

    def fail(*_args: object) -> object:
        called.append(True)
        raise AssertionError("authentication must not start")

    monkeypatch.setattr(cli_module, "authenticated_context", fail)
    return called


@pytest.mark.parametrize("requested_policy", ["fresh", "reuse"])
def test_collect_rejects_complete_run_before_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, requested_policy: str
) -> None:
    paths = initialize_workspace(tmp_path / "runtime")
    store = StateStore(paths.state_db)
    store.initialize()
    store.start_run("run-001", "fresh")
    store.finish_run("run-001", "complete")
    called = _prevent_auth(monkeypatch)

    with pytest.raises(cli_module.StateError, match="not eligible"):
        asyncio.run(
            cli_module.collect_run(
                paths, cli_module.AuthPolicy(requested_policy), 7, "run-001"
            )
        )
    assert called == []


def test_collect_rejects_reserved_and_policy_mismatch_before_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = initialize_workspace(tmp_path / "runtime")
    store = StateStore(paths.state_db)
    store.initialize()
    store.start_run("run-001", "fresh")
    store.finish_run("run-001", "failed")
    called = _prevent_auth(monkeypatch)

    with pytest.raises(ValueError, match="reserved"):
        asyncio.run(
            cli_module.collect_run(
                paths, cli_module.AuthPolicy.FRESH, 7, "historical-seed"
            )
        )
    with pytest.raises(cli_module.StateError, match="authentication policy"):
        asyncio.run(
            cli_module.collect_run(paths, cli_module.AuthPolicy.REUSE, 7, "run-001")
        )
    assert called == []


def test_failed_run_same_policy_resumes_and_existing_briefing_is_protected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = initialize_workspace(tmp_path / "runtime")
    store = StateStore(paths.state_db)
    store.initialize()
    store.start_run("run-001", "fresh")
    store.finish_run("run-001", "failed")
    monkeypatch.setattr(
        cli_module, "authenticated_context", lambda *_args: _DummyContextManager()
    )
    monkeypatch.setattr(cli_module, "ensure_helper", lambda path: path / "helper")

    async def fake_process(*_args: object, **_kwargs: object) -> Path:
        return paths.briefings_dir / "run-001.md"

    monkeypatch.setattr(cli_module, "process_client_run", fake_process)
    assert asyncio.run(
        cli_module.collect_run(paths, cli_module.AuthPolicy.FRESH, 7, "run-001")
    ) == paths.briefings_dir / "run-001.md"
    assert store.run_details("run-001") == ("complete", "fresh")

    store.start_run("run-002", "fresh")
    store.finish_run("run-002", "failed")
    protected = paths.briefings_dir / "run-002.md"
    protected.write_text("human edit", encoding="utf-8")
    with pytest.raises(cli_module.StateError, match="published"):
        asyncio.run(
            cli_module.collect_run(paths, cli_module.AuthPolicy.FRESH, 7, "run-002")
        )
    assert protected.read_text(encoding="utf-8") == "human edit"


def test_manual_prepare_requires_existing_unpublished_interrupted_run(
    tmp_path: Path,
) -> None:
    paths = initialize_workspace(tmp_path / "runtime")
    assert main(
        ["prepare-briefing", "--workspace", str(paths.root), "--run", "missing"]
    ) == 1
    store = StateStore(paths.state_db)
    store.start_run("complete-run", "fresh")
    store.finish_run("complete-run", "complete")
    assert main(
        [
            "prepare-briefing",
            "--workspace",
            str(paths.root),
            "--run",
            "complete-run",
        ]
    ) == 1
    assert main(
        [
            "prepare-briefing",
            "--workspace",
            str(paths.root),
            "--run",
            "historical-seed",
        ]
    ) == 1


@pytest.mark.asyncio
async def test_same_run_collect_lease_rejects_concurrent_caller_before_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = initialize_workspace(tmp_path / "runtime")
    entered = asyncio.Event()
    release = asyncio.Event()
    auth_calls = 0

    class HoldingContext:
        async def __aenter__(self) -> object:
            nonlocal auth_calls
            auth_calls += 1
            entered.set()
            await release.wait()
            return type("Context", (), {"pages": [object()]})()

        async def __aexit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(cli_module, "authenticated_context", lambda *_args: HoldingContext())
    monkeypatch.setattr(cli_module, "ensure_helper", lambda path: path / "helper")

    async def fake_process(*_args: object, **_kwargs: object) -> Path:
        return paths.briefings_dir / "run-001.md"

    monkeypatch.setattr(cli_module, "process_client_run", fake_process)
    first = asyncio.create_task(
        cli_module.collect_run(paths, cli_module.AuthPolicy.FRESH, 7, "run-001")
    )
    await entered.wait()
    with pytest.raises(cli_module.StateError, match="already operating"):
        await cli_module.collect_run(paths, cli_module.AuthPolicy.FRESH, 7, "run-001")
    assert auth_calls == 1
    release.set()
    assert await first == paths.briefings_dir / "run-001.md"


def test_collect_lease_releases_after_auth_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = initialize_workspace(tmp_path / "runtime")
    auth_calls = 0

    class FailingContext:
        async def __aenter__(self) -> object:
            nonlocal auth_calls
            auth_calls += 1
            raise AuthRequired("authorization required")

        async def __aexit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(cli_module, "authenticated_context", lambda *_args: FailingContext())
    for _ in range(2):
        with pytest.raises(AuthRequired):
            asyncio.run(
                cli_module.collect_run(
                    paths, cli_module.AuthPolicy.FRESH, 7, "run-001"
                )
            )
    assert auth_calls == 2
    assert StateStore(paths.state_db).run_status("run-001") == "failed"


@pytest.mark.parametrize("unsafe_kind", ["symlink", "directory", "hardlink"])
def test_collect_rejects_unsafe_run_lock_leaf_before_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unsafe_kind: str
) -> None:
    paths = initialize_workspace(tmp_path / "runtime")
    run_dir = paths.runs_dir / "run-001"
    run_dir.mkdir()
    lock = run_dir / ".shelfsignal.lock"
    outside = tmp_path / "outside-lock"
    outside.write_text("outside", encoding="utf-8")
    if unsafe_kind == "symlink":
        lock.symlink_to(outside)
    elif unsafe_kind == "directory":
        lock.mkdir()
    else:
        lock.hardlink_to(outside)
    called = _prevent_auth(monkeypatch)

    with pytest.raises(cli_module.StateError, match="unsafe"):
        asyncio.run(
            cli_module.collect_run(paths, cli_module.AuthPolicy.FRESH, 7, "run-001")
        )
    assert called == []
    assert outside.read_text(encoding="utf-8") == "outside"
