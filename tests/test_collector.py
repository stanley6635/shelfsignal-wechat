from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from shelfsignal.collector import collect_articles
from shelfsignal.errors import ContentContractUnavailable
from shelfsignal.models import (
    ArticleContent,
    ArticleStatus,
    RemoteArticle,
    ShelfAccount,
)
from shelfsignal.weread import PlaywrightWeReadClient

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
    ) -> None:
        self.url = url
        self.status = status
        self.ok = 200 <= status < 300
        self.headers = {"content-type": content_type, "content-length": str(len(body))}
        self._body = body
        self.body_called = False

    async def body(self):
        self.body_called = True
        return self._body


class FakeRequest:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.urls: list[str] = []

    async def get(self, url, **kwargs):
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
