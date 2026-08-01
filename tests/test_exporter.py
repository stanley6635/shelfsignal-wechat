from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path

import pytest

import shelfsignal.exporter as exporter_module
from shelfsignal.content import safe_asset_path
from shelfsignal.exporter import ExportError, export_selected

JPEG = b"\xff\xd8\xfffictional-jpeg"
PNG = b"\x89PNG\r\n\x1a\nfictional-png"


def make_article(root: Path, article_id: str) -> Path:
    directory = root / article_id
    (directory / "assets").mkdir(parents=True)
    source = f"# {article_id}\n\n![image](assets/image.jpg)\n".encode()
    (directory / "source.md").write_bytes(source)
    values = {
        "account_id": "fictional-account",
        "account_name": "Fictional Account",
        "article_id": article_id,
        "published_at": "2026-07-31T00:00:00+00:00",
        "source_sha256": hashlib.sha256(source).hexdigest(),
        "source_url": f"https://example.invalid/{article_id}",
        "status": "complete",
        "title": article_id,
    }
    metadata = ["# Source metadata", ""]
    metadata.extend(
        f"- {key}: {json.dumps(value)}" for key, value in sorted(values.items())
    )
    (directory / "metadata.md").write_text("\n".join(metadata) + "\n", encoding="utf-8")
    (directory / "assets" / "image.jpg").write_bytes(JPEG)
    return directory


def replace_source(article: Path, markdown: str) -> None:
    source = markdown.encode("utf-8")
    (article / "source.md").write_bytes(source)
    metadata_path = article / "metadata.md"
    metadata = metadata_path.read_text(encoding="utf-8")
    metadata = re.sub(
        r"(?m)^- source_sha256: .*$",
        f"- source_sha256: {json.dumps(hashlib.sha256(source).hexdigest())}",
        metadata,
    )
    metadata_path.write_text(metadata, encoding="utf-8")


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
    replace_source(article, "# safe\n")
    assets = article / "assets"
    (assets / "image.jpg").unlink()
    assets.rmdir()
    assets.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ExportError, match="assets"):
        export_selected(("selected-1",), library, destination)
    assets.unlink()
    assets.mkdir()
    (assets / "image.jpg").write_bytes(JPEG)

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
    replace_source(article, f"# selected-1\n\n[untrusted]({target})\n")

    with pytest.raises(ExportError, match="Markdown link"):
        export_selected(("selected-1",), library, tmp_path / "exports" / "bundle")


def test_export_rejects_unsafe_reference_style_markdown_link(tmp_path: Path):
    library = tmp_path / "library"
    article = make_article(library, "selected-1")
    replace_source(
        article,
        "# selected-1\n\n[private][ref]\n\n[ref]: ../private.md\n",
    )

    with pytest.raises(ExportError, match="Markdown link"):
        export_selected(("selected-1",), library, tmp_path / "exports" / "bundle")


def test_export_allows_https_and_fragment_links(tmp_path: Path):
    library = tmp_path / "library"
    article = make_article(library, "selected-1")
    replace_source(
        article,
        "# selected-1\n\n[section](#section)\n\n"
        "[remote](https://example.invalid/a)\n\n"
        "<https://example.invalid/autolink>\n\n2 < 5 and 7 > 3\n",
    )

    export_selected(("selected-1",), library, tmp_path / "exports" / "bundle")


@pytest.mark.parametrize(
    "markup",
    [
        "<file:///etc/passwd>",
        "<data:text/plain,private>",
        '<a href="https://example.invalid/hidden">hidden</a>',
        '<img src="https://example.invalid/hidden.jpg">',
    ],
)
def test_export_rejects_unsafe_autolinks_and_raw_html(
    tmp_path: Path, markup: str
):
    library = tmp_path / "library"
    article = make_article(library, "selected-1")
    replace_source(article, f"# selected-1\n\n{markup}\n")

    with pytest.raises(ExportError, match="Markdown link|raw HTML"):
        export_selected(("selected-1",), library, tmp_path / "exports" / "bundle")


def test_export_accepts_suffixless_collector_asset_when_magic_is_raster(tmp_path: Path):
    library = tmp_path / "library"
    article = make_article(library, "selected-1")
    default = article / "assets" / "image.jpg"
    default.unlink()
    generated = safe_asset_path(
        article / "assets",
        "https://mmbiz.qpic.cn/mmbiz_png/fictional-token/640",
    )
    assert generated.suffix == ".bin"
    generated.write_bytes(PNG)
    replace_source(
        article,
        f"# selected-1\n\n![image](assets/{generated.name})\n",
    )

    destination = tmp_path / "exports" / "bundle"
    export_selected(("selected-1",), library, destination)

    assert (destination / "articles" / "selected-1" / "assets" / generated.name).read_bytes() == PNG


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("image.jpg", b"plain text"),
        ("image.bin", b"arbitrary binary"),
        ("image.png", JPEG),
    ],
)
def test_export_rejects_fake_or_mismatched_raster_assets(
    tmp_path: Path, name: str, content: bytes
):
    library = tmp_path / "library"
    article = make_article(library, "selected-1")
    default = article / "assets" / "image.jpg"
    default.unlink()
    (article / "assets" / name).write_bytes(content)
    replace_source(article, f"# selected-1\n\n![image](assets/{name})\n")

    with pytest.raises(ExportError, match="raster signature"):
        export_selected(("selected-1",), library, tmp_path / "exports" / "bundle")


@pytest.mark.parametrize(
    "relative",
    ["source.md", "metadata.md", "ocr.md", "assets/image.jpg"],
)
def test_export_rejects_hardlinked_article_files_without_exporting_private_bytes(
    tmp_path: Path, relative: str
):
    library = tmp_path / "library"
    article = make_article(library, "selected-1")
    private = tmp_path / "private-hardlink"
    private.write_bytes(JPEG if relative.startswith("assets/") else b"private hardlinked data")
    target = article / relative
    target.unlink(missing_ok=True)
    os.link(private, target)
    destination = tmp_path / "exports" / "bundle"

    with pytest.raises(ExportError, match="unsafe"):
        export_selected(("selected-1",), library, destination)

    assert not destination.exists()
    exports = tmp_path / "exports"
    if exports.exists():
        assert not list(exports.glob(".bundle.staging-*"))


@pytest.mark.parametrize("damage", ["article-id", "source-hash", "missing-field"])
def test_export_validates_stored_article_metadata_contract(
    tmp_path: Path, damage: str
):
    library = tmp_path / "library"
    article = make_article(library, "selected-1")
    metadata = article / "metadata.md"
    if damage == "article-id":
        metadata.write_text(
            metadata.read_text(encoding="utf-8").replace(
                '- article_id: "selected-1"', '- article_id: "other-id"'
            ),
            encoding="utf-8",
        )
    elif damage == "source-hash":
        (article / "source.md").write_bytes(b"changed private source")
    else:
        metadata.write_text(
            re.sub(r"(?m)^- title: .*\n", "", metadata.read_text(encoding="utf-8")),
            encoding="utf-8",
        )

    with pytest.raises(ExportError, match="metadata|hash mismatch"):
        export_selected(("selected-1",), library, tmp_path / "exports" / "bundle")
    assert not (tmp_path / "exports" / "bundle").exists()


def test_idempotency_read_rejects_hardlinked_destination_file(tmp_path: Path):
    library = tmp_path / "library"
    make_article(library, "selected-1")
    destination = tmp_path / "exports" / "bundle"
    export_selected(("selected-1",), library, destination)
    os.link(destination / "index.md", tmp_path / "second-index-link.md")

    with pytest.raises(ExportError, match="unsafe destination"):
        export_selected(("selected-1",), library, destination)


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
