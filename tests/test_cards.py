from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from shelfsignal.cards import build_card, write_cards
from shelfsignal.models import ArticleStatus, RemoteArticle, StoredArticle


def make_stored_article(
    root: Path,
    article_id: str,
    *,
    source_text: str = "Compact evidence.",
    title: str | None = None,
    published_at: datetime | None = None,
) -> StoredArticle:
    root.mkdir(parents=True, exist_ok=True)
    source = root / "source.md"
    source.write_text(source_text, encoding="utf-8")
    article = RemoteArticle(
        article_id,
        "account-1",
        "Example Account",
        title or f"Title {article_id}",
        f"https://example.invalid/{article_id}",
        published_at or datetime(2026, 7, 31, tzinfo=UTC),
    )
    return StoredArticle(
        article,
        root,
        source,
        root / "metadata.md",
        (),
        None,
        "abc",
        ArticleStatus.COMPLETE,
    )


def test_card_excerpt_is_bounded(tmp_path: Path):
    stored = make_stored_article(tmp_path, "article-1", source_text="证据" * 1000)

    card = build_card(stored, max_characters=800)

    assert len(card.excerpt) <= 800
    assert card.article_id == "article-1"
    assert card.excerpt.endswith("…")


@pytest.mark.parametrize("limit", [True, 0, -1, 10_001, 1.5, "800"])
def test_card_rejects_malformed_max_characters(tmp_path: Path, limit: object):
    stored = make_stored_article(tmp_path, "article-1")

    with pytest.raises(ValueError, match="max_characters"):
        build_card(stored, max_characters=limit)  # type: ignore[arg-type]


def test_card_reports_missing_source_and_ocr(tmp_path: Path):
    stored = make_stored_article(tmp_path, "article-1")
    stored.source_path.unlink()
    missing_ocr = tmp_path / "ocr.md"
    stored = StoredArticle(
        stored.article,
        stored.directory,
        stored.source_path,
        stored.metadata_path,
        stored.asset_paths,
        missing_ocr,
        stored.source_sha256,
        ArticleStatus.OCR_INCOMPLETE,
    )

    card = build_card(stored)

    assert card.excerpt == "[Source unavailable: missing]"
    assert card.ocr_status == "missing"
    assert card.retrieval_status == "ocr_incomplete; source-missing"


def test_card_reports_incomplete_and_available_ocr(tmp_path: Path):
    stored = make_stored_article(tmp_path / "a", "article-a")
    incomplete = stored.directory / "ocr.md"
    incomplete.write_text("# OCR evidence\n\nOCR incomplete: failed\n", encoding="utf-8")
    stored = StoredArticle(
        stored.article,
        stored.directory,
        stored.source_path,
        stored.metadata_path,
        stored.asset_paths,
        incomplete,
        stored.source_sha256,
        stored.status,
    )
    assert build_card(stored).ocr_status == "incomplete"

    complete = make_stored_article(tmp_path / "b", "article-b")
    ocr = complete.directory / "ocr.md"
    ocr.write_text("# OCR evidence\n\nReadable text\n", encoding="utf-8")
    complete = StoredArticle(
        complete.article,
        complete.directory,
        complete.source_path,
        complete.metadata_path,
        complete.asset_paths,
        ocr,
        complete.source_sha256,
        complete.status,
    )
    assert build_card(complete).ocr_status == "available"


def test_card_rejects_unsafe_source_and_ocr_paths(tmp_path: Path):
    stored = make_stored_article(tmp_path / "article", "article-1")
    outside = tmp_path / "outside.md"
    outside.write_text("secret", encoding="utf-8")
    stored.source_path.unlink()
    stored.source_path.symlink_to(outside)
    with pytest.raises(ValueError, match="unsafe source"):
        build_card(stored)

    stored = make_stored_article(tmp_path / "other", "article-2")
    ocr = stored.directory / "ocr.md"
    ocr.symlink_to(outside)
    unsafe_ocr = StoredArticle(
        stored.article,
        stored.directory,
        stored.source_path,
        stored.metadata_path,
        stored.asset_paths,
        ocr,
        stored.source_sha256,
        stored.status,
    )
    with pytest.raises(ValueError, match="unsafe OCR"):
        build_card(unsafe_ocr)


def test_card_rejects_oversized_ocr(tmp_path: Path):
    stored = make_stored_article(tmp_path, "article-1")
    ocr = tmp_path / "ocr.md"
    ocr.write_bytes(b"x" * (8 * 1024 * 1024 + 1))
    stored = StoredArticle(
        stored.article,
        stored.directory,
        stored.source_path,
        stored.metadata_path,
        stored.asset_paths,
        ocr,
        stored.source_sha256,
        stored.status,
    )

    with pytest.raises(ValueError, match="OCR file is too large"):
        build_card(stored)


def test_card_bounds_oversized_source_scan_and_marks_retrieval(tmp_path: Path):
    stored = make_stored_article(tmp_path, "article-1")
    with stored.source_path.open("ab") as stream:
        stream.truncate(8 * 1024 * 1024 + 1)

    card = build_card(stored, max_characters=80)

    assert len(card.excerpt) <= 80
    assert card.retrieval_status == "complete; source-scan-truncated"


def test_write_cards_includes_each_id_once_and_sorts_deterministically(tmp_path: Path):
    first = make_stored_article(
        tmp_path / "a",
        "article-a",
        published_at=datetime(2026, 7, 30, tzinfo=UTC),
    )
    second = make_stored_article(tmp_path / "b", "article-b")

    path = write_cards(
        (build_card(second), build_card(first)),
        tmp_path / "cards.md",
    )
    text = path.read_text(encoding="utf-8")

    assert text.count("## article-a") == 1
    assert text.count("## article-b") == 1
    assert text.index("## article-a") < text.index("## article-b")


def test_write_cards_escapes_metadata_markdown_injection(tmp_path: Path):
    stored = make_stored_article(
        tmp_path / "article",
        "article-1",
        title="Safe title\n## injected heading",
    )

    path = write_cards((build_card(stored),), tmp_path / "cards.md")
    text = path.read_text(encoding="utf-8")

    assert "\n## injected heading" not in text
    assert '"Safe title\\n## injected heading"' in text


def test_write_cards_rejects_duplicate_ids(tmp_path: Path):
    stored = make_stored_article(tmp_path / "article", "article-1")
    card = build_card(stored)

    with pytest.raises(ValueError, match="duplicate reading card"):
        write_cards((card, card), tmp_path / "cards.md")


def test_write_cards_rejects_symlink_target(tmp_path: Path):
    stored = make_stored_article(tmp_path / "article", "article-1")
    outside = tmp_path / "outside.md"
    outside.write_text("keep", encoding="utf-8")
    target = tmp_path / "cards.md"
    target.symlink_to(outside)

    with pytest.raises(ValueError, match="unsafe stored article file"):
        write_cards((build_card(stored),), target)
    assert outside.read_text(encoding="utf-8") == "keep"
