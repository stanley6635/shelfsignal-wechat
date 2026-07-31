from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path


class ArticleStatus(StrEnum):
    DISCOVERED = "discovered"
    COMPLETE = "complete"
    BODY_UNAVAILABLE = "body_unavailable"
    OCR_INCOMPLETE = "ocr_incomplete"


@dataclass(frozen=True)
class RemoteArticle:
    article_id: str
    account_id: str
    account_name: str
    title: str
    source_url: str
    published_at: datetime


@dataclass(frozen=True)
class ShelfAccount:
    account_id: str
    name: str


@dataclass(frozen=True)
class ArticleContent:
    article_id: str
    html: str
    image_urls: tuple[str, ...]


@dataclass(frozen=True)
class StoredArticle:
    article: RemoteArticle
    directory: Path
    source_path: Path
    metadata_path: Path
    asset_paths: tuple[Path, ...]
    ocr_path: Path | None
    source_sha256: str
    status: ArticleStatus


@dataclass(frozen=True)
class ReadingCard:
    article_id: str
    title: str
    account_name: str
    published_at: datetime
    source_url: str
    source_path: Path
    excerpt: str
    meaningful_image_count: int
    ocr_status: str
    retrieval_status: str
