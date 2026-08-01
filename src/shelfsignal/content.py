from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlparse

from .models import ArticleStatus, RemoteArticle, StoredArticle

_BLOCK_TAGS = {
    "article",
    "blockquote",
    "div",
    "figcaption",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "li",
    "p",
    "pre",
    "section",
}
_IGNORED_TAGS = {"script", "style", "template"}
_METADATA_KEY = re.compile(r"[a-z][a-z0-9_]*")
_RESERVED_METADATA_KEYS = {"retrieved_at", "source_sha256"}
_REQUIRED_METADATA_KEYS = {
    "account_id",
    "account_name",
    "article_id",
    "published_at",
    "source_sha256",
    "source_url",
    "status",
    "title",
}


@dataclass(frozen=True)
class NormalizedContent:
    markdown: str
    image_urls: tuple[str, ...]


def _remote_image_url(source: str) -> bool:
    if "\\" in source:
        return False
    try:
        parsed = urlparse(source)
        hostname = parsed.hostname
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and bool(hostname)


def _dimension(value: str | None) -> int | None:
    if value is None or not value.isdecimal():
        return None
    dimension = int(value)
    return dimension if dimension >= 0 else None


class _ArticleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lines: list[str] = []
        self.images: list[str] = []
        self._text: list[str] = []
        self._ignored_depth = 0

    def _flush_text(self) -> None:
        text = re.sub(r"\s+", " ", " ".join(self._text)).strip()
        if text:
            self.lines.append(text)
        self._text.clear()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in _IGNORED_TAGS:
            self._ignored_depth += 1
            return
        if self._ignored_depth:
            return
        if tag == "br":
            self._flush_text()
        if tag != "img":
            return

        values = {key.lower(): value for key, value in attrs}
        source = values.get("src")
        width = _dimension(values.get("width"))
        height = _dimension(values.get("height"))
        classes = (values.get("class") or "").lower().split()
        is_avatar = any("avatar" in token for token in classes)
        if (
            source
            and width is not None
            and height is not None
            and not is_avatar
            and max(width, height) >= 320
            and _remote_image_url(source)
        ):
            self.images.append(source)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _IGNORED_TAGS:
            if self._ignored_depth:
                self._ignored_depth -= 1
            return
        if not self._ignored_depth and tag in _BLOCK_TAGS:
            self._flush_text()

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        text = re.sub(r"\s+", " ", data).strip()
        if text:
            self._text.append(text)

    def close(self) -> None:
        super().close()
        self._flush_text()


def normalize_html(html: str) -> NormalizedContent:
    parser = _ArticleParser()
    parser.feed(html)
    parser.close()
    markdown = "\n\n".join(parser.lines)
    return NormalizedContent(
        markdown=f"{markdown}\n" if markdown else "",
        image_urls=tuple(dict.fromkeys(parser.images)),
    )


def _is_symlink(path: Path) -> bool:
    try:
        return stat.S_ISLNK(path.lstat().st_mode)
    except FileNotFoundError:
        return False


def _require_safe_directory(directory: Path, label: str) -> Path:
    if _is_symlink(directory):
        raise ValueError(f"unsafe {label} directory")
    if directory.exists() and not directory.is_dir():
        raise ValueError(f"unsafe {label} directory")
    return directory.resolve()


def safe_asset_path(asset_dir: Path, source: str) -> Path:
    if not _remote_image_url(source):
        raise ValueError("unsafe asset URL")
    parsed = urlparse(source)
    decoded_path = unquote(parsed.path)
    if "\\" in decoded_path or ".." in Path(decoded_path).parts:
        raise ValueError("unsafe asset URL")
    name = Path(decoded_path).name
    if not name or name in {".", ".."}:
        raise ValueError("unsafe asset filename")

    resolved_asset_dir = _require_safe_directory(asset_dir, "asset")
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:12]
    raw_suffix = Path(name).suffix.lower()
    suffix = raw_suffix if re.fullmatch(r"\.[a-z0-9]{1,10}", raw_suffix) else ".bin"
    destination = resolved_asset_dir / f"{digest}{suffix}"
    if destination.parent != resolved_asset_dir or _is_symlink(destination):
        raise ValueError("unsafe asset path")
    return destination


def safe_article_dir(library_dir: Path, article_id: str) -> Path:
    if not re.fullmatch(r"[a-zA-Z0-9_.:-]+", article_id):
        raise ValueError("unsafe article ID")
    resolved_library_dir = _require_safe_directory(library_dir, "article")
    directory = (resolved_library_dir / article_id).resolve()
    if directory.parent != resolved_library_dir or _is_symlink(resolved_library_dir / article_id):
        raise ValueError("unsafe article path")
    return directory


def atomic_write(path: Path, content: bytes) -> None:
    if _is_symlink(path):
        raise ValueError("unsafe atomic write target")
    path.parent.mkdir(parents=True, exist_ok=True)
    _require_safe_directory(path.parent, "atomic write")
    handle, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _validate_metadata(metadata: dict[str, str]) -> None:
    for key, value in metadata.items():
        if not _METADATA_KEY.fullmatch(key):
            raise ValueError(f"unsafe metadata key: {key!r}")
        if key in _RESERVED_METADATA_KEYS:
            raise ValueError(f"reserved metadata key: {key}")
        if not isinstance(value, str):
            raise TypeError(f"metadata value must be text: {key}")


def write_source(
    directory: Path,
    markdown: str,
    metadata: dict[str, str],
) -> tuple[Path, Path, str]:
    _validate_metadata(metadata)
    _require_safe_directory(directory, "article")
    directory.mkdir(parents=True, exist_ok=True)

    source = directory / "source.md"
    metadata_path = directory / "metadata.md"
    source_bytes = markdown.encode("utf-8")
    digest = hashlib.sha256(source_bytes).hexdigest()
    retrieved_at = datetime.now(UTC).isoformat()
    metadata_lines = ["# Source metadata", ""]
    for key in sorted(metadata):
        metadata_lines.append(f"- {key}: {json.dumps(metadata[key], ensure_ascii=False)}")
    metadata_lines.append(f"- retrieved_at: {json.dumps(retrieved_at)}")
    metadata_lines.append(f"- source_sha256: {json.dumps(digest)}")
    metadata_bytes = ("\n".join(metadata_lines) + "\n").encode("utf-8")

    atomic_write(source, source_bytes)
    atomic_write(metadata_path, metadata_bytes)
    return source, metadata_path, digest


def _load_metadata(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("- "):
            continue
        if ": " not in line:
            raise ValueError("malformed source metadata")
        key, raw = line[2:].split(": ", 1)
        if not _METADATA_KEY.fullmatch(key) or key in values:
            raise ValueError("malformed source metadata")
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("malformed source metadata") from exc
        if not isinstance(value, str):
            raise TypeError("source metadata value must be text")
        values[key] = value
    missing = _REQUIRED_METADATA_KEYS - values.keys()
    if missing:
        raise ValueError(f"missing source metadata: {', '.join(sorted(missing))}")
    return values


def load_stored_article(directory: Path) -> StoredArticle:
    _require_safe_directory(directory, "article")
    source_path = directory / "source.md"
    metadata_path = directory / "metadata.md"
    if _is_symlink(source_path) or _is_symlink(metadata_path):
        raise ValueError("unsafe stored article path")
    values = _load_metadata(metadata_path)

    source_digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
    if source_digest != values["source_sha256"]:
        raise ValueError("source hash mismatch")
    try:
        published_at = datetime.fromisoformat(values["published_at"])
        status = ArticleStatus(values["status"])
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid source metadata") from exc
    if published_at.tzinfo is None:
        raise ValueError("invalid source metadata")

    article = RemoteArticle(
        article_id=values["article_id"],
        account_id=values["account_id"],
        account_name=values["account_name"],
        title=values["title"],
        source_url=values["source_url"],
        published_at=published_at,
    )
    assets_dir = directory / "assets"
    if _is_symlink(assets_dir):
        raise ValueError("unsafe stored article assets")
    asset_paths: tuple[Path, ...] = ()
    if assets_dir.is_dir():
        paths = sorted(assets_dir.iterdir())
        if any(path.is_symlink() for path in paths):
            raise ValueError("unsafe stored article asset")
        asset_paths = tuple(path for path in paths if path.is_file())

    ocr_path = directory / "ocr.md"
    if _is_symlink(ocr_path):
        raise ValueError("unsafe stored article OCR")
    return StoredArticle(
        article=article,
        directory=directory,
        source_path=source_path,
        metadata_path=metadata_path,
        asset_paths=asset_paths,
        ocr_path=ocr_path if ocr_path.exists() else None,
        source_sha256=values["source_sha256"],
        status=status,
    )
