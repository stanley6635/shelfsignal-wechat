from pathlib import Path

import pytest

from shelfsignal.cli import main
from shelfsignal.seed import SeedError, seed_markdown_archive
from shelfsignal.state import StateStore
from shelfsignal.workspace import initialize_workspace


def test_seed_is_read_only_and_idempotent(tmp_path: Path):
    fixture = Path("tests/fixtures/historical-briefing.md")
    archive = tmp_path / "archive.md"
    archive.write_bytes(fixture.read_bytes())
    before = archive.read_bytes()
    store = StateStore(tmp_path / "state.db")
    store.initialize()

    first = seed_markdown_archive(archive, store)
    second = seed_markdown_archive(archive, store)

    assert first.scanned_files == 1
    assert first.discovered == 1
    assert first.imported == 1
    assert second.imported == 0
    assert store.is_known_url("https://example.invalid/wechat/demo-001")
    assert archive.read_bytes() == before


def test_seed_recursively_scans_markdown_only(tmp_path: Path):
    archive = tmp_path / "archive"
    nested = archive / "nested"
    nested.mkdir(parents=True)
    (archive / "first.md").write_text(
        "https://example.invalid/wechat/first,\n", encoding="utf-8"
    )
    (nested / "second.md").write_text(
        "[Second](https://example.invalid/wechat/second)\n", encoding="utf-8"
    )
    (nested / "ignored.txt").write_text(
        "https://example.invalid/wechat/ignored\n", encoding="utf-8"
    )
    store = StateStore(tmp_path / "state.db")
    store.initialize()

    result = seed_markdown_archive(archive, store)

    assert result.scanned_files == 2
    assert result.discovered == 2
    assert result.imported == 2
    assert store.is_known_url("https://example.invalid/wechat/first")
    assert store.is_known_url("https://example.invalid/wechat/second")
    assert not store.is_known_url("https://example.invalid/wechat/ignored")


def test_seed_cli_prints_only_counts(tmp_path: Path, capsys):
    workspace = initialize_workspace(tmp_path / "workspace")
    archive = Path("tests/fixtures/historical-briefing.md")

    assert main(
        ["seed", "--workspace", str(workspace.root), str(archive)]
    ) == 0

    captured = capsys.readouterr()
    assert captured.out == "scanned=1 discovered=1 imported=1\n"
    assert captured.err == ""


def test_seed_cli_rejects_symlinked_workspace_before_database_access(
    tmp_path: Path, capsys
):
    workspace = initialize_workspace(tmp_path / "workspace")
    workspace_link = tmp_path / "workspace-link"
    workspace_link.symlink_to(workspace.root, target_is_directory=True)

    assert main(
        [
            "seed",
            "--workspace",
            str(workspace_link),
            "tests/fixtures/historical-briefing.md",
        ]
    ) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "workspace path is a symbolic link" in captured.err
    assert not workspace.state_db.exists()


def test_seed_cli_rejects_uninitialized_workspace(tmp_path: Path, capsys):
    workspace = tmp_path / "not-initialized"
    workspace.mkdir()

    assert main(
        [
            "seed",
            "--workspace",
            str(workspace),
            "tests/fixtures/historical-briefing.md",
        ]
    ) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "not an initialized ShelfSignal workspace" in captured.err
    assert not (workspace / "state.db").exists()


def test_seed_cli_rejects_workspace_inside_git_repository(
    tmp_path: Path, capsys
):
    repository = tmp_path / "repository"
    (repository / ".git").mkdir(parents=True)

    assert main(
        [
            "seed",
            "--workspace",
            str(repository),
            "tests/fixtures/historical-briefing.md",
        ]
    ) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "inside a Git repository" in captured.err
    assert not (repository / "state.db").exists()


def test_seed_rejects_symlink_archive_root_without_reading_target(tmp_path: Path):
    target = tmp_path / "private.md"
    target.write_text("https://example.invalid/private", encoding="utf-8")
    archive = tmp_path / "archive.md"
    archive.symlink_to(target)
    store = StateStore(tmp_path / "state.db")
    store.initialize()

    with pytest.raises(SeedError, match="symbolic link"):
        seed_markdown_archive(archive, store)

    assert not store.is_known_url("https://example.invalid/private")


@pytest.mark.parametrize("symlink_kind", ["file", "directory"])
def test_seed_rejects_symlinks_inside_archive(
    tmp_path: Path, symlink_kind: str
):
    archive = tmp_path / "archive"
    archive.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    if symlink_kind == "file":
        target = outside / "private.md"
        target.write_text("https://example.invalid/private", encoding="utf-8")
        (archive / "private.md").symlink_to(target)
    else:
        target = outside / "private"
        target.mkdir()
        (target / "briefing.md").write_text(
            "https://example.invalid/private", encoding="utf-8"
        )
        (archive / "private").symlink_to(target, target_is_directory=True)
    store = StateStore(tmp_path / "state.db")
    store.initialize()

    with pytest.raises(SeedError, match="symbolic link"):
        seed_markdown_archive(archive, store)

    assert not store.is_known_url("https://example.invalid/private")


def test_seed_normalizes_markup_and_sentence_delimiters_but_keeps_query(
    tmp_path: Path,
):
    archive = tmp_path / "archive.md"
    archive.write_text(
        "[Linked](https://example.invalid/wechat/linked)\n"
        "**https://example.invalid/wechat/emphasized**.\n"
        "https://example.invalid/wechat/ascii.\n"
        "https://example.invalid/wechat/cjk。\n"
        "https://example.invalid/wechat/query?from=archive&kind=full\n"
        "https://example.invalid/wechat/encoded"
        "?redirect=https%3A%2F%2Ftarget.invalid%2Fa%3Fx%3D1%26y%3D2",
        encoding="utf-8",
    )
    store = StateStore(tmp_path / "state.db")
    store.initialize()

    result = seed_markdown_archive(archive, store)

    assert result.discovered == 6
    assert result.imported == 6
    for url in (
        "https://example.invalid/wechat/linked",
        "https://example.invalid/wechat/emphasized",
        "https://example.invalid/wechat/ascii",
        "https://example.invalid/wechat/cjk",
        "https://example.invalid/wechat/query?from=archive&kind=full",
        (
            "https://example.invalid/wechat/encoded"
            "?redirect=https%3A%2F%2Ftarget.invalid%2Fa%3Fx%3D1%26y%3D2"
        ),
    ):
        assert store.is_known_url(url)


@pytest.mark.parametrize(
    "terminal_delimiter",
    [
        "]",
        '"',
        "'",
        "`",
        "）",
        "】",
        "》",
        "〉",
        "」",
        "』",
        "”",
        "’",
        "、",
        ".",
        "。",
    ],
)
def test_seed_removes_terminal_wrappers_from_url_fingerprint(
    tmp_path: Path, terminal_delimiter: str
):
    clean_url = "https://example.invalid/wechat/wrapped?from=archive&kind=full"
    corrupted_url = f"{clean_url}{terminal_delimiter}"
    archive = tmp_path / "archive.md"
    archive.write_text(f"原文（{corrupted_url}\n", encoding="utf-8")
    store = StateStore(tmp_path / "state.db")
    store.initialize()

    result = seed_markdown_archive(archive, store)

    assert result.imported == 1
    assert store.is_known_url(clean_url)
    assert not store.is_known_url(corrupted_url)
