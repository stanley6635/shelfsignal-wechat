from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

import pytest

import shelfsignal.exporter as exporter_module
from shelfsignal.exporter import ExportError, export_selected


def make_article(root: Path, article_id: str) -> Path:
    directory = root / article_id
    (directory / "assets").mkdir(parents=True)
    (directory / "source.md").write_text(
        f"# {article_id}\n\n![image](assets/image.jpg)\n", encoding="utf-8"
    )
    (directory / "metadata.md").write_text(
        f"# Source metadata\n\n- article_id: \"{article_id}\"\n", encoding="utf-8"
    )
    (directory / "assets" / "image.jpg").write_bytes(b"fictional-image")
    return directory


def manifest(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_export_contains_selected_article_only(tmp_path: Path):
    library = tmp_path / "library"
    make_article(library, "selected-1")
    make_article(library, "not-selected")
    destination = tmp_path / "exports" / "run-001-selected"

    result = export_selected(("selected-1",), library, destination)

    assert result == destination
    assert (destination / "articles" / "selected-1" / "source.md").exists()
    assert not (destination / "articles" / "not-selected").exists()
    assert "selected-1" in (destination / "index.md").read_text(encoding="utf-8")
    assert stat.S_IMODE(destination.stat().st_mode) == 0o700
    assert all(
        stat.S_IMODE(path.stat().st_mode) == 0o600
        for path in destination.rglob("*")
        if path.is_file()
    )


def test_export_is_idempotent_private_and_self_contained(tmp_path: Path):
    library = tmp_path / "library"
    article = make_article(library, "selected-1")
    (article / "ocr.md").write_text("# OCR-derived evidence\n", encoding="utf-8")
    (article / "cookies.sqlite").write_bytes(b"secret")
    (article / "profile.md").write_text("private", encoding="utf-8")
    (article / "state.db").write_bytes(b"private-state")
    (article / ".hidden.jpg").write_bytes(b"private")
    destination = tmp_path / "exports" / "run-001-selected"

    export_selected(("selected-1",), library, destination)
    first = manifest(destination)
    export_selected(("selected-1",), library, destination)

    assert manifest(destination) == first
    assert (destination / "articles" / "selected-1" / "ocr.md").exists()
    assert not list(destination.rglob("cookies.sqlite"))
    assert not list(destination.rglob("profile.md"))
    assert not list(destination.rglob("state.db"))
    assert not any(path.name.startswith(".") for path in destination.rglob("*"))


@pytest.mark.parametrize(
    "article_ids",
    [
        ("selected-1", "selected-1"),
        ("../outside",),
        ("/absolute",),
        ("",),
        (".hidden",),
    ],
)
def test_export_rejects_duplicate_or_unsafe_article_ids(
    tmp_path: Path, article_ids: tuple[str, ...]
):
    library = tmp_path / "library"
    make_article(library, "selected-1")

    with pytest.raises((ExportError, ValueError)):
        export_selected(article_ids, library, tmp_path / "exports" / "bundle")

    assert not (tmp_path / "exports" / "bundle").exists()


def test_export_rejects_symlinked_library_article_files_assets_and_destination(
    tmp_path: Path,
):
    library = tmp_path / "library"
    article = make_article(library, "selected-1")
    destination = tmp_path / "exports" / "bundle"
    outside = tmp_path / "outside"
    outside.mkdir()

    source = article / "source.md"
    source.unlink()
    source.symlink_to(outside / "source.md")
    with pytest.raises(ExportError, match="source.md"):
        export_selected(("selected-1",), library, destination)

    source.unlink()
    source.write_text("# safe\n", encoding="utf-8")
    assets = article / "assets"
    (assets / "image.jpg").unlink()
    assets.rmdir()
    assets.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ExportError, match="assets"):
        export_selected(("selected-1",), library, destination)
    assets.unlink()
    assets.mkdir()
    (assets / "image.jpg").write_bytes(b"fictional-image")

    alias = tmp_path / "library-alias"
    alias.symlink_to(library, target_is_directory=True)
    with pytest.raises(ExportError, match="library"):
        export_selected(("selected-1",), alias, destination)

    exports = tmp_path / "exports"
    exports.mkdir(exist_ok=True)
    destination.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ExportError, match="destination"):
        export_selected(("selected-1",), library, destination)


def test_export_rejects_nonregular_nested_hidden_and_nonraster_assets(tmp_path: Path):
    library = tmp_path / "library"
    article = make_article(library, "selected-1")
    assets = article / "assets"

    (assets / "nested").mkdir()
    with pytest.raises(ExportError, match="asset"):
        export_selected(("selected-1",), library, tmp_path / "exports" / "nested")
    (assets / "nested").rmdir()

    (assets / ".private.jpg").write_bytes(b"private")
    with pytest.raises(ExportError, match="hidden"):
        export_selected(("selected-1",), library, tmp_path / "exports" / "hidden")
    (assets / ".private.jpg").unlink()

    (assets / "payload.txt").write_bytes(b"not-raster")
    with pytest.raises(ExportError, match="raster"):
        export_selected(("selected-1",), library, tmp_path / "exports" / "nonraster")


@pytest.mark.parametrize(
    "target",
    [
        "../private.md",
        "/private.md",
        "file:///private.md",
        "http://example.invalid/insecure",
        "assets/missing.jpg",
        "assets/%2e%2e/private.md",
        r"assets\\image.jpg",
    ],
)
def test_export_rejects_unsafe_or_unresolved_markdown_links(
    tmp_path: Path, target: str
):
    library = tmp_path / "library"
    article = make_article(library, "selected-1")
    (article / "source.md").write_text(
        f"# selected-1\n\n[untrusted]({target})\n", encoding="utf-8"
    )

    with pytest.raises(ExportError, match="Markdown link"):
        export_selected(("selected-1",), library, tmp_path / "exports" / "bundle")


def test_export_rejects_unsafe_reference_style_markdown_link(tmp_path: Path):
    library = tmp_path / "library"
    article = make_article(library, "selected-1")
    (article / "source.md").write_text(
        "# selected-1\n\n[private][ref]\n\n[ref]: ../private.md\n",
        encoding="utf-8",
    )

    with pytest.raises(ExportError, match="Markdown link"):
        export_selected(("selected-1",), library, tmp_path / "exports" / "bundle")


def test_export_allows_https_and_fragment_links(tmp_path: Path):
    library = tmp_path / "library"
    article = make_article(library, "selected-1")
    (article / "source.md").write_text(
        "# selected-1\n\n[section](#section)\n\n[remote](https://example.invalid/a)\n",
        encoding="utf-8",
    )

    export_selected(("selected-1",), library, tmp_path / "exports" / "bundle")


def test_export_enforces_count_file_and_aggregate_size_bounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    library = tmp_path / "library"
    make_article(library, "selected-1")
    destination = tmp_path / "exports" / "bundle"

    monkeypatch.setattr(exporter_module, "_MAX_ARTICLES", 0)
    with pytest.raises(ExportError, match="too many selected"):
        export_selected(("selected-1",), library, destination)
    monkeypatch.setattr(exporter_module, "_MAX_ARTICLES", 2_000)

    monkeypatch.setattr(exporter_module, "_MAX_SOURCE_BYTES", 4)
    with pytest.raises(ExportError, match="source.md.*too large"):
        export_selected(("selected-1",), library, destination)
    monkeypatch.setattr(exporter_module, "_MAX_SOURCE_BYTES", 128 * 1024 * 1024)

    monkeypatch.setattr(exporter_module, "_MAX_EXPORT_BYTES", 10)
    with pytest.raises(ExportError, match="aggregate"):
        export_selected(("selected-1",), library, destination)
    assert not destination.exists()


def test_differing_existing_destination_fails_without_mutation_or_staging_leak(
    tmp_path: Path,
):
    library = tmp_path / "library"
    make_article(library, "selected-1")
    make_article(library, "selected-2")
    destination = tmp_path / "exports" / "bundle"
    export_selected(("selected-1", "selected-2"), library, destination)
    before = manifest(destination)

    with pytest.raises(ExportError, match="already exists with different content"):
        export_selected(("selected-1",), library, destination)

    assert manifest(destination) == before
    assert not list(destination.parent.glob(".bundle.staging-*"))


def test_write_failure_leaves_no_partial_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    library = tmp_path / "library"
    make_article(library, "selected-1")
    destination = tmp_path / "exports" / "bundle"
    real_write = exporter_module._write_file_at
    calls = 0

    def fail_second_write(directory_fd: int, name: str, content: bytes) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated write failure")
        real_write(directory_fd, name, content)

    monkeypatch.setattr(exporter_module, "_write_file_at", fail_second_write)
    with pytest.raises(OSError, match="simulated"):
        export_selected(("selected-1",), library, destination)

    assert not destination.exists()
    assert not list(destination.parent.glob(".bundle.staging-*"))


def test_export_rejects_relative_and_non_normalized_roots(tmp_path: Path):
    library = tmp_path / "library"
    make_article(library, "selected-1")

    with pytest.raises(ExportError, match="library"):
        export_selected(("selected-1",), Path("library"), tmp_path / "exports" / "bundle")
    with pytest.raises(ExportError, match="destination"):
        export_selected(("selected-1",), library, tmp_path / "exports" / ".." / "bundle")


def test_existing_destination_with_hidden_or_nonregular_content_is_rejected(
    tmp_path: Path,
):
    library = tmp_path / "library"
    make_article(library, "selected-1")
    destination = tmp_path / "exports" / "bundle"
    export_selected(("selected-1",), library, destination)
    (destination / ".secret").write_bytes(b"secret")

    with pytest.raises(ExportError, match="unsafe destination"):
        export_selected(("selected-1",), library, destination)


def test_directory_and_file_modes_are_repaired_only_for_new_bundle(tmp_path: Path):
    library = tmp_path / "library"
    make_article(library, "selected-1")
    os.chmod(library / "selected-1" / "source.md", 0o644)
    destination = tmp_path / "exports" / "bundle"

    export_selected(("selected-1",), library, destination)

    assert stat.S_IMODE((destination / "articles").stat().st_mode) == 0o700
    assert stat.S_IMODE(
        (destination / "articles" / "selected-1" / "source.md").stat().st_mode
    ) == 0o600
