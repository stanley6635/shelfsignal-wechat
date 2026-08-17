from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

import shelfsignal.cards as cards_module
from shelfsignal.cards import build_card, write_cards
from shelfsignal.models import ArticleStatus, RemoteArticle, StoredArticle


def make_stored_article(
    root: Path,
    article_id: str,
    *,
    source_text: str = "Compact evidence.",
    title: str | None = None,
    account_name: str = "Example Account",
    published_at: datetime | None = None,
) -> StoredArticle:
    root.mkdir(parents=True, exist_ok=True)
    source = root / "source.md"
    source.write_text(source_text, encoding="utf-8")
    article = RemoteArticle(
        article_id,
        "account-1",
        account_name,
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


def test_card_removes_leading_weread_reader_header_from_excerpt(tmp_path: Path):
    title = "博士伦SeeLuma™获NMPA批准，全数字双筒目镜手术平台正式登陆中国"
    stored = make_stored_article(
        tmp_path,
        "article-1",
        title=title,
        account_name="青白视角",
        source_text=(
            f"{title}\n\n原创 青白视角 青白视角 青白视角\n\n"
            "在小说阅读器读本章 去阅读 在小说阅读器中沉浸阅读\n\n"
            "这是应当保留的正文第一段。"
        ),
    )

    card = build_card(stored)

    assert card.excerpt == "这是应当保留的正文第一段。"


def test_card_keeps_reader_words_outside_a_matching_header(tmp_path: Path):
    stored = make_stored_article(
        tmp_path,
        "article-1",
        title="示例标题",
        source_text="正文建议读者去阅读原始研究，在小说阅读器中沉浸阅读只是后文引语。",
    )

    card = build_card(stored)

    assert card.excerpt == "正文建议读者去阅读原始研究，在小说阅读器中沉浸阅读只是后文引语。"


def test_card_removes_multiline_header_with_author_byline(tmp_path: Path):
    """Regression: bylines usually repeat the author name, which differs from
    the account name (e.g. 原创 赵泓维 赵泓维 动脉网). Each chrome part sits on
    its own line in stored source files."""
    stored = make_stored_article(
        tmp_path,
        "article-1",
        title="新一轮评级即将来临，医疗信息化续命？",
        account_name="动脉网",
        source_text=(
            "新一轮评级即将来临，医疗信息化续命？\n\n"
            "原创 赵泓维 赵泓维 动脉网\n\n"
            "在小说阅读器读本章\n\n"
            "去阅读\n\n"
            "在小说阅读器中沉浸阅读\n\n"
            "撰稿 ｜ 赵泓维\n\n"
            "这是应当保留的正文第一段。"
        ),
    )

    card = build_card(stored)

    assert card.excerpt == "撰稿 ｜ 赵泓维 这是应当保留的正文第一段。"


def test_card_keeps_header_parts_without_reader_buttons(tmp_path: Path):
    """Title + byline alone (no reader buttons) is article content, not chrome."""
    stored = make_stored_article(
        tmp_path,
        "article-1",
        title="示例标题",
        account_name="示例公众号",
        source_text=(
            "示例标题\n\n"
            "原创 作者甲 作者甲 示例公众号\n\n"
            "这是应当保留的正文第一段。"
        ),
    )

    card = build_card(stored)

    assert card.excerpt == "示例标题 原创 作者甲 作者甲 示例公众号 这是应当保留的正文第一段。"


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


def test_missing_source_placeholder_respects_one_character_limit(tmp_path: Path):
    stored = make_stored_article(tmp_path, "article-1")
    stored.source_path.unlink()

    card = build_card(stored, max_characters=1)

    assert card.excerpt == "…"
    assert len(card.excerpt) == 1


@pytest.mark.parametrize("ocr_path_kind", ["none", "missing"])
def test_ocr_incomplete_status_is_visible_without_ocr_output(
    tmp_path: Path, ocr_path_kind: str
):
    stored = make_stored_article(tmp_path, "article-1")
    stored = replace(
        stored,
        ocr_path=None if ocr_path_kind == "none" else tmp_path / "ocr.md",
        status=ArticleStatus.OCR_INCOMPLETE,
    )

    card = build_card(stored)

    assert card.ocr_status in {"incomplete", "missing"}
    assert card.ocr_status != "not-needed"


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


def test_card_and_writer_bound_untrusted_metadata(tmp_path: Path):
    stored = make_stored_article(tmp_path / "article", "article-1")
    huge = "x" * 1_000_000
    stored = replace(
        stored,
        article=replace(
            stored.article,
            title=huge,
            account_name=huge,
            source_url=f"https://example.invalid/{huge}",
        ),
    )

    card = build_card(stored)

    assert card.title.endswith("…")
    assert card.account_name.endswith("…")
    assert card.source_url.endswith("…")
    direct = replace(
        card,
        title=huge,
        account_name=huge,
        source_url=huge,
        source_path=Path(huge),
        excerpt=huge,
        ocr_status=huge,
        retrieval_status=huge,
    )
    output = write_cards((direct,), tmp_path / "cards.md").read_bytes()
    assert len(output) <= 32 * 1024
    assert b"\xe2\x80\xa6" in output


def test_write_cards_has_card_count_and_total_artifact_budgets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    stored = make_stored_article(tmp_path / "article", "article-1")
    card = build_card(stored)
    too_many = tuple(replace(card, article_id=f"article-{index}") for index in range(2001))
    with pytest.raises(ValueError, match="too many reading cards"):
        write_cards(too_many, tmp_path / "many.md")

    monkeypatch.setattr(cards_module, "_MAX_ARTIFACT_BYTES", 200)
    with pytest.raises(ValueError, match="artifact is too large"):
        write_cards((card,), tmp_path / "large.md")


def test_write_cards_sorts_by_utc_instant_and_rejects_naive_datetime(tmp_path: Path):
    card = build_card(make_stored_article(tmp_path / "article", "article-base"))
    later_local_date = replace(
        card,
        article_id="later-local-date",
        published_at=datetime(2026, 8, 1, 0, 30, tzinfo=timezone(timedelta(hours=8))),
    )
    earlier_local_date = replace(
        card,
        article_id="earlier-local-date",
        published_at=datetime(2026, 7, 31, 17, 0, tzinfo=UTC),
    )

    text = write_cards(
        (earlier_local_date, later_local_date), tmp_path / "ordered.md"
    ).read_text(encoding="utf-8")
    assert text.index("## later-local-date") < text.index("## earlier-local-date")

    naive = replace(card, published_at=card.published_at.replace(tzinfo=None))
    with pytest.raises(ValueError, match="timezone-aware"):
        write_cards((naive,), tmp_path / "naive.md")


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
