import json
from pathlib import Path

import pytest

from shelfsignal.errors import ContentContractUnavailable, ShelfUnavailable
from shelfsignal.models import ShelfAccount
from shelfsignal.weread import parse_article_content, parse_book_read, parse_shelf_html


def test_parse_shelf_html_returns_stable_accounts():
    html = Path("tests/fixtures/shelf.html").read_text(encoding="utf-8")
    accounts = parse_shelf_html(html)
    assert [(item.account_id, item.name) for item in accounts] == [
        ("MP_DEMO_ACCOUNT", "Example Account")
    ]


def test_parse_shelf_html_rejects_unreadable_shelf():
    with pytest.raises(ShelfUnavailable, match="empty or unreadable"):
        parse_shelf_html("<main></main>")


def test_parse_shelf_html_accumulates_nested_title_and_matches_class_tokens():
    accounts = parse_shelf_html(
        """
        <a data-book-id=" MP_DEMO_ACCOUNT " data-book-type="official-account">
          <span class="label title selected"> Example <em>Account</em><br> </span>
        </a>
        """
    )
    assert accounts == (ShelfAccount("MP_DEMO_ACCOUNT", "Example Account"),)


@pytest.mark.parametrize(
    "html",
    [
        (
            '<a data-book-id="   " data-book-type="official-account">'
            '<span class="title">Example Account</span></a>'
        ),
        (
            '<a data-book-id="MP_DEMO_ACCOUNT" data-book-type="official-account">'
            '<span class="title">   </span></a>'
        ),
    ],
)
def test_parse_shelf_html_rejects_blank_account_fields(html):
    with pytest.raises(ShelfUnavailable, match="empty or unreadable"):
        parse_shelf_html(html)


def test_parse_article_content_rejects_missing_required_body():
    with pytest.raises(ContentContractUnavailable, match="content body"):
        parse_article_content({"reviewId": "MP_DEMO_ARTICLE"})


@pytest.mark.parametrize("payload", [None, [], "payload"])
def test_parse_article_content_rejects_non_mapping_payload(payload):
    with pytest.raises(ContentContractUnavailable, match="payload"):
        parse_article_content(payload)


def test_parse_article_content_rejects_blank_id_and_preserves_body_exactly():
    with pytest.raises(ContentContractUnavailable, match="article ID"):
        parse_article_content({"reviewId": "  ", "content": "<p>body</p>"})

    body = "  <p>Fictional body.</p>\n"
    content = parse_article_content({"reviewId": " MP_DEMO_ARTICLE ", "content": body})
    assert content.article_id == "MP_DEMO_ARTICLE"
    assert content.html == body


def test_parse_article_content_normalizes_body_and_images():
    payload = json.loads(
        Path("tests/fixtures/article-content.json").read_text(encoding="utf-8")
    )
    content = parse_article_content(payload)
    assert content.article_id == "MP_DEMO_ARTICLE"
    assert content.html.startswith("<p>")
    assert content.image_urls == ("https://example.invalid/image-001.jpg",)


def test_parse_book_read_returns_remote_articles():
    payload = json.loads(
        Path("tests/fixtures/book-read.json").read_text(encoding="utf-8")
    )
    articles = parse_book_read(
        payload, ShelfAccount("MP_DEMO_ACCOUNT", "Example Account")
    )
    assert [(item.article_id, item.title) for item in articles] == [
        ("MP_DEMO_ARTICLE", "Fictional article")
    ]


@pytest.mark.parametrize("payload", [None, [], "payload"])
def test_parse_book_read_rejects_non_mapping_payload(payload):
    with pytest.raises(ContentContractUnavailable, match="payload"):
        parse_book_read(payload, ShelfAccount("MP_DEMO_ACCOUNT", "Example Account"))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("reviewId", "   "),
        ("title", "   "),
        ("url", "https:///missing-host"),
        ("url", "not-https://example.invalid/article"),
        ("publishTime", True),
        ("publishTime", 10**100),
    ],
)
def test_parse_book_read_rejects_invalid_chapter_fields(field, value):
    chapter = {
        "reviewId": "MP_DEMO_ARTICLE",
        "title": "Fictional article",
        "url": "https://example.invalid/article",
        "publishTime": 1785427200,
    }
    chapter[field] = value
    with pytest.raises(ContentContractUnavailable, match="book chapter"):
        parse_book_read(
            {"chapterInfos": [chapter]},
            ShelfAccount("MP_DEMO_ACCOUNT", "Example Account"),
        )


def test_parse_book_read_normalizes_ids_titles_and_collapses_identical_duplicates():
    chapter = {
        "reviewId": " MP_DEMO_ARTICLE ",
        "title": " Fictional article ",
        "url": "https://example.invalid/article",
        "publishTime": 1785427200,
    }
    articles = parse_book_read(
        {"chapterInfos": [chapter, dict(chapter)]},
        ShelfAccount("MP_DEMO_ACCOUNT", "Example Account"),
    )
    assert len(articles) == 1
    assert articles[0].article_id == "MP_DEMO_ARTICLE"
    assert articles[0].title == "Fictional article"


def test_parse_book_read_rejects_conflicting_duplicate_ids():
    first = {
        "reviewId": "MP_DEMO_ARTICLE",
        "title": "Fictional article",
        "url": "https://example.invalid/article",
        "publishTime": 1785427200,
    }
    second = {**first, "title": "Conflicting title"}
    with pytest.raises(ContentContractUnavailable, match="duplicate article ID"):
        parse_book_read(
            {"chapterInfos": [first, second]},
            ShelfAccount("MP_DEMO_ACCOUNT", "Example Account"),
        )
