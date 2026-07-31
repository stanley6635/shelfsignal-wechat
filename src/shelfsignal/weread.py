from __future__ import annotations

from datetime import UTC, datetime
from html.parser import HTMLParser
from urllib.parse import urlsplit

from .errors import ContentContractUnavailable, ShelfUnavailable
from .models import ArticleContent, RemoteArticle, ShelfAccount


class _ShelfParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.accounts: list[ShelfAccount] = []
        self._account_id: str | None = None
        self._title_chunks: list[str] = []
        self._title_span_nesting: int | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if (
            tag == "a"
            and self._account_id is None
            and values.get("data-book-type") == "official-account"
        ):
            raw_account_id = values.get("data-book-id")
            account_id = (
                raw_account_id.strip() if isinstance(raw_account_id, str) else ""
            )
            if account_id:
                self._account_id = account_id
        elif self._title_span_nesting is not None and tag == "span":
            self._title_span_nesting += 1
        elif (
            self._account_id
            and self._title_span_nesting is None
            and tag == "span"
            and "title" in (values.get("class") or "").split()
        ):
            self._title_chunks = []
            self._title_span_nesting = 1

    def handle_data(self, data: str) -> None:
        if self._title_span_nesting is not None:
            self._title_chunks.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "span" and self._title_span_nesting is not None:
            self._title_span_nesting -= 1
            if self._title_span_nesting == 0:
                name = " ".join("".join(self._title_chunks).split())
                if self._account_id and name:
                    self.accounts.append(ShelfAccount(self._account_id, name))
                self._title_chunks = []
                self._title_span_nesting = None
        if tag == "a":
            self._account_id = None
            self._title_chunks = []
            self._title_span_nesting = None


def parse_shelf_html(html: str) -> tuple[ShelfAccount, ...]:
    parser = _ShelfParser()
    parser.feed(html)
    if not parser.accounts:
        raise ShelfUnavailable("saved official-account shelf is empty or unreadable")
    unique = {item.account_id: item for item in parser.accounts}
    return tuple(unique[key] for key in sorted(unique))


def parse_article_content(payload: object) -> ArticleContent:
    if not isinstance(payload, dict):
        raise ContentContractUnavailable("article content payload changed type")
    article_id = payload.get("reviewId")
    body = payload.get("content")
    if not isinstance(article_id, str) or not (article_id := article_id.strip()):
        raise ContentContractUnavailable("article ID is missing")
    if not isinstance(body, str) or not body.strip():
        raise ContentContractUnavailable("article content body is missing")
    images = payload.get("images", [])
    if not isinstance(images, list) or not all(isinstance(item, str) for item in images):
        raise ContentContractUnavailable("article image list has changed type")
    return ArticleContent(article_id, body, tuple(dict.fromkeys(images)))


def parse_book_read(
    payload: object,
    account: ShelfAccount,
) -> tuple[RemoteArticle, ...]:
    if not isinstance(payload, dict):
        raise ContentContractUnavailable("book read payload changed type")
    chapters = payload.get("chapterInfos")
    if not isinstance(chapters, list):
        raise ContentContractUnavailable("book chapter list is missing")
    articles: list[RemoteArticle] = []
    articles_by_id: dict[str, RemoteArticle] = {}
    for chapter in chapters:
        if not isinstance(chapter, dict):
            raise ContentContractUnavailable("book chapter entry changed type")
        article_id = chapter.get("reviewId")
        title = chapter.get("title")
        source_url = chapter.get("url")
        published = chapter.get("publishTime")
        if not isinstance(article_id, str) or not (article_id := article_id.strip()):
            raise ContentContractUnavailable("book chapter fields are incomplete")
        if not isinstance(title, str) or not (title := title.strip()):
            raise ContentContractUnavailable("book chapter fields are incomplete")
        if not isinstance(source_url, str):
            raise ContentContractUnavailable("book chapter fields are incomplete")
        source_url = source_url.strip()
        try:
            parsed_url = urlsplit(source_url)
        except ValueError as exc:
            raise ContentContractUnavailable(
                "book chapter URL is invalid"
            ) from exc
        if parsed_url.scheme != "https" or not parsed_url.netloc:
            raise ContentContractUnavailable("book chapter URL is invalid")
        if type(published) is not int:
            raise ContentContractUnavailable("book chapter fields are incomplete")
        try:
            published_at = datetime.fromtimestamp(published, tz=UTC)
        except (OverflowError, OSError, ValueError) as exc:
            raise ContentContractUnavailable(
                "book chapter timestamp is invalid"
            ) from exc
        article = RemoteArticle(
            article_id=article_id,
            account_id=account.account_id,
            account_name=account.name,
            title=title,
            source_url=source_url,
            published_at=published_at,
        )
        existing = articles_by_id.get(article_id)
        if existing is None:
            articles_by_id[article_id] = article
            articles.append(article)
        elif existing != article:
            raise ContentContractUnavailable(
                "duplicate article ID has conflicting fields"
            )
    return tuple(articles)
