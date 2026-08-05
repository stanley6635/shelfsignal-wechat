from __future__ import annotations

import json
import os
import re
import stat
from datetime import UTC, datetime
from pathlib import Path

from .content import atomic_write
from .models import ArticleStatus, ReadingCard, StoredArticle

_MAX_CARD_CHARACTERS = 10_000
_MAX_SOURCE_SCAN_BYTES = 8 * 1024 * 1024
_MAX_OCR_BYTES = 8 * 1024 * 1024
_MAX_TITLE_CHARACTERS = 512
_MAX_ACCOUNT_CHARACTERS = 256
_MAX_URL_CHARACTERS = 4096
_MAX_PATH_CHARACTERS = 4096
_MAX_STATUS_CHARACTERS = 256
_MAX_CARDS = 2000
_MAX_SERIALIZED_CARD_BYTES = 96 * 1024
_MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
_SAFE_ARTICLE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:~-]{0,127}\Z")


def _bounded_excerpt(text: str, limit: int) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    return compact if len(compact) <= limit else compact[: limit - 1].rstrip() + "…"


def _bounded_field(value: str, limit: int, label: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be text")
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _utc_instant(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("published_at must be timezone-aware")
    return value.astimezone(UTC)


def _validate_limit(limit: int) -> None:
    if type(limit) is not int or not 1 <= limit <= _MAX_CARD_CHARACTERS:
        raise ValueError(
            f"max_characters must be between 1 and {_MAX_CARD_CHARACTERS}"
        )


def _expected_article_path(stored: StoredArticle, path: Path, name: str, label: str) -> Path:
    expected = stored.directory / name
    if (
        not stored.directory.is_absolute()
        or not path.is_absolute()
        or ".." in stored.directory.parts
        or ".." in path.parts
        or Path(os.path.normpath(os.fspath(path))) != path
        or Path(os.path.normpath(os.fspath(stored.directory))) != stored.directory
        or path != expected
    ):
        raise ValueError(f"unsafe {label} path")
    return path


def _open_article_directory(directory: Path, label: str) -> int:
    if not directory.is_absolute() or ".." in directory.parts:
        raise ValueError(f"unsafe {label} path")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(directory.anchor, flags)
    except OSError as exc:
        raise ValueError(f"unsafe {label} path") from exc
    try:
        for part in directory.parts[1:]:
            try:
                child = os.open(part, flags, dir_fd=descriptor)
            except OSError as exc:
                raise ValueError(f"unsafe {label} path") from exc
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _read_article_file(
    stored: StoredArticle,
    path: Path,
    *,
    expected_name: str,
    label: str,
    max_bytes: int,
    truncate: bool = False,
) -> tuple[str | None, bool]:
    path = _expected_article_path(stored, path, expected_name, label)
    directory_fd = _open_article_directory(stored.directory, label)
    try:
        try:
            descriptor = os.open(
                path.name,
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0),
                dir_fd=directory_fd,
            )
        except FileNotFoundError:
            return None, False
        except OSError as exc:
            raise ValueError(f"unsafe {label} file") from exc
        try:
            details = os.fstat(descriptor)
            if not stat.S_ISREG(details.st_mode):
                raise ValueError(f"unsafe {label} file")
            oversized = details.st_size > max_bytes
            if oversized and not truncate:
                raise ValueError(f"{label} file is too large")
            content = bytearray()
            while len(content) < max_bytes:
                chunk = os.read(descriptor, min(64 * 1024, max_bytes - len(content)))
                if not chunk:
                    break
                content.extend(chunk)
            if not oversized:
                oversized = bool(os.read(descriptor, 1))
            if oversized and not truncate:
                raise ValueError(f"{label} file is too large")
        finally:
            os.close(descriptor)
    finally:
        os.close(directory_fd)

    try:
        text = bytes(content).decode("utf-8")
    except UnicodeDecodeError as exc:
        if not (truncate and oversized):
            raise ValueError(f"{label} file must be UTF-8 Markdown") from exc
        text = bytes(content).decode("utf-8", errors="ignore")
    return text, oversized


def build_card(stored: StoredArticle, max_characters: int = 800) -> ReadingCard:
    _validate_limit(max_characters)
    if not _SAFE_ARTICLE_ID.fullmatch(stored.article.article_id):
        raise ValueError("unsafe article ID")
    _utc_instant(stored.article.published_at)

    source, source_truncated = _read_article_file(
        stored,
        stored.source_path,
        expected_name="source.md",
        label="source",
        max_bytes=_MAX_SOURCE_SCAN_BYTES,
        truncate=True,
    )
    if source is None:
        excerpt = _bounded_excerpt("[Source unavailable: missing]", max_characters)
        retrieval_status = f"{stored.status.value}; source-missing"
    else:
        excerpt = _bounded_excerpt(source, max_characters)
        retrieval_status = stored.status.value
        if source_truncated:
            retrieval_status += "; source-scan-truncated"

    if stored.ocr_path is None:
        ocr_status = (
            "incomplete"
            if stored.status is ArticleStatus.OCR_INCOMPLETE
            else "not-needed"
        )
    else:
        ocr, _ = _read_article_file(
            stored,
            stored.ocr_path,
            expected_name="ocr.md",
            label="OCR",
            max_bytes=_MAX_OCR_BYTES,
        )
        if ocr is None:
            ocr_status = "missing"
        elif stored.status is ArticleStatus.OCR_INCOMPLETE or "OCR incomplete:" in ocr:
            ocr_status = "incomplete"
        else:
            ocr_status = "available"

    return ReadingCard(
        article_id=stored.article.article_id,
        title=_bounded_field(stored.article.title, _MAX_TITLE_CHARACTERS, "title"),
        account_name=_bounded_field(
            stored.article.account_name, _MAX_ACCOUNT_CHARACTERS, "account name"
        ),
        published_at=stored.article.published_at,
        source_url=_bounded_field(
            stored.article.source_url, _MAX_URL_CHARACTERS, "source URL"
        ),
        source_path=Path(
            _bounded_field(os.fspath(stored.source_path), _MAX_PATH_CHARACTERS, "source path")
        ),
        excerpt=excerpt,
        meaningful_image_count=len(stored.asset_paths),
        ocr_status=ocr_status,
        retrieval_status=retrieval_status,
    )


def _markdown_value(value: object) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def _render_card(card: ReadingCard) -> bytes:
    title = _bounded_field(card.title, _MAX_TITLE_CHARACTERS, "title")
    account = _bounded_field(
        card.account_name, _MAX_ACCOUNT_CHARACTERS, "account name"
    )
    source_url = _bounded_field(card.source_url, _MAX_URL_CHARACTERS, "source URL")
    source_path = _bounded_field(
        os.fspath(card.source_path), _MAX_PATH_CHARACTERS, "source path"
    )
    excerpt = _bounded_excerpt(
        _bounded_field(card.excerpt, _MAX_CARD_CHARACTERS, "excerpt"),
        _MAX_CARD_CHARACTERS,
    )
    ocr_status = _bounded_field(
        card.ocr_status, _MAX_STATUS_CHARACTERS, "OCR status"
    )
    retrieval_status = _bounded_field(
        card.retrieval_status, _MAX_STATUS_CHARACTERS, "retrieval status"
    )
    block = "\n".join(
        [
            f"## {card.article_id}",
            "",
            f"- Title: {_markdown_value(title)}",
            f"- Account: {_markdown_value(account)}",
            f"- Published: {_markdown_value(card.published_at.isoformat())}",
            f"- Source URL: {_markdown_value(source_url)}",
            f"- Source path: {_markdown_value(source_path)}",
            f"- Meaningful images: {card.meaningful_image_count}",
            f"- OCR: {_markdown_value(ocr_status)}",
            f"- Retrieval: {_markdown_value(retrieval_status)}",
            "",
            f"> {excerpt}",
            "",
            "",
        ]
    ).encode("utf-8")
    if len(block) > _MAX_SERIALIZED_CARD_BYTES:
        raise ValueError(f"reading card is too large: {card.article_id}")
    return block


def write_cards(cards: tuple[ReadingCard, ...], path: Path) -> Path:
    if len(cards) > _MAX_CARDS:
        raise ValueError(f"too many reading cards; maximum is {_MAX_CARDS}")
    article_ids = [card.article_id for card in cards]
    if len(set(article_ids)) != len(article_ids):
        raise ValueError("duplicate reading card article ID")
    if any(not _SAFE_ARTICLE_ID.fullmatch(article_id) for article_id in article_ids):
        raise ValueError("unsafe article ID")

    ordered = sorted(cards, key=lambda item: (_utc_instant(item.published_at), item.article_id))
    content = bytearray(b"# ShelfSignal reading cards\n\n")
    for card in ordered:
        content.extend(_render_card(card))
        if len(content) > _MAX_ARTIFACT_BYTES:
            raise ValueError("reading cards artifact is too large")
    content = bytearray(bytes(content).rstrip() + b"\n")
    atomic_write(path, bytes(content))
    return path
