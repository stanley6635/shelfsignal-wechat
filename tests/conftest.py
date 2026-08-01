from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta

import pytest

from shelfsignal.models import ArticleContent, RemoteArticle, ShelfAccount

_ONE_PIXEL_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAP//////////////////////////////////////////////////////////////////////////////////////"
    "2wBDAf//////////////////////////////////////////////////////////////////////////////////////"
    "wAARCAABAAEDASIAAhEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAAAAX/xAAUEAEAAAAAAAAAAAAAAAAAAAAA"
    "/9oADAMBAAIQAxAAAAEf/8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABBQJ//8QAFBEBAAAAAAAAAAAAAAAA"
    "AAAAAP/aAAgBAwEBPwF//8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAAgBAgEBPwF//8QAFBABAAAAAAAAAAAAAAAA"
    "AAAAAP/aAAgBAQAGPwJ//8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABPyF//9oADAMBAAIAAwAAABAf/8QA"
    "FBEBAAAAAAAAAAAAAAAAAAAAAP/aAAgBAwEBPxB//8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAAgBAgEBPxB//8QA"
    "FBABAQAAAAAAAAAAAAAAAAAAABH/2gAIAQEAAT8QH//Z"
)


class FakeArticleClient:
    def __init__(self) -> None:
        self.content_calls = 0

    async def shelf(self) -> tuple[ShelfAccount, ...]:
        return (ShelfAccount("account-1", "Example Account"),)

    async def articles(self, account: ShelfAccount) -> tuple[RemoteArticle, ...]:
        now = datetime.now(UTC)
        return (
            RemoteArticle(
                "article-text",
                account.account_id,
                account.name,
                "Text article",
                "https://example.invalid/text",
                now,
            ),
            RemoteArticle(
                "article-image",
                account.account_id,
                account.name,
                "Image article",
                "https://example.invalid/image",
                now,
            ),
            RemoteArticle(
                "article-old",
                account.account_id,
                account.name,
                "Old article",
                "https://example.invalid/old",
                now - timedelta(days=30),
            ),
        )

    async def content(self, article: RemoteArticle) -> ArticleContent:
        self.content_calls += 1
        if article.article_id == "article-image":
            return ArticleContent(
                article.article_id,
                '<p>Short.</p><img width="1200" height="8000" '
                'src="https://example.invalid/long.jpg">',
                ("https://example.invalid/long.jpg",),
            )
        return ArticleContent(
            article.article_id,
            "<p>Complete fictional body with enough text for the fake run.</p>",
            (),
        )

    async def asset(self, url: str) -> bytes:
        assert url == "https://example.invalid/long.jpg"
        return _ONE_PIXEL_JPEG


@pytest.fixture
def fake_article_client() -> FakeArticleClient:
    return FakeArticleClient()
