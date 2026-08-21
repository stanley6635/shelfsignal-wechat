from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from html import unescape
from html.parser import HTMLParser
from typing import Protocol
from urllib.parse import parse_qs, urlsplit

from playwright.async_api import BrowserContext, Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from .content import _safe_https_url
from .errors import (
    ArticleBodyUnavailable,
    AuthRequired,
    ContentContractUnavailable,
    ShelfUnavailable,
)
from .models import ArticleContent, RemoteArticle, ShelfAccount

SHELF_URL = "https://weread.qq.com/web/shelf"
COVER_URL = "https://weread.qq.com/web/mp/cover"
MP_CONTENT_URL = "https://weread.qq.com/web/mp/content"
BOOKREAD_URL = "https://weread.qq.com/web/mp/articles"
ARTICLES_PER_ACCOUNT = 3
MAX_ASSET_BYTES = 25 * 1024 * 1024
MAX_ARTICLE_HTML_BYTES = 25 * 1024 * 1024
SHELF_RENDER_TIMEOUT_MS = 30_000
SHELF_ACCOUNT_SELECTOR = (
    'a[data-book-type="official-account"], '
    'a.shelfBook[href*="/web/mp/reader/"]:not(.shelfBook_add)'
)
_ACCOUNT_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,256}")
_SAFE_LOCAL_ARTICLE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:~-]{0,127}")
_PUBLISH_TIME_PATTERN = re.compile(
    r"(?:\bct\b|\bpublish_time\b)\s*[:=]\s*['\"]?(\d{10})"
)
_SOURCE_FIELD_PATTERN = re.compile(
    r"(?:\bmsg_link\b|\bsource_url\b|\bcontent_url\b)\s*[:=]\s*(\"(?:\\.|[^\"])*\")"
)
_UNICODE_ESCAPE_PATTERN = re.compile(r"\\u([0-9A-Fa-f]{4})")
_VOID_HTML_TAGS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}

_MIME_KINDS = {
    "image/avif": {"avif"},
    "image/bmp": {"bmp"},
    "image/gif": {"gif"},
    "image/heic": {"heic"},
    "image/heif": {"heic"},
    "image/jpeg": {"jpeg"},
    "image/jpg": {"jpeg"},
    "image/png": {"png"},
    "image/tiff": {"tiff"},
    "image/webp": {"webp"},
}


class ArticleClient(Protocol):
    async def shelf(self) -> tuple[ShelfAccount, ...]: ...

    async def articles(self, account: ShelfAccount) -> tuple[RemoteArticle, ...]: ...

    async def content(self, article: RemoteArticle) -> ArticleContent: ...

    async def asset(self, url: str) -> bytes: ...


@dataclass(frozen=True)
class _LatestSeed:
    account: ShelfAccount
    article_id: str
    review_id: str
    title: str


class PlaywrightWeReadClient:
    coverage_warning = None

    def __init__(
        self,
        context: BrowserContext,
        page: Page,
        *,
        lookback_days: int | None = None,
    ):
        self.context = context
        self.page = page
        # Kept as an internal compatibility argument for callers from v0.1;
        # collection is now governed by the fixed three-article window.
        _ = lookback_days
        self.coverage_warning: str | None = None
        self._latest_by_account: dict[str, _LatestSeed] = {}
        self._content_by_article: dict[str, ArticleContent] = {}
        self._review_by_article: dict[str, str] = {}

    async def shelf(self) -> tuple[ShelfAccount, ...]:
        try:
            response = await self.page.goto(SHELF_URL, wait_until="domcontentloaded")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise ShelfUnavailable("saved shelf request failed") from exc
        _validate_shelf_navigation(response, getattr(self.page, "url", ""))
        for attempt in range(2):
            dom_accounts, book_ids = await self._shelf_snapshot()
            if book_ids is None:
                return await self._stable_official_accounts(dom_accounts)
            if len(book_ids) == len(dom_accounts):
                break
            if attempt == 1:
                raise ShelfUnavailable(
                    "saved shelf account count does not match its cover responses"
                )
            try:
                response = await self.page.reload(wait_until="domcontentloaded")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise ShelfUnavailable("saved shelf refresh failed") from exc
            _validate_shelf_navigation(response, getattr(self.page, "url", ""))
        latest: dict[str, _LatestSeed] = {}
        for book_id in book_ids:
            response = await self.context.request.get(COVER_URL, params={"bookId": book_id})
            _validate_api_response(response, "/web/mp/cover", "mp/cover")
            if not response.ok:
                raise ContentContractUnavailable(
                    f"mp/cover returned HTTP {response.status}"
                )
            payload = await _response_json(response, "mp/cover")
            _reject_cover_rate_limit(payload, book_id)
            seed = parse_cover_payload(payload, book_id)
            latest[book_id] = seed
            self._review_by_article[seed.article_id] = seed.review_id
        self._latest_by_account = latest
        return tuple(latest[key].account for key in sorted(latest))

    async def _shelf_snapshot(
        self,
    ) -> tuple[tuple[ShelfAccount, ...], tuple[str, ...] | None]:
        try:
            await self.page.wait_for_selector(
                SHELF_ACCOUNT_SELECTOR,
                state="attached",
                timeout=SHELF_RENDER_TIMEOUT_MS,
            )
        except asyncio.CancelledError:
            raise
        except PlaywrightTimeoutError as exc:
            raise ShelfUnavailable(
                "saved official-account shelf is empty or unreadable"
            ) from exc
        except Exception as exc:
            raise ShelfUnavailable("saved shelf could not finish rendering") from exc
        try:
            await self.page.wait_for_load_state("networkidle", timeout=5_000)
        except (PlaywrightTimeoutError, AttributeError):
            pass
        try:
            html = await self.page.content()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise ShelfUnavailable("saved shelf content could not be read") from exc
        dom_accounts = parse_shelf_html(html)
        if 'data-book-type="official-account"' in html:
            return dom_accounts, None
        try:
            resource_urls = await self.page.evaluate(
                "performance.getEntriesByType('resource').map((entry) => entry.name)"
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise ShelfUnavailable("saved shelf cover requests could not be read") from exc
        return dom_accounts, parse_cover_book_ids(resource_urls)

    async def _stable_official_accounts(
        self, first: tuple[ShelfAccount, ...]
    ) -> tuple[ShelfAccount, ...]:
        previous_ids = {account.account_id for account in first}
        for _ in range(2):
            try:
                response = await self.page.reload(wait_until="domcontentloaded")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise ShelfUnavailable("saved shelf stability check failed") from exc
            _validate_shelf_navigation(response, getattr(self.page, "url", ""))
            current, _ = await self._shelf_snapshot()
            current_ids = {account.account_id for account in current}
            if current_ids == previous_ids:
                return current
            previous_ids = current_ids
        raise ShelfUnavailable("saved official-account shelf did not stabilize")

    async def articles(self, account: ShelfAccount) -> tuple[RemoteArticle, ...]:
        try:
            articles = await self._articles_from_bookread(account)
        except AuthRequired:
            raise
        except Exception:  # noqa: BLE001 - visible fallback preserves partial results
            articles = ()
        if articles:
            return articles
        self.coverage_warning = (
            "latest-three list unavailable; attempted the latest article fallback "
            "for one or more accounts"
        )
        return await self._articles_from_cover(account)

    async def _articles_from_bookread(self, account: ShelfAccount) -> tuple[RemoteArticle, ...]:
        # Web (cookie-authenticated) article list API. The i.weread.qq.com
        # /book/articles endpoint requires client-side skey/vid headers and
        # returns -2012 (登录超时) for browser cookies, so the web API is
        # used instead: GET /web/mp/articles?bookId=MP_WXS_xxx&offset=N
        articles: list[RemoteArticle] = []
        offset = 0
        max_pages = 5  # bounded defensive pagination; normally returns on page one
        while offset < max_pages * 20:
            response = await self.context.request.get(
                BOOKREAD_URL,
                params={
                    "bookId": account.account_id,
                    "offset": str(offset),
                },
            )
            _validate_api_response(response, "/web/mp/articles", "web/mp/articles")
            if not response.ok:
                raise ContentContractUnavailable(
                    f"web/mp/articles returned HTTP {response.status}"
                )
            payload = await _response_json(response, "web/mp/articles")
            reviews = payload.get("reviews")
            if not isinstance(reviews, list) or not reviews:
                break
            for item in reviews:
                if not isinstance(item, dict):
                    continue
                sub_reviews = item.get("subReviews")
                if not isinstance(sub_reviews, list) or not sub_reviews:
                    continue
                sub = sub_reviews[0]
                if not isinstance(sub, dict):
                    continue
                review = sub.get("review")
                if not isinstance(review, dict):
                    continue
                article_id = review.get("reviewId")
                mp_info = review.get("mpInfo")
                if not isinstance(mp_info, dict):
                    continue
                title = mp_info.get("title")
                publish_time = mp_info.get("time")
                if not isinstance(article_id, str) or not (article_id := article_id.strip()):
                    continue
                if not isinstance(title, str) or not (title := title.strip()):
                    continue
                if type(publish_time) is not int:
                    continue
                try:
                    published_at = datetime.fromtimestamp(publish_time, tz=UTC)
                except (OverflowError, OSError, ValueError):
                    continue
                # mpInfo has no mp.weixin.qq.com URL; fetch content to
                # resolve the real source URL for dedup.
                source_url = ""
                try:
                    content = await self._fetch_content(article_id, article_id)
                    _, source_url = parse_latest_html_metadata(content.html)
                except Exception:  # noqa: BLE001 - body retrieval reports later per article
                    source_url = ""
                articles.append(RemoteArticle(
                    article_id=article_id,
                    account_id=account.account_id,
                    account_name=account.name,
                    title=title,
                    source_url=source_url,
                    published_at=published_at,
                ))
                if len(articles) == ARTICLES_PER_ACCOUNT:
                    return tuple(articles)
            if len(reviews) < 20:
                break
            offset += 20
        return tuple(articles)

    async def _articles_from_cover(self, account: ShelfAccount) -> tuple[RemoteArticle, ...]:
        seed = self._latest_by_account.get(account.account_id)
        if seed is None:
            raise ContentContractUnavailable("latest cover is missing for shelf account")
        content = await self._fetch_content(seed.review_id, seed.article_id)
        try:
            published_at, source_url = parse_latest_html_metadata(content.html)
        except ContentContractUnavailable as exc:
            raise ArticleBodyUnavailable(
                f"latest article metadata unavailable for shelf account: {account.account_id}"
            ) from exc
        return (
            RemoteArticle(
                article_id=seed.article_id,
                account_id=account.account_id,
                account_name=account.name,
                title=seed.title,
                source_url=source_url,
                published_at=published_at,
            ),
        )

    async def content(self, article: RemoteArticle) -> ArticleContent:
        cached = self._content_by_article.get(article.article_id)
        if cached is not None:
            return cached
        review_id = self._review_by_article.get(article.article_id, article.article_id)
        return await self._fetch_content(review_id, article.article_id)

    async def _fetch_content(self, review_id: str, article_id: str) -> ArticleContent:
        response = await self.context.request.get(
            MP_CONTENT_URL,
            params={"reviewId": review_id},
        )
        _validate_api_response(response, "/web/mp/content", "mp/content")
        if response.status == 404:
            raise ArticleBodyUnavailable(
                f"article body unavailable: {article_id}"
            )
        if not response.ok:
            raise ContentContractUnavailable(
                f"mp/content returned HTTP {response.status}"
            )
        content_type = getattr(response, "headers", {}).get("content-type", "")
        if isinstance(content_type, str) and "json" in content_type.lower():
            content = parse_article_content(await _response_json(response, "mp/content"))
            if content.article_id != review_id:
                raise ContentContractUnavailable("article content ID does not match request")
            content = ArticleContent(article_id, content.html, content.image_urls)
        else:
            body = await _response_text(response, "mp/content")
            content = ArticleContent(article_id, body, ())
        self._content_by_article[article_id] = content
        return content

    async def asset(self, url: str) -> bytes:
        if not _safe_https_url(url, image=True):
            raise OSError("unsafe asset URL")
        response = await self.context.request.get(url, max_redirects=0)
        status = getattr(response, "status", None)
        ok = getattr(response, "ok", None)
        headers = getattr(response, "headers", None)
        if type(status) is not int or not isinstance(ok, bool) or not isinstance(headers, dict):
            raise OSError("asset returned an invalid response type")
        final_url = getattr(response, "url", "")
        if not isinstance(final_url, str) or not _safe_https_url(final_url, image=True):
            raise OSError("asset returned an unsafe final URL")
        if status in {401, 403}:
            raise AuthRequired("WeRead asset authorization is required")
        if not ok:
            raise OSError(f"asset returned HTTP {status}")

        raw_content_type = headers.get("content-type", "")
        if not isinstance(raw_content_type, str):
            raise OSError("asset returned an unsupported content type")
        content_type = raw_content_type.split(";", 1)[0].strip().lower()
        expected_kinds = _MIME_KINDS.get(content_type)
        if expected_kinds is None:
            raise OSError("asset returned an unsupported content type")
        content_length = headers.get("content-length")
        if content_length is not None and not isinstance(content_length, str):
            raise OSError("asset returned an invalid content length")
        _validate_content_length(content_length)

        body = await response.body()
        if not isinstance(body, bytes):
            raise OSError("asset returned an invalid response body")
        if len(body) > MAX_ASSET_BYTES:
            raise OSError("asset exceeds the download size limit")
        kind = _raster_kind(body)
        if kind is None or kind not in expected_kinds:
            raise OSError("asset content signature does not match its content type")
        return body


async def _response_json(response: object, label: str) -> object:
    try:
        return await response.json()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise ContentContractUnavailable(f"{label} returned invalid JSON") from exc


async def _response_text(response: object, label: str) -> str:
    try:
        body = await response.text()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise ContentContractUnavailable(f"{label} returned unreadable text") from exc
    if not isinstance(body, str) or not body.strip():
        raise ContentContractUnavailable(f"{label} returned an empty body")
    if len(body.encode("utf-8")) > MAX_ARTICLE_HTML_BYTES:
        raise ContentContractUnavailable(f"{label} exceeded the body size limit")
    return body


def _validate_shelf_navigation(response: object, page_url: object) -> None:
    if response is None:
        raise ShelfUnavailable("saved shelf request failed: no response")
    response_path = _trusted_path(
        getattr(response, "url", ""),
        {"/web/shelf", "/web/login"},
        ShelfUnavailable,
        "saved shelf returned an unexpected endpoint",
    )
    page_path = _trusted_path(
        page_url,
        {"/web/shelf", "/web/login"},
        ShelfUnavailable,
        "saved shelf returned an unexpected endpoint",
    )
    status = getattr(response, "status", None)
    ok = getattr(response, "ok", None)
    if type(status) is not int or not isinstance(ok, bool):
        raise ShelfUnavailable("saved shelf returned an invalid response")
    if response_path == "/web/login" or page_path == "/web/login" or status in {401, 403}:
        raise AuthRequired("WeRead authorization is required")
    if not ok:
        raise ShelfUnavailable(f"saved shelf request failed: HTTP {status}")


def _validate_api_response(response: object, expected_path: str, label: str) -> None:
    path = _trusted_path(
        getattr(response, "url", ""),
        {expected_path, "/web/login"},
        ContentContractUnavailable,
        f"{label} returned an unexpected endpoint",
    )
    status = getattr(response, "status", None)
    ok = getattr(response, "ok", None)
    if type(status) is not int or not isinstance(ok, bool):
        raise ContentContractUnavailable(f"{label} returned an invalid response")
    if path == "/web/login" or status in {401, 403}:
        raise AuthRequired("WeRead authorization is required")


def _trusted_path(
    url: object,
    allowed_paths: set[str],
    error_type: type[Exception],
    message: str,
) -> str:
    if not isinstance(url, str):
        raise error_type(message)
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise error_type(message) from exc
    if (
        parsed.scheme != "https"
        or not (parsed.netloc == "weread.qq.com" or parsed.netloc.endswith(".weread.qq.com"))
        or parsed.path not in allowed_paths
    ):
        raise error_type(message)
    return parsed.path


def _validate_content_length(value: str | None) -> None:
    if value is None:
        return
    try:
        size = int(value)
    except (TypeError, ValueError) as exc:
        raise OSError("asset returned an invalid content length") from exc
    if size < 0 or size > MAX_ASSET_BYTES:
        raise OSError("asset exceeds the download size limit")


def _raster_kind(content: bytes) -> str | None:
    if content.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "webp"
    if content.startswith(b"BM"):
        return "bmp"
    if content.startswith((b"II*\x00", b"MM\x00*")):
        return "tiff"
    if len(content) >= 12 and content[4:8] == b"ftyp":
        brand = content[8:12]
        if brand in {b"avif", b"avis"}:
            return "avif"
        if brand in {b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1"}:
            return "heic"
    return None


def parse_cover_book_ids(resource_urls: object) -> tuple[str, ...]:
    if not isinstance(resource_urls, list) or not all(
        isinstance(item, str) for item in resource_urls
    ):
        raise ShelfUnavailable("saved shelf resource list changed type")
    book_ids: set[str] = set()
    for url in resource_urls:
        try:
            parsed = urlsplit(url)
        except ValueError:
            continue
        if (
            parsed.scheme != "https"
            or parsed.netloc != "weread.qq.com"
            or parsed.path != "/web/mp/cover"
            or parsed.fragment
        ):
            continue
        try:
            query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
        except ValueError:
            continue
        if set(query) != {"bookId"} or len(query["bookId"]) != 1:
            continue
        book_id = query["bookId"][0]
        if _ACCOUNT_ID_PATTERN.fullmatch(book_id):
            book_ids.add(book_id)
    if not book_ids:
        raise ShelfUnavailable("saved shelf did not expose readable cover responses")
    return tuple(sorted(book_ids))


def _reject_cover_rate_limit(payload: object, book_id: str) -> None:
    """Fail loudly on WeRead rate limiting instead of confusing parse errors.

    WeRead's mp/cover endpoint returns errCode -2014 ("请求频率过高") when
    the account is throttled. Without this check the payload would fail
    parse_cover_payload with a misleading "account name is missing".
    """
    if not isinstance(payload, dict):
        return
    err_code = payload.get("errCode")
    err_msg = payload.get("errMsg")
    if err_code == -2014:
        raise ContentContractUnavailable(
            f"mp/cover rate limited for {book_id}: {err_msg}"
        )


def parse_cover_payload(payload: object, book_id: str) -> _LatestSeed:
    if not _ACCOUNT_ID_PATTERN.fullmatch(book_id):
        raise ContentContractUnavailable("cover book ID is invalid")
    if not isinstance(payload, dict):
        raise ContentContractUnavailable("cover payload changed type")
    name = payload.get("name")
    title = payload.get("title")
    article_id = payload.get("reviewId")
    if not isinstance(name, str) or not (name := " ".join(name.split())):
        raise ContentContractUnavailable("cover account name is missing")
    if not isinstance(title, str) or not (title := " ".join(title.split())):
        raise ContentContractUnavailable("cover article title is missing")
    if not isinstance(article_id, str):
        raise ContentContractUnavailable(
            f"cover article ID changed type: {type(article_id).__name__}"
        )
    article_id = article_id.strip()
    if not article_id:
        raise ContentContractUnavailable("cover article ID is empty")
    if len(article_id) > 512 or any(ord(character) < 32 for character in article_id):
        raise ContentContractUnavailable("cover article ID is invalid")
    if max(len(name), len(title)) > 2_000:
        raise ContentContractUnavailable("cover text exceeds the size limit")
    account = ShelfAccount(book_id, name)
    local_id = _local_article_id(article_id)
    return _LatestSeed(account, local_id, article_id, title)


def _local_article_id(review_id: str) -> str:
    if _SAFE_LOCAL_ARTICLE_ID.fullmatch(review_id):
        return review_id
    digest = hashlib.sha256(review_id.encode("utf-8")).hexdigest()
    return f"review-{digest[:24]}"


class _SourceURLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.source_url: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "meta" or self.source_url is not None:
            return
        values = dict(attrs)
        key = values.get("property") or values.get("name")
        content = values.get("content")
        if key in {"og:url", "article:url"} and isinstance(content, str):
            self.source_url = unescape(content.strip())


def parse_latest_html_metadata(html: object) -> tuple[datetime, str]:
    if not isinstance(html, str) or not html.strip():
        raise ContentContractUnavailable("article HTML is empty")
    if len(html.encode("utf-8")) > MAX_ARTICLE_HTML_BYTES:
        raise ContentContractUnavailable("article HTML exceeds the size limit")
    timestamp_match = _PUBLISH_TIME_PATTERN.search(html)
    if timestamp_match is None:
        raise ContentContractUnavailable("article publication time is missing")
    try:
        published_at = datetime.fromtimestamp(int(timestamp_match.group(1)), tz=UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise ContentContractUnavailable("article publication time is invalid") from exc

    parser = _SourceURLParser()
    parser.feed(html)
    source_url = parser.source_url
    if source_url is None:
        match = _SOURCE_FIELD_PATTERN.search(html)
        if match is not None:
            try:
                decoded = json.loads(match.group(1))
            except (json.JSONDecodeError, TypeError):
                decoded = None
            if isinstance(decoded, str):
                source_url = unescape(decoded.strip())
    if not isinstance(source_url, str):
        raise ContentContractUnavailable("article source URL is missing")
    source_url = _decode_url_escapes(source_url)
    try:
        parsed = urlsplit(source_url)
    except ValueError as exc:
        raise ContentContractUnavailable("article source URL is invalid") from exc
    if parsed.scheme != "https" or not parsed.netloc:
        raise ContentContractUnavailable("article source URL is invalid")
    return published_at, source_url


def _decode_url_escapes(value: str) -> str:
    return _UNICODE_ESCAPE_PATTERN.sub(
        lambda match: chr(int(match.group(1), 16)), value
    ).replace(r"\x26", "&")


class _ShelfParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.accounts: list[ShelfAccount] = []
        self._account_id: str | None = None
        self._title_chunks: list[str] = []
        self._title_nesting: int | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "a" and self._account_id is None:
            account_id = ""
            if values.get("data-book-type") == "official-account":
                raw_account_id = values.get("data-book-id")
                account_id = (
                    raw_account_id.strip()
                    if isinstance(raw_account_id, str)
                    else ""
                )
            elif "shelfBook" in (values.get("class") or "").split():
                account_id = _account_id_from_shelf_link(values.get("href")) or ""
            if account_id:
                self._account_id = account_id
        elif self._title_nesting is not None and tag not in _VOID_HTML_TAGS:
            self._title_nesting += 1
        elif (
            self._account_id
            and self._title_nesting is None
            and "title" in (values.get("class") or "").split()
        ):
            self._title_chunks = []
            self._title_nesting = 1

    def handle_data(self, data: str) -> None:
        if self._title_nesting is not None:
            self._title_chunks.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag not in _VOID_HTML_TAGS and self._title_nesting is not None:
            self._title_nesting -= 1
            if self._title_nesting == 0:
                name = " ".join("".join(self._title_chunks).split())
                if self._account_id and name:
                    self.accounts.append(ShelfAccount(self._account_id, name))
                self._title_chunks = []
                self._title_nesting = None
        if tag == "a":
            self._account_id = None
            self._title_chunks = []
            self._title_nesting = None


def parse_shelf_html(html: str) -> tuple[ShelfAccount, ...]:
    parser = _ShelfParser()
    parser.feed(html)
    if not parser.accounts:
        raise ShelfUnavailable("saved official-account shelf is empty or unreadable")
    unique = {item.account_id: item for item in parser.accounts}
    return tuple(unique[key] for key in sorted(unique))


def _account_id_from_shelf_link(href: str | None) -> str | None:
    if not isinstance(href, str):
        return None
    try:
        parsed = urlsplit(href.strip())
    except ValueError:
        return None
    if (parsed.scheme or parsed.netloc) and (
        parsed.scheme != "https" or parsed.netloc != "weread.qq.com"
    ):
        return None
    prefix = "/web/mp/reader/"
    if parsed.query or parsed.fragment or parsed.path.count("/") != prefix.count("/"):
        return None
    if not parsed.path.startswith(prefix):
        return None
    account_id = parsed.path.removeprefix(prefix)
    return account_id if _ACCOUNT_ID_PATTERN.fullmatch(account_id) else None


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
