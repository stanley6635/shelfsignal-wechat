from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .content import (
    atomic_write,
    ensure_safe_directory,
    load_stored_article,
    normalize_html,
    safe_article_dir,
    safe_asset_path,
    write_source,
)
from .errors import AuthRequired, ContentContractUnavailable
from .models import (
    ArticleStatus,
    CollectionOmission,
    CollectionResult,
    RemoteArticle,
    StoredArticle,
)
from .weread import ArticleClient

MAX_LOOKBACK_DAYS = 36_500
_MAX_REASON_LENGTH = 500


async def collect_articles(
    client: ArticleClient,
    library_dir: Path,
    lookback_days: int,
    run_id: str,
    is_known: Callable[[str], bool] | None = None,
    on_stored: Callable[[StoredArticle], None] | None = None,
    account_ids: set[str] | None = None,
) -> CollectionResult:
    _validate_collection_inputs(lookback_days, run_id)
    library_dir = ensure_safe_directory(library_dir)
    cutoff = datetime.now(UTC) - timedelta(days=lookback_days)
    stored: list[StoredArticle] = []
    omissions: list[CollectionOmission] = []
    seen_article_ids: set[str] = set()
    seen_source_urls: set[str] = set()

    accounts = await client.shelf()
    if account_ids:
        available = {account.account_id for account in accounts}
        for missing in sorted(account_ids - available):
            omissions.append(CollectionOmission("account", missing, "not found on shelf"))
        accounts = tuple(account for account in accounts if account.account_id in account_ids)

    for account in sorted(accounts, key=lambda item: item.account_id):
        try:
            articles = await client.articles(account)
        except (AuthRequired, ContentContractUnavailable):
            raise
        except Exception as exc:  # noqa: BLE001 - account failures are visible partial results
            omissions.append(
                CollectionOmission("account", account.account_id, _failure_reason(exc))
            )
            continue

        for article in sorted(articles, key=_article_sort_key):
            try:
                published_at = _published_at_utc(article)
            except (TypeError, ValueError) as exc:
                omissions.append(
                    CollectionOmission("article", article.article_id, _failure_reason(exc))
                )
                continue
            if published_at < cutoff:
                continue
            if article.article_id in seen_article_ids or article.source_url in seen_source_urls:
                continue
            seen_article_ids.add(article.article_id)
            seen_source_urls.add(article.source_url)
            if is_known is not None and is_known(article.source_url):
                continue

            item = await _collect_one(
                client,
                library_dir,
                article,
                run_id,
                omissions,
            )
            if item is None:
                continue
            stored.append(item)
            if on_stored is not None:
                on_stored(item)

    return CollectionResult(tuple(stored), tuple(omissions))


async def _collect_one(
    client: ArticleClient,
    library_dir: Path,
    article: RemoteArticle,
    run_id: str,
    omissions: list[CollectionOmission],
) -> StoredArticle | None:
    existing: StoredArticle | None = None
    prior_artifacts = False
    try:
        directory = safe_article_dir(library_dir, article.article_id)
        prior_artifacts = _stored_artifacts_present(directory)
        if prior_artifacts:
            try:
                existing = load_stored_article(directory)
            except (OSError, TypeError, ValueError):
                pass
            else:
                if (
                    existing.article.article_id != article.article_id
                    or existing.article.source_url != article.source_url
                ):
                    raise ContentContractUnavailable(
                        "stored article identity conflicts with remote article"
                    )
        remote_content = await client.content(article)
        if existing is not None and existing.status is ArticleStatus.COMPLETE:
            return existing
        normalized = normalize_html(remote_content.html)
        asset_paths: list[Path] = []
        image_urls = tuple(
            dict.fromkeys((*normalized.image_urls, *remote_content.image_urls))
        )
        for image_url in image_urls:
            try:
                destination = safe_asset_path(directory / "assets", image_url)
                content = await client.asset(image_url)
                atomic_write(destination, content)
                asset_paths.append(destination)
            except (AuthRequired, ContentContractUnavailable):
                raise
            except Exception as exc:  # noqa: BLE001 - one asset must not discard source text
                omissions.append(
                    CollectionOmission("asset", article.article_id, _failure_reason(exc))
                )

        markdown = normalized.markdown
        if asset_paths:
            image_lines = [f"![content image](assets/{path.name})" for path in asset_paths]
            markdown = markdown.rstrip() + "\n\n" + "\n\n".join(image_lines) + "\n"
        source, metadata, digest = write_source(
            directory,
            markdown,
            _metadata(article, run_id, ArticleStatus.COMPLETE, "weread-mp-content"),
        )
        return StoredArticle(
            article=article,
            directory=directory,
            source_path=source,
            metadata_path=metadata,
            asset_paths=tuple(asset_paths),
            ocr_path=None,
            source_sha256=digest,
            status=ArticleStatus.COMPLETE,
        )
    except (AuthRequired, ContentContractUnavailable):
        raise
    except Exception as exc:  # noqa: BLE001 - retain a visible metadata-only candidate
        omissions.append(
            CollectionOmission("article", article.article_id, _failure_reason(exc))
        )
        if existing is not None and existing.status is ArticleStatus.COMPLETE:
            return existing
        if prior_artifacts:
            omissions.append(
                CollectionOmission(
                    "article",
                    article.article_id,
                    "existing stored article is invalid; placeholder not written",
                )
            )
            return None

    try:
        directory = safe_article_dir(library_dir, article.article_id)
        placeholder = (
            f"# {article.title}\n\n"
            f"- Source: {article.source_url}\n"
            "- Retrieval: article body unavailable\n"
        )
        source, metadata, digest = write_source(
            directory,
            placeholder,
            _metadata(article, run_id, ArticleStatus.BODY_UNAVAILABLE, "metadata-only"),
        )
    except Exception as exc:  # noqa: BLE001 - storage failure is isolated to this article
        omissions.append(
            CollectionOmission("article", article.article_id, f"placeholder: {_failure_reason(exc)}")
        )
        return None
    return StoredArticle(
        article=article,
        directory=directory,
        source_path=source,
        metadata_path=metadata,
        asset_paths=(),
        ocr_path=None,
        source_sha256=digest,
        status=ArticleStatus.BODY_UNAVAILABLE,
    )


def _stored_artifacts_present(directory: Path) -> bool:
    for name in ("source.md", "metadata.md"):
        try:
            os.lstat(directory / name)
        except FileNotFoundError:
            continue
        return True
    return False


def _validate_collection_inputs(lookback_days: int, run_id: str) -> None:
    if type(lookback_days) is not int or not 0 <= lookback_days <= MAX_LOOKBACK_DAYS:
        raise ValueError(f"lookback_days must be between 0 and {MAX_LOOKBACK_DAYS}")
    if not isinstance(run_id, str) or not run_id.strip() or len(run_id) > 128:
        raise ValueError("run_id must be non-empty and at most 128 characters")


def _published_at_utc(article: RemoteArticle) -> datetime:
    published_at = article.published_at
    if not isinstance(published_at, datetime) or published_at.tzinfo is None:
        raise ValueError("article publication time must include a timezone")
    return published_at.astimezone(UTC)


def _article_sort_key(article: RemoteArticle) -> tuple[str, str]:
    published_at = article.published_at
    timestamp = published_at.isoformat() if isinstance(published_at, datetime) else ""
    return timestamp, article.article_id


def _failure_reason(exc: Exception) -> str:
    reason = " ".join(str(exc).split()) or type(exc).__name__
    return reason[:_MAX_REASON_LENGTH]


def _metadata(
    article: RemoteArticle,
    run_id: str,
    status: ArticleStatus,
    extraction_method: str,
) -> dict[str, str]:
    return {
        "account_id": article.account_id,
        "account_name": article.account_name,
        "article_id": article.article_id,
        "extraction_method": extraction_method,
        "published_at": article.published_at.isoformat(),
        "run_id": run_id,
        "source_url": article.source_url,
        "status": status.value,
        "title": article.title,
    }
