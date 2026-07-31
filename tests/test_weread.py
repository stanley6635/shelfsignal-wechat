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


def test_parse_article_content_rejects_missing_required_body():
    with pytest.raises(ContentContractUnavailable, match="content body"):
        parse_article_content({"reviewId": "MP_DEMO_ARTICLE"})


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
