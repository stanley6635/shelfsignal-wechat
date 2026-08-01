from __future__ import annotations

import os
from pathlib import Path

import pytest

from shelfsignal.content import (
    atomic_write,
    load_stored_article,
    normalize_html,
    safe_article_dir,
    safe_asset_path,
    write_source,
)
from shelfsignal.models import ArticleStatus


def test_normalize_html_keeps_text_and_meaningful_images():
    html = Path("tests/fixtures/article-text.html").read_text(encoding="utf-8")
    normalized = normalize_html(html)
    assert normalized.markdown == "Example article\n\nFictional evidence paragraph.\n"
    assert normalized.image_urls == ("https://example.invalid/content.jpg",)


def test_normalize_html_ignores_unsafe_images_and_malformed_dimensions():
    normalized = normalize_html(
        """
        <p>Visible text.</p>
        <script>private = 'not article text'</script>
        <img width="large" height="900" src="https://example.invalid/bad-width.jpg">
        <img width="640" height="480" src="javascript:alert(1)">
        <img width="640" height="480" src="https:///missing-host.jpg">
        <img width="640" height="480" src="https://example.invalid/repeated.jpg">
        <img width="640" height="480" src="https://example.invalid/repeated.jpg">
        """
    )
    assert normalized.markdown == "Visible text.\n"
    assert normalized.image_urls == ("https://example.invalid/repeated.jpg",)


@pytest.mark.parametrize(
    "source",
    [
        "../../cookie.txt",
        "https://example.invalid/../cookie.txt",
        "https://example.invalid/%2e%2e/cookie.txt",
        "https:///missing-host.jpg",
        "file:///tmp/cookie.txt",
    ],
)
def test_asset_path_blocks_traversal_and_unsafe_urls(tmp_path: Path, source: str):
    with pytest.raises(ValueError, match="unsafe asset"):
        safe_asset_path(tmp_path, source)


def test_asset_path_is_deterministic_and_rejects_symlink_directory(tmp_path: Path):
    assets = tmp_path / "assets"
    assets.mkdir()
    first = safe_asset_path(assets, "https://example.invalid/image.JPG?size=large")
    second = safe_asset_path(assets, "https://example.invalid/image.JPG?size=large")
    assert first == second
    assert first.parent == assets.resolve()
    assert first.suffix == ".jpg"

    linked = tmp_path / "linked-assets"
    linked.symlink_to(assets, target_is_directory=True)
    with pytest.raises(ValueError, match="unsafe asset"):
        safe_asset_path(linked, "https://example.invalid/image.jpg")


def test_article_directory_blocks_traversal_and_symlink(tmp_path: Path):
    with pytest.raises(ValueError, match="unsafe article"):
        safe_article_dir(tmp_path, "../../browser")

    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "article-1").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="unsafe article"):
        safe_article_dir(tmp_path, "article-1")


def test_atomic_write_replaces_content_and_cleans_temporary_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    destination = tmp_path / "source.md"
    atomic_write(destination, b"first")
    assert destination.read_bytes() == b"first"

    def fail_replace(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
        raise OSError("fictional replace failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="fictional replace failure"):
        atomic_write(destination, b"second")
    assert destination.read_bytes() == b"first"
    assert list(tmp_path.glob(".source.md.*")) == []


def test_stored_article_round_trip(tmp_path: Path):
    directory = tmp_path / "article-1"
    (directory / "assets").mkdir(parents=True)
    (directory / "assets" / "image.jpg").write_bytes(b"image")
    source, metadata, digest = write_source(
        directory,
        "# Fictional title\n",
        {
            "account_id": "account-1",
            "account_name": "Example Account",
            "article_id": "article-1",
            "published_at": "2026-07-31T00:00:00+00:00",
            "source_url": "https://example.invalid/article-1",
            "status": "complete",
            "title": "Fictional title",
        },
    )
    loaded = load_stored_article(directory)
    assert loaded.article.title == "Fictional title"
    assert loaded.article.published_at.isoformat() == "2026-07-31T00:00:00+00:00"
    assert loaded.status is ArticleStatus.COMPLETE
    assert loaded.source_path == source
    assert loaded.metadata_path == metadata
    assert loaded.source_sha256 == digest
    assert [path.name for path in loaded.asset_paths] == ["image.jpg"]


def test_load_stored_article_rejects_source_hash_mismatch(tmp_path: Path):
    directory = tmp_path / "article-1"
    write_source(
        directory,
        "Original fictional source.\n",
        {
            "account_id": "account-1",
            "account_name": "Example Account",
            "article_id": "article-1",
            "published_at": "2026-07-31T00:00:00+00:00",
            "source_url": "https://example.invalid/article-1",
            "status": "complete",
            "title": "Fictional title",
        },
    )
    (directory / "source.md").write_text("Changed source.\n", encoding="utf-8")
    with pytest.raises(ValueError, match="source hash mismatch"):
        load_stored_article(directory)


def test_write_source_rejects_metadata_injection_and_reserved_keys(tmp_path: Path):
    with pytest.raises(ValueError, match="unsafe metadata key"):
        write_source(tmp_path / "article-1", "source\n", {"title\n- status": "complete"})
    with pytest.raises(ValueError, match="reserved metadata key"):
        write_source(tmp_path / "article-2", "source\n", {"source_sha256": "forged"})
