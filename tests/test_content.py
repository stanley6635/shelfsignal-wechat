from __future__ import annotations

import os
import stat
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
    assert normalized.image_urls == (
        "https://example.invalid/bad-width.jpg",
        "https://example.invalid/repeated.jpg",
    )


def test_normalize_html_accepts_one_dimension_and_data_src_without_huge_integer_crash():
    normalized = normalize_html(
        """
        <img width="640" src="https://example.invalid/width-only.jpg">
        <img height="480" data-src="https://example.invalid/lazy.png">
        <img width="999999999999999999999999999999999999999999999999999999999999"
             src="https://example.invalid/huge.jpg">
        <img width="640" src="https://unapproved.example/image.jpg"
             data-src="https://example.invalid/ignored-fallback.jpg">
        """
    )
    assert normalized.image_urls == (
        "https://example.invalid/width-only.jpg",
        "https://example.invalid/lazy.png",
    )


@pytest.mark.parametrize(
    "source",
    [
        "../../outside.txt",
        "https://example.invalid/../outside.txt",
        "https://example.invalid/%2e%2e/outside.txt",
        "https:///missing-host.jpg",
        "file:///tmp/outside.txt",
        "https://user@example.invalid/image.jpg",
        "https://example.invalid:8443/image.jpg",
        "https://127.0.0.1/image.jpg",
        "https://localhost/image.jpg",
        "https://unapproved.example/image.jpg",
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


def test_asset_path_allows_approved_cdn_and_downgrades_unknown_extension(tmp_path: Path):
    assets = tmp_path / "assets"
    destination = safe_asset_path(assets, "https://mmbiz.qpic.cn/content.svg")
    assert destination.suffix == ".bin"
    assert stat.S_IMODE(assets.stat().st_mode) == 0o700


def test_article_directory_blocks_traversal_and_symlink(tmp_path: Path):
    with pytest.raises(ValueError, match="unsafe article"):
        safe_article_dir(tmp_path, "../../browser")

    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "article-1").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="unsafe article"):
        safe_article_dir(tmp_path, "article-1")


def test_article_directory_rejects_symlink_ancestor_without_outside_mutation(tmp_path: Path):
    trusted = tmp_path / "trusted"
    outside = tmp_path / "outside"
    outside.mkdir()
    trusted.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="unsafe article"):
        safe_article_dir(trusted, "article-1")
    assert list(outside.iterdir()) == []


def test_atomic_write_replaces_content_and_cleans_temporary_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    destination = tmp_path / "source.md"
    atomic_write(destination, b"first")
    assert destination.read_bytes() == b"first"

    def fail_replace(
        source: str | os.PathLike[str],
        target: str | os.PathLike[str],
        **kwargs: int,
    ) -> None:
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
    assert stat.S_IMODE(source.stat().st_mode) == 0o600
    assert stat.S_IMODE(metadata.stat().st_mode) == 0o600


def test_write_source_rolls_back_existing_pair_when_metadata_commit_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    directory = safe_article_dir(tmp_path, "article-1")
    fields = {
        "account_id": "account-1",
        "account_name": "Example Account",
        "article_id": "article-1",
        "published_at": "2026-07-31T00:00:00+00:00",
        "source_url": "https://example.invalid/article-1",
        "status": "complete",
        "title": "Original title",
    }
    write_source(directory, "Original source.\n", fields)
    real_replace = os.replace
    commits = 0

    def fail_second_commit(
        source: str | os.PathLike[str],
        target: str | os.PathLike[str],
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        nonlocal commits
        if os.fspath(target) in {"source.md", "metadata.md"}:
            commits += 1
            if commits == 2:
                raise OSError("fictional metadata commit failure")
        real_replace(
            source,
            target,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(os, "replace", fail_second_commit)
    with pytest.raises(OSError, match="fictional metadata commit failure"):
        write_source(directory, "Replacement source.\n", {**fields, "title": "Replacement"})
    loaded = load_stored_article(directory)
    assert loaded.article.title == "Original title"
    assert loaded.source_path.read_text(encoding="utf-8") == "Original source.\n"
    assert not [path for path in directory.iterdir() if path.name.startswith(".")]


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


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("article_id", "different-id", "article ID"),
        ("published_at", "2026-07-31T00:00:00", "metadata"),
        ("source_url", "http://example.invalid/article-1", "metadata"),
    ],
)
def test_load_rejects_invalid_identity_timestamp_and_source_url(
    tmp_path: Path, field: str, value: str, message: str
):
    directory = safe_article_dir(tmp_path, "article-1")
    fields = {
        "account_id": "account-1",
        "account_name": "Example Account",
        "article_id": "article-1",
        "published_at": "2026-07-31T00:00:00+00:00",
        "source_url": "https://example.invalid/article-1",
        "status": "complete",
        "title": "Fictional title",
    }
    fields[field] = value
    write_source(directory, "Source.\n", fields)
    with pytest.raises(ValueError, match=message):
        load_stored_article(directory)
