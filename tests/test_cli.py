from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import shelfsignal.cli as cli_module
from shelfsignal.briefing import (
    initial_run_bindings,
    read_run_manifest,
    selected_ids,
    validate_briefing,
)
from shelfsignal.cli import build_parser, main, process_client_run
from shelfsignal.errors import (
    AuthRequired,
    ContentContractUnavailable,
    ShelfUnavailable,
)
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

    tampered = checked.replace('Title: "Image article"', 'Title: "Edited title"')
    briefing.write_text(tampered, encoding="utf-8")
    assert main(
        ["export", "--workspace", str(paths.root), "--briefing", str(briefing)]
    ) == 1


def test_collect_run_marks_cancelled_run_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = initialize_workspace(tmp_path / "runtime")

    class CancelContext:
        async def __aenter__(self) -> object:
            raise asyncio.CancelledError

        async def __aexit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(cli_module, "authenticated_context", lambda *_args: CancelContext())

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            cli_module.collect_run(paths, cli_module.AuthPolicy.FRESH, 7, "run-001")
        )
    assert StateStore(paths.state_db).run_status("run-001") == "failed"
