from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import secrets
import stat
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
_SAFE_ARTICLE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_APPROVED_IMAGE_HOSTS = {
    "mmbiz.qpic.cn",
    "mmbiz.qlogo.cn",
    "res.wx.qq.com",
    "weread.qq.com",
    "cdn.weread.qq.com",
    "wfqqreader-1252317822.image.myqcloud.com",
}
_RASTER_SUFFIXES = {".avif", ".bmp", ".gif", ".heic", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}


@dataclass(frozen=True)
class NormalizedContent:
    markdown: str
    image_urls: tuple[str, ...]


def _safe_https_url(source: str, *, image: bool = False) -> bool:
    if "\\" in source:
        return False
    try:
        parsed = urlparse(source)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return False
    if (
        parsed.scheme != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or hostname == "localhost"
    ):
        return False
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        return False
    if not image:
        return True
    host = hostname.rstrip(".").lower()
    return host.endswith(".invalid") or any(
        host == approved or host.endswith(f".{approved}")
        for approved in _APPROVED_IMAGE_HOSTS
    )


def _dimension(value: str | None) -> int | None:
    if value is None or len(value) > 6 or not value.isdecimal():
        return 0
    return min(int(value), 100_000)


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
        source = values.get("src") or values.get("data-src")
        width = _dimension(values.get("width") or values.get("data-w"))
        height = _dimension(values.get("height") or values.get("data-h"))
        classes = (values.get("class") or "").lower().split()
        is_avatar = any("avatar" in token for token in classes)
        if (
            source
            and not is_avatar
            and max(width, height) >= 320
            and _safe_https_url(source, image=True)
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


def _directory_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


def _normalized_absolute(path: Path, label: str) -> Path:
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError(f"unsafe {label} path")
    normalized = Path(os.path.normpath(os.fspath(path)))
    if normalized != path or normalized == Path(normalized.anchor):
        raise ValueError(f"unsafe {label} path")
    return normalized


def _verify_directory_at(parent_fd: int, name: str, child_fd: int, label: str) -> None:
    current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    opened = os.fstat(child_fd)
    if (
        not stat.S_ISDIR(current.st_mode)
        or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
    ):
        raise ValueError(f"unsafe {label} directory")


def _open_directory(path: Path, label: str, *, create_final: bool = False) -> tuple[int, Path]:
    path = _normalized_absolute(path, label)
    try:
        parent_fd = os.open(path.anchor, _directory_flags())
    except OSError as exc:
        raise ValueError(f"unsafe {label} path") from exc
    current = Path(path.anchor)
    try:
        for index, name in enumerate(path.parts[1:]):
            current /= name
            is_final = index == len(path.parts[1:]) - 1
            if is_final and create_final:
                try:
                    os.mkdir(name, 0o700, dir_fd=parent_fd)
                except FileExistsError:
                    pass
                except OSError as exc:
                    raise ValueError(f"unsafe {label} directory") from exc
            try:
                child_fd = os.open(name, _directory_flags(), dir_fd=parent_fd)
            except OSError as exc:
                raise ValueError(f"unsafe {label} directory: {current}") from exc
            try:
                _verify_directory_at(parent_fd, name, child_fd, label)
                if is_final and create_final:
                    os.fchmod(child_fd, 0o700)
            except BaseException:
                os.close(child_fd)
                raise
            os.close(parent_fd)
            parent_fd = child_fd
        return parent_fd, path
    except BaseException:
        os.close(parent_fd)
        raise


def safe_asset_path(asset_dir: Path, source: str) -> Path:
    if not _safe_https_url(source, image=True):
        raise ValueError("unsafe asset URL")
    parsed = urlparse(source)
    decoded_path = unquote(parsed.path)
    if "\\" in decoded_path or ".." in Path(decoded_path).parts:
        raise ValueError("unsafe asset URL")
    name = Path(decoded_path).name
    if not name or name in {".", ".."}:
        raise ValueError("unsafe asset filename")

    descriptor, asset_dir = _open_directory(asset_dir, "asset", create_final=True)
    os.close(descriptor)
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:12]
    raw_suffix = Path(name).suffix.lower()
    suffix = raw_suffix if raw_suffix in _RASTER_SUFFIXES else ".bin"
    return asset_dir / f"{digest}{suffix}"


def safe_article_dir(library_dir: Path, article_id: str) -> Path:
    if not _SAFE_ARTICLE_ID.fullmatch(article_id):
        raise ValueError("unsafe article ID")
    library_fd, library_dir = _open_directory(library_dir, "article")
    os.close(library_fd)
    directory = library_dir / article_id
    descriptor, directory = _open_directory(directory, "article", create_final=True)
    os.close(descriptor)
    return directory


def atomic_write(path: Path, content: bytes) -> None:
    parent_fd, _ = _open_directory(path.parent, "atomic write")
    temporary = f".{path.name}.{secrets.token_hex(8)}"
    try:
        _write_staged_file(parent_fd, temporary, content)
        _require_regular_or_missing(parent_fd, path.name)
        os.replace(temporary, path.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        os.fsync(parent_fd)
    finally:
        _unlink_if_exists(parent_fd, temporary)
        os.close(parent_fd)


def _write_staged_file(directory_fd: int, name: str, content: bytes) -> None:
    descriptor = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
        dir_fd=directory_fd,
    )
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(os.dup(descriptor), "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)


def _require_regular_or_missing(directory_fd: int, name: str) -> bool:
    try:
        details = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(details.st_mode):
        raise ValueError("unsafe stored article file")
    return True


def _unlink_if_exists(directory_fd: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=directory_fd)
    except FileNotFoundError:
        pass


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
    directory_fd, directory = _open_directory(directory, "article", create_final=True)

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

    source_temp = f".source.md.{secrets.token_hex(8)}"
    metadata_temp = f".metadata.md.{secrets.token_hex(8)}"
    source_backup = f".source.md.backup.{secrets.token_hex(8)}"
    metadata_backup = f".metadata.md.backup.{secrets.token_hex(8)}"
    staged = (source_temp, metadata_temp)
    backups = (source_backup, metadata_backup)
    source_moved = metadata_moved = source_committed = metadata_committed = False
    preserve_backups = False
    pair_durable = False
    try:
        source_exists = _require_regular_or_missing(directory_fd, source.name)
        metadata_exists = _require_regular_or_missing(directory_fd, metadata_path.name)
        if source_exists != metadata_exists:
            raise ValueError("incomplete stored article pair")
        _write_staged_file(directory_fd, source_temp, source_bytes)
        _write_staged_file(directory_fd, metadata_temp, metadata_bytes)
        if source_exists:
            os.replace(source.name, source_backup, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
            source_moved = True
            os.replace(
                metadata_path.name,
                metadata_backup,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
            metadata_moved = True
            os.fsync(directory_fd)
        os.replace(source_temp, source.name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        source_committed = True
        os.replace(
            metadata_temp,
            metadata_path.name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        metadata_committed = True
        os.fsync(directory_fd)
        pair_durable = True
        for backup in backups:
            _unlink_if_exists(directory_fd, backup)
        os.fsync(directory_fd)
    except BaseException as primary_error:
        try:
            if not pair_durable:
                if source_committed:
                    _unlink_if_exists(directory_fd, source.name)
                if metadata_committed:
                    _unlink_if_exists(directory_fd, metadata_path.name)
                if source_moved:
                    os.replace(
                        source_backup,
                        source.name,
                        src_dir_fd=directory_fd,
                        dst_dir_fd=directory_fd,
                    )
                if metadata_moved:
                    os.replace(
                        metadata_backup,
                        metadata_path.name,
                        src_dir_fd=directory_fd,
                        dst_dir_fd=directory_fd,
                    )
                os.fsync(directory_fd)
            else:
                preserve_backups = True
        except Exception as rollback_error:  # noqa: BLE001 - preserve primary commit failure
            preserve_backups = True
            primary_error.add_note(f"stored article rollback also failed: {rollback_error}")
        raise
    finally:
        cleanup = staged if preserve_backups else (*staged, *backups)
        for temporary in cleanup:
            _unlink_if_exists(directory_fd, temporary)
        os.close(directory_fd)
    return source, metadata_path, digest


def _load_metadata(content: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in content.splitlines():
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
        if not isinstance(value, str) or len(value) > 64_000:
            raise TypeError("source metadata value must be text")
        values[key] = value
    missing = _REQUIRED_METADATA_KEYS - values.keys()
    if missing:
        raise ValueError(f"missing source metadata: {', '.join(sorted(missing))}")
    return values


def _read_regular_file_at(directory_fd: int, name: str, *, max_bytes: int) -> bytes:
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory_fd,
        )
    except OSError as exc:
        raise ValueError("unsafe stored article file") from exc
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_size > max_bytes:
            raise ValueError("unsafe stored article file")
        os.fchmod(descriptor, 0o600)
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def load_stored_article(directory: Path) -> StoredArticle:
    directory_fd, directory = _open_directory(directory, "article")
    source_path = directory / "source.md"
    metadata_path = directory / "metadata.md"
    try:
        source_bytes = _read_regular_file_at(
            directory_fd, source_path.name, max_bytes=128 * 1024 * 1024
        )
        metadata_bytes = _read_regular_file_at(
            directory_fd, metadata_path.name, max_bytes=1024 * 1024
        )
        try:
            values = _load_metadata(metadata_bytes.decode("utf-8"))
        except UnicodeDecodeError as exc:
            raise ValueError("malformed source metadata") from exc

        source_digest = hashlib.sha256(source_bytes).hexdigest()
        if source_digest != values["source_sha256"]:
            raise ValueError("source hash mismatch")
        if not _SAFE_ARTICLE_ID.fullmatch(values["article_id"]):
            raise ValueError("invalid source article ID")
        if values["article_id"] != directory.name:
            raise ValueError("source article ID does not match directory")
        try:
            published_at = datetime.fromisoformat(values["published_at"])
            status = ArticleStatus(values["status"])
        except (ValueError, TypeError) as exc:
            raise ValueError("invalid source metadata") from exc
        if published_at.tzinfo is None or not _safe_https_url(values["source_url"]):
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
        asset_paths: tuple[Path, ...] = ()
        try:
            assets_fd = os.open("assets", _directory_flags(), dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise ValueError("unsafe stored article assets") from exc
        else:
            try:
                _verify_directory_at(directory_fd, "assets", assets_fd, "stored article assets")
                os.fchmod(assets_fd, 0o700)
                names = sorted(os.listdir(assets_fd))
                for name in names:
                    try:
                        asset_fd = os.open(
                            name,
                            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                            dir_fd=assets_fd,
                        )
                    except OSError as exc:
                        raise ValueError("unsafe stored article asset") from exc
                    try:
                        if not stat.S_ISREG(os.fstat(asset_fd).st_mode):
                            raise ValueError("unsafe stored article asset")
                        os.fchmod(asset_fd, 0o600)
                    finally:
                        os.close(asset_fd)
                    if "/" in name or "\\" in name:
                        raise ValueError("unsafe stored article asset")
                asset_paths = tuple(assets_dir / name for name in names)
            finally:
                os.close(assets_fd)

        ocr_path = directory / "ocr.md"
        try:
            ocr_details = os.stat(ocr_path.name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            stored_ocr_path = None
        else:
            if not stat.S_ISREG(ocr_details.st_mode):
                raise ValueError("unsafe stored article OCR")
            ocr_fd = os.open(
                ocr_path.name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory_fd,
            )
            try:
                os.fchmod(ocr_fd, 0o600)
            finally:
                os.close(ocr_fd)
            stored_ocr_path = ocr_path
        return StoredArticle(
            article=article,
            directory=directory,
            source_path=source_path,
            metadata_path=metadata_path,
            asset_paths=asset_paths,
            ocr_path=stored_ocr_path,
            source_sha256=values["source_sha256"],
            status=status,
        )
    finally:
        os.close(directory_fd)
