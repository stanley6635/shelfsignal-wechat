from __future__ import annotations

from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any

from .errors import ContentContractUnavailable, ShelfUnavailable
from .models import ArticleContent, RemoteArticle, ShelfAccount


class _ShelfParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.accounts: list[ShelfAccount] = []
        self._account_id: str | None = None
        self._inside_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "a" and values.get("data-book-type") == "official-account":
            self._account_id = values.get("data-book-id")
        elif self._account_id and tag == "span" and values.get("class") == "title":
            self._inside_title = True

    def handle_data(self, data: str) -> None:
        if self._inside_title and self._account_id and data.strip():
            self.accounts.append(ShelfAccount(self._account_id, data.strip()))

    def handle_endtag(self, tag: str) -> None:
        if tag == "span":
            self._inside_title = False
        elif tag == "a":
            self._account_id = None


def parse_shelf_html(html: str) -> tuple[ShelfAccount, ...]:
    parser = _ShelfParser()
    parser.feed(html)
    if not parser.accounts:
        raise ShelfUnavailable("saved official-account shelf is empty or unreadable")
    unique = {item.account_id: item for item in parser.accounts}
    return tuple(unique[key] for key in sorted(unique))


def parse_article_content(payload: dict[str, Any]) -> ArticleContent:
    article_id = payload.get("reviewId")
    body = payload.get("content")
    if not isinstance(article_id, str) or not article_id:
        raise ContentContractUnavailable("article ID is missing")
    if not isinstance(body, str) or not body.strip():
        raise ContentContractUnavailable("article content body is missing")
    images = payload.get("images", [])
    if not isinstance(images, list) or not all(isinstance(item, str) for item in images):
        raise ContentContractUnavailable("article image list has changed type")
    return ArticleContent(article_id, body, tuple(dict.fromkeys(images)))


def parse_book_read(
    payload: dict[str, Any],
    account: ShelfAccount,
) -> tuple[RemoteArticle, ...]:
    chapters = payload.get("chapterInfos")
    if not isinstance(chapters, list):
        raise ContentContractUnavailable("book chapter list is missing")
    articles: list[RemoteArticle] = []
    for chapter in chapters:
        if not isinstance(chapter, dict):
            raise ContentContractUnavailable("book chapter entry changed type")
        article_id = chapter.get("reviewId")
        title = chapter.get("title")
        source_url = chapter.get("url")
        published = chapter.get("publishTime")
        if not (
            isinstance(article_id, str)
            and isinstance(title, str)
            and isinstance(source_url, str)
            and source_url.startswith("https://")
            and isinstance(published, int)
        ):
            raise ContentContractUnavailable("book chapter fields are incomplete")
        articles.append(
            RemoteArticle(
                article_id=article_id,
                account_id=account.account_id,
                account_name=account.name,
                title=title,
                source_url=source_url,
                published_at=datetime.fromtimestamp(published, tz=UTC),
            )
        )
    return tuple(articles)
