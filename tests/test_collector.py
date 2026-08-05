from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from shelfsignal.collector import collect_articles
from shelfsignal.errors import (
    ArticleBodyUnavailable,
    AuthRequired,
    ContentContractUnavailable,
    ShelfUnavailable,
)
from shelfsignal.models import (
    ArticleContent,
    ArticleStatus,
    RemoteArticle,
    ShelfAccount,
)
from shelfsignal.weread import (
    PlaywrightWeReadClient,
    parse_cover_book_ids,
    parse_cover_payload,
    parse_latest_html_metadata,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"fictional-pixels"


class FakeClient:
    def __init__(self) -> None:
        self.content_calls: list[str] = []

    async def shelf(self):
        return (ShelfAccount("account-1", "Example Account"),)

    async def articles(self, account):
        now = datetime.now(UTC)
        return (
            RemoteArticle(
                "article-1",
                account.account_id,
                account.name,
                "Recent",
                "https://example.invalid/recent",
                now,
            ),
            RemoteArticle(
                "article-old",
                account.account_id,
                account.name,
                "Old",
                "https://example.invalid/old",
                now - timedelta(days=30),
            ),
        )

    async def content(self, article):
        self.content_calls.append(article.article_id)
        return ArticleContent(article.article_id, "<p>Complete body.</p>", ())

    async def asset(self, url):
        return PNG


@pytest.mark.asyncio
async def test_collector_applies_window_and_records_complete_article(tmp_path: Path):
    result = await collect_articles(
        client=FakeClient(),
        library_dir=tmp_path / "library",
        lookback_days=7,
        run_id="run-001",
    )
    assert [item.article.article_id for item in result.stored] == ["article-1"]
    assert result.stored[0].status is ArticleStatus.COMPLETE
    assert result.omissions == ()


@pytest.mark.asyncio
async def test_collector_exposes_latest_only_coverage_warning(tmp_path: Path):
    client = FakeClient()
    client.coverage_warning = "latest-only: historical articles are not traversed"
    result = await collect_articles(client, tmp_path / "library", 7, "run-001")
    assert any(
        item.scope == "coverage"
        and item.identifier == "run-001"
        and item.reason.startswith("latest-only:")
        for item in result.omissions
    )


@pytest.mark.asyncio
async def test_asset_failure_is_visible_but_article_remains(tmp_path: Path):
    class FailedAssetClient(FakeClient):
        async def content(self, article):
            return ArticleContent(
                article.article_id,
                '<p>Body remains.</p><img width="1200" height="1200" '
                'src="https://example.invalid/missing.png">',
                ("https://example.invalid/missing.png",),
            )

        async def asset(self, url):
            raise OSError("asset timeout")

    result = await collect_articles(
        FailedAssetClient(), tmp_path / "library", 7, "run-001"
    )
    assert [item.article.article_id for item in result.stored] == ["article-1"]
    assert result.stored[0].status is ArticleStatus.COMPLETE
    assert any(
        item.scope == "asset" and item.identifier == "article-1" and item.reason == "asset timeout"
        for item in result.omissions
    )


@pytest.mark.asyncio
async def test_seeded_url_is_not_refetched(tmp_path: Path):
    client = FakeClient()
    result = await collect_articles(
        client,
        tmp_path / "library",
        7,
        "run-001",
        is_known=lambda url: url.endswith("/recent"),
    )
    assert result.stored == ()
    assert client.content_calls == []


@pytest.mark.asyncio
async def test_body_failure_keeps_visible_placeholder(tmp_path: Path):
    class FailedBodyClient(FakeClient):
        async def content(self, article):
            raise OSError("body timeout")

    result = await collect_articles(
        FailedBodyClient(), tmp_path / "library", 7, "run-001"
    )
    assert result.stored[0].status is ArticleStatus.BODY_UNAVAILABLE
    assert "body unavailable" in result.stored[0].source_path.read_text(encoding="utf-8")
    assert any(item.scope == "article" and item.reason == "body timeout" for item in result.omissions)


@pytest.mark.asyncio
async def test_transient_retry_preserves_existing_complete_article_bytes(tmp_path: Path):
    library = tmp_path / "library"
    first = await collect_articles(FakeClient(), library, 7, "run-001")
    original = first.stored[0]
    source_bytes = original.source_path.read_bytes()
    metadata_bytes = original.metadata_path.read_bytes()

    class FailedRetryClient(FakeClient):
        async def content(self, article):
            raise OSError("transient body timeout")

    retried = await collect_articles(FailedRetryClient(), library, 7, "run-001")
    preserved = retried.stored[0]
    assert preserved.status is ArticleStatus.COMPLETE
    assert preserved.source_sha256 == original.source_sha256
    assert preserved.source_path.read_bytes() == source_bytes
    assert preserved.metadata_path.read_bytes() == metadata_bytes
    assert any(item.reason == "transient body timeout" for item in retried.omissions)


@pytest.mark.asyncio
async def test_existing_article_identity_conflict_is_global_contract_failure(tmp_path: Path):
    library = tmp_path / "library"
    await collect_articles(FakeClient(), library, 7, "run-001")

    class ConflictingClient(FakeClient):
        async def articles(self, account):
            article = (await super().articles(account))[0]
            return (
                RemoteArticle(
                    article.article_id,
                    article.account_id,
                    article.account_name,
                    article.title,
                    "https://example.invalid/conflicting-source",
                    article.published_at,
                ),
            )

    with pytest.raises(ContentContractUnavailable, match="identity conflicts"):
        await collect_articles(ConflictingClient(), library, 7, "run-001")


@pytest.mark.asyncio
async def test_existing_wechat_article_accepts_tracking_url_variation(tmp_path: Path):
    class WeChatClient(FakeClient):
        source_url = (
            "https://mp.weixin.qq.com/s?__biz=demo&mid=100&idx=1&sn=stable"
        )

        async def articles(self, account):
            article = (await super().articles(account))[0]
            return (
                RemoteArticle(
                    article.article_id,
                    article.account_id,
                    article.account_name,
                    article.title,
                    self.source_url,
                    article.published_at,
                ),
            )

    library = tmp_path / "library"
    first = await collect_articles(WeChatClient(), library, 7, "run-001")

    retry = WeChatClient()
    retry.source_url = (
        "https://mp.weixin.qq.com/s?idx=1\\u0026mid=100\\u0026__biz=demo\\u0026scene=1"
    )
    second = await collect_articles(retry, library, 7, "run-001")
    assert second.stored[0].source_sha256 == first.stored[0].source_sha256


@pytest.mark.asyncio
async def test_collector_rejects_symlink_library_ancestor_before_empty_shelf_mutation(
    tmp_path: Path,
):
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "linked").symlink_to(outside, target_is_directory=True)

    class EmptyClient(FakeClient):
        async def shelf(self):
            return ()

    with pytest.raises(ValueError, match="unsafe collection"):
        await collect_articles(
            EmptyClient(), tmp_path / "linked" / "library", 7, "run-001"
        )
    assert list(outside.iterdir()) == []


@pytest.mark.asyncio
async def test_collector_checkpoints_each_stored_article(tmp_path: Path):
    checkpoints = []
    await collect_articles(
        FakeClient(),
        tmp_path / "library",
        7,
        "run-001",
        on_stored=checkpoints.append,
    )
    assert [item.article.article_id for item in checkpoints] == ["article-1"]


@pytest.mark.asyncio
async def test_collector_canary_can_target_one_account_and_reports_missing(tmp_path: Path):
    result = await collect_articles(
        FakeClient(),
        tmp_path / "library",
        7,
        "run-001",
        account_ids={"account-1", "missing"},
    )
    assert [item.article.account_id for item in result.stored] == ["account-1"]
    assert len(result.omissions) == 1
    assert (result.omissions[0].scope, result.omissions[0].identifier) == ("account", "missing")


@pytest.mark.asyncio
async def test_account_failure_is_visible_and_other_accounts_continue(tmp_path: Path):
    class PartialClient(FakeClient):
        async def shelf(self):
            return (
                ShelfAccount("broken", "Broken Account"),
                ShelfAccount("working", "Working Account"),
            )

        async def articles(self, account):
            if account.account_id == "broken":
                raise OSError("account timeout")
            return (
                RemoteArticle(
                    "working-article",
                    account.account_id,
                    account.name,
                    "Working article",
                    "https://example.invalid/working",
                    datetime.now(UTC),
                ),
            )

    result = await collect_articles(PartialClient(), tmp_path / "library", 7, "run-001")
    assert [item.article.article_id for item in result.stored] == ["working-article"]
    assert any(item.scope == "account" and item.identifier == "broken" for item in result.omissions)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["articles", "content", "asset"])
async def test_global_content_contract_failure_propagates(tmp_path: Path, phase: str):
    class ChangedContractClient(FakeClient):
        async def articles(self, account):
            if phase == "articles":
                raise ContentContractUnavailable("global contract changed")
            return await super().articles(account)

        async def content(self, article):
            if phase == "content":
                raise ContentContractUnavailable("global contract changed")
            if phase == "asset":
                return ArticleContent(
                    article.article_id,
                    '<img width="640" src="https://example.invalid/image.png">',
                    (),
                )
            return await super().content(article)

        async def asset(self, url):
            raise ContentContractUnavailable("global contract changed")

    with pytest.raises(ContentContractUnavailable, match="global contract changed"):
        await collect_articles(
            ChangedContractClient(), tmp_path / "library", 7, "run-001"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["articles", "content", "asset"])
async def test_auth_required_propagates_from_every_remote_phase(tmp_path: Path, phase: str):
    class ExpiredClient(FakeClient):
        async def articles(self, account):
            if phase == "articles":
                raise AuthRequired("authorization expired")
            return await super().articles(account)

        async def content(self, article):
            if phase == "content":
                raise AuthRequired("authorization expired")
            if phase == "asset":
                return ArticleContent(
                    article.article_id,
                    '<img width="640" src="https://example.invalid/image.png">',
                    (),
                )
            return await super().content(article)

        async def asset(self, url):
            raise AuthRequired("authorization expired")

    with pytest.raises(AuthRequired, match="authorization expired"):
        await collect_articles(ExpiredClient(), tmp_path / "library", 7, "run-001")


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["shelf", "articles", "content", "asset"])
async def test_cancellation_is_never_swallowed(tmp_path: Path, phase: str):
    class CancelledClient(FakeClient):
        async def shelf(self):
            if phase == "shelf":
                raise asyncio.CancelledError
            return await super().shelf()

        async def articles(self, account):
            if phase == "articles":
                raise asyncio.CancelledError
            return await super().articles(account)

        async def content(self, article):
            if phase == "content":
                raise asyncio.CancelledError
            if phase == "asset":
                return ArticleContent(
                    article.article_id,
                    '<img width="640" src="https://example.invalid/image.png">',
                    (),
                )
            return await super().content(article)

        async def asset(self, url):
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await collect_articles(CancelledClient(), tmp_path / "library", 7, "run-001")


@pytest.mark.asyncio
async def test_duplicate_article_identity_is_collected_once(tmp_path: Path):
    class DuplicateClient(FakeClient):
        async def articles(self, account):
            recent = (await super().articles(account))[0]
            return (recent, recent)

    result = await collect_articles(DuplicateClient(), tmp_path / "library", 7, "run-001")
    assert [item.article.article_id for item in result.stored] == ["article-1"]


class FakeResponse:
    def __init__(
        self,
        *,
        url: str = "https://example.invalid/image.png",
        status: int = 200,
        content_type: str = "image/png",
        body: bytes = PNG,
        payload: object | None = None,
    ) -> None:
        self.url = url
        self.status = status
        self.ok = 200 <= status < 300
        self.headers = {"content-type": content_type, "content-length": str(len(body))}
        self._body = body
        self._payload = payload
        self.body_called = False

    async def body(self):
        self.body_called = True
        return self._body

    async def json(self):
        return self._payload

    async def text(self):
        return self._body.decode("utf-8")


class FakeRequest:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.urls: list[str] = []
        self.get_kwargs: list[dict[str, object]] = []

    async def get(self, url, **kwargs):
        self.urls.append(url)
        self.get_kwargs.append(kwargs)
        return self.response


    async def post(self, url, **kwargs):
        self.urls.append(url)
        return self.response


class FakeContext:
    def __init__(self, response: FakeResponse) -> None:
        self.request = FakeRequest(response)


@pytest.mark.asyncio
async def test_playwright_asset_accepts_matching_safe_raster():
    response = FakeResponse()
    client = PlaywrightWeReadClient(FakeContext(response), object())
    assert await client.asset("https://example.invalid/image.png") == PNG
    assert response.body_called
    assert client.context.request.get_kwargs == [{"max_redirects": 0}]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "message"),
    [
        (FakeResponse(status=503), "HTTP 503"),
        (FakeResponse(url="https://unapproved.example/image.png"), "unsafe final"),
        (FakeResponse(content_type="text/html"), "content type"),
        (FakeResponse(content_type="image/svg+xml", body=b"<svg/>"), "content type"),
        (FakeResponse(content_type="image/png", body=b"not-a-png"), "signature"),
    ],
)
async def test_playwright_asset_rejects_status_redirect_mime_and_magic_before_storage(
    response: FakeResponse, message: str
):
    client = PlaywrightWeReadClient(FakeContext(response), object())
    with pytest.raises(OSError, match=message):
        await client.asset("https://example.invalid/image.png")


@pytest.mark.asyncio
async def test_playwright_asset_rejects_unsafe_initial_url_before_request():
    response = FakeResponse()
    context = FakeContext(response)
    client = PlaywrightWeReadClient(context, object())
    with pytest.raises(OSError, match="unsafe asset URL"):
        await client.asset("https://127.0.0.1/image.png")
    assert context.request.urls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "endpoint", "response_url"),
    [("content", "https://weread.qq.com/web/mp/content", "https://evil.invalid/web/mp/content")],
)
async def test_playwright_api_rejects_unexpected_final_endpoint(
    method: str, endpoint: str, response_url: str
):
    payload = {"chapterInfos": []} if method == "articles" else {
        "reviewId": "article-1",
        "content": "<p>Body.</p>",
    }
    response = FakeResponse(url=response_url, payload=payload)
    client = PlaywrightWeReadClient(FakeContext(response), object())
    article = RemoteArticle(
        "article-1", "account-1", "Example Account", "Title", endpoint, datetime.now(UTC)
    )
    with pytest.raises(ContentContractUnavailable, match="unexpected endpoint"):
        if method == "articles":
            await client.articles(ShelfAccount("account-1", "Example Account"))
        else:
            await client.content(article)


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["content"])
@pytest.mark.parametrize(
    ("url", "status"),
    [("https://weread.qq.com/web/login", 200), ("https://weread.qq.com/web/mp/content", 401)],
)
async def test_playwright_api_classifies_login_and_unauthorized_as_auth_required(
    method: str, url: str, status: int
):
    expected_path = "/web/mp/content"
    if status == 401:
        url = f"https://weread.qq.com{expected_path}"
    response = FakeResponse(url=url, status=status)
    client = PlaywrightWeReadClient(FakeContext(response), object())
    article = RemoteArticle(
        "article-1", "account-1", "Example Account", "Title", "https://example.invalid/a", datetime.now(UTC)
    )
    with pytest.raises(AuthRequired):
        if method == "articles":
            await client.articles(ShelfAccount("account-1", "Example Account"))
        else:
            await client.content(article)


@pytest.mark.asyncio
async def test_content_404_is_body_unavailable_only_at_expected_endpoint():
    article = RemoteArticle(
        "article-1", "account-1", "Example Account", "Title", "https://example.invalid/a", datetime.now(UTC)
    )
    expected = FakeResponse(url="https://weread.qq.com/web/mp/content", status=404)
    with pytest.raises(ArticleBodyUnavailable):
        await PlaywrightWeReadClient(FakeContext(expected), object()).content(article)

    unexpected = FakeResponse(url="https://evil.invalid/not-found", status=404)
    with pytest.raises(ContentContractUnavailable, match="unexpected endpoint"):
        await PlaywrightWeReadClient(FakeContext(unexpected), object()).content(article)


class FakePage:
    def __init__(self, response: FakeResponse, final_url: str) -> None:
        self.response = response
        self.url = final_url

    async def goto(self, url, **kwargs):
        return self.response

    async def wait_for_selector(self, selector, **kwargs):
        return object()

    async def content(self):
        return '<a data-book-type="official-account" data-book-id="account-1"><span class="title">Example</span></a>'


def test_cover_resource_parser_is_exact_deduplicated_and_safe():
    urls = [
        "https://weread.qq.com/web/mp/cover?bookId=book-2",
        "https://weread.qq.com/web/mp/cover?bookId=book-1",
        "https://weread.qq.com/web/mp/cover?bookId=book-1",
        "https://evil.invalid/web/mp/cover?bookId=evil",
        "https://weread.qq.com/web/mp/cover?bookId=book-3&extra=1",
        "https://weread.qq.com/web/mp/content?bookId=wrong-path",
        "not a URL",
    ]
    assert parse_cover_book_ids(urls) == ("book-1", "book-2")


def test_cover_resource_parser_rejects_missing_contract():
    with pytest.raises(ShelfUnavailable, match="cover responses"):
        parse_cover_book_ids(["https://weread.qq.com/web/mp/content?bookId=none"])


def test_cover_payload_builds_one_latest_seed():
    seed = parse_cover_payload(
        {"name": " Example Account ", "title": " Latest Article ", "reviewId": "review-1"},
        "book-1",
    )
    assert seed.account == ShelfAccount("book-1", "Example Account")
    assert (seed.article_id, seed.review_id, seed.title) == (
        "review-1",
        "review-1",
        "Latest Article",
    )


def test_cover_payload_keeps_tilde_in_remote_id_now_that_paths_are_hashed():
    # Path safety moved to hashed article dirs (article_dir_name); the
    # article ID itself keeps its original characters (e.g. '~').
    seed = parse_cover_payload(
        {"name": "Account", "title": "Latest", "reviewId": "remote~review"},
        "book-1",
    )
    assert seed.review_id == "remote~review"
    assert seed.article_id == "remote~review"
    assert "~" in seed.article_id


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"name": "", "title": "Title", "reviewId": "review-1"},
    ],
)
def test_cover_payload_rejects_incomplete_fields(payload):
    with pytest.raises(ContentContractUnavailable):
        parse_cover_payload(payload, "book-1")


def test_latest_html_metadata_uses_publication_time_and_canonical_url():
    published, source_url = parse_latest_html_metadata(
        '<meta property="og:url" content="https://mp.weixin.qq.com/s?a=1\\u0026b=2">'
        '<script>var ct = "1722600000";</script>'
    )
    assert published == datetime.fromtimestamp(1722600000, tz=UTC)
    assert source_url == "https://mp.weixin.qq.com/s?a=1&b=2"


def test_latest_html_metadata_supports_script_source_fallback():
    published, source_url = parse_latest_html_metadata(
        '<script>publish_time: 1722600000, msg_link: '
        '"https://mp.weixin.qq.com/s?x=1\\u0026y=2"</script>'
    )
    assert published == datetime.fromtimestamp(1722600000, tz=UTC)
    assert source_url == "https://mp.weixin.qq.com/s?x=1&y=2"


@pytest.mark.parametrize(
    "html",
    [
        '<meta property="og:url" content="https://example.invalid/a">',
        '<script>ct = "1722600000"</script>',
        '<meta property="og:url" content="http://example.invalid/a"><script>ct=1722600000</script>',
    ],
)
def test_latest_html_metadata_rejects_missing_or_unsafe_fields(html):
    with pytest.raises(ContentContractUnavailable):
        parse_latest_html_metadata(html)


class LatestOnlyRequest:
    def __init__(self) -> None:
        self.content_calls = 0

    async def get(self, url, **kwargs):
        params = kwargs.get("params", {})
        if url.endswith("/cover"):
            book_id = params["bookId"]
            return FakeResponse(
                url="https://weread.qq.com/web/mp/cover",
                content_type="application/json",
                payload={
                    "name": f"Account {book_id}",
                    "title": f"Latest {book_id}",
                    "reviewId": f"review-{book_id}",
                },
            )
        if url.endswith("/articles"):
            return FakeResponse(
                url="https://weread.qq.com/web/mp/articles",
                content_type="application/json",
                payload={"reviews": [], "clearAll": 0, "synckey": 0},
            )
        self.content_calls += 1
        article_id = params["reviewId"]
        body = (
            f'<meta property="og:url" content="https://mp.weixin.qq.com/s/{article_id}">'
            '<script>ct = "1722600000"</script><p>Body.</p>'
        ).encode()
        return FakeResponse(
            url="https://weread.qq.com/web/mp/content",
            content_type="text/plain; charset=utf-8",
            body=body,
        )


class LatestOnlyContext:
    def __init__(self) -> None:
        self.request = LatestOnlyRequest()


class LatestOnlyPage:
    url = "https://weread.qq.com/web/shelf"

    async def goto(self, url, **kwargs):
        return FakeResponse(url=self.url)

    async def reload(self, **kwargs):
        return FakeResponse(url=self.url)

    async def wait_for_selector(self, selector, **kwargs):
        return object()

    async def wait_for_load_state(self, state, **kwargs):
        return None

    async def content(self):
        return (
            '<a class="shelfBook" href="/web/mp/reader/encoded-1"><div class="title">One</div></a>'
            '<a class="shelfBook" href="/web/mp/reader/encoded-2"><div class="title">Two</div></a>'
        )

    async def evaluate(self, expression):
        return [
            "https://weread.qq.com/web/mp/cover?bookId=book-2",
            "https://weread.qq.com/web/mp/cover?bookId=book-1",
        ]


@pytest.mark.asyncio
async def test_latest_only_client_uses_cover_and_caches_direct_html_content():
    context = LatestOnlyContext()
    client = PlaywrightWeReadClient(context, LatestOnlyPage())
    accounts = await client.shelf()
    assert [item.account_id for item in accounts] == ["book-1", "book-2"]

    articles = await client.articles(accounts[0])
    assert len(articles) == 1
    assert articles[0].article_id == "review-book-1"
    assert articles[0].source_url == "https://mp.weixin.qq.com/s/review-book-1"
    content = await client.content(articles[0])
    assert "<p>Body.</p>" in content.html
    assert context.request.content_calls == 1


@pytest.mark.asyncio
async def test_latest_only_shelf_fails_closed_on_cover_count_mismatch():
    class MismatchedPage(LatestOnlyPage):
        async def evaluate(self, expression):
            return ["https://weread.qq.com/web/mp/cover?bookId=book-1"]

    with pytest.raises(ShelfUnavailable, match="count does not match"):
        await PlaywrightWeReadClient(LatestOnlyContext(), MismatchedPage()).shelf()


@pytest.mark.asyncio
async def test_latest_only_shelf_reloads_once_after_partial_first_render():
    class TransientPage(LatestOnlyPage):
        def __init__(self):
            self.resource_reads = 0
            self.reloads = 0

        async def reload(self, **kwargs):
            self.reloads += 1
            return await super().reload(**kwargs)

        async def evaluate(self, expression):
            self.resource_reads += 1
            if self.resource_reads == 1:
                return ["https://weread.qq.com/web/mp/cover?bookId=book-1"]
            return await super().evaluate(expression)

    page = TransientPage()
    accounts = await PlaywrightWeReadClient(LatestOnlyContext(), page).shelf()
    assert [item.account_id for item in accounts] == ["book-1", "book-2"]
    assert page.reloads == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("url", "status", "error"),
    [
        ("https://weread.qq.com/web/login", 200, AuthRequired),
        ("https://weread.qq.com/web/shelf", 401, AuthRequired),
        ("https://evil.invalid/web/shelf", 200, ShelfUnavailable),
    ],
)
async def test_playwright_shelf_validates_final_page_taxonomy(url, status, error):
    response = FakeResponse(url=url, status=status)
    page = FakePage(response, url)
    with pytest.raises(error):
        await PlaywrightWeReadClient(FakeContext(response), page).shelf()
