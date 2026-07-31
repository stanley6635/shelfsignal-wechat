from pathlib import Path

from shelfsignal.cli import main
from shelfsignal.seed import seed_markdown_archive
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
