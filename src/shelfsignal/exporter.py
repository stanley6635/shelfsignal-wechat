from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import re
import secrets
import stat
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

from .content import _load_metadata, ensure_safe_directory
from .weread import _raster_kind

ALLOWED_FILES = ("source.md", "metadata.md", "ocr.md")

_SAFE_ARTICLE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_SAFE_ASSET_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}\Z")
_MARKDOWN_LINK = re.compile(r"!?\[[^\]\n]*\]\(([^)\n]+)\)")
_MARKDOWN_REFERENCE = re.compile(r"(?m)^\s{0,3}\[[^\]\n]+\]:\s*(\S+)")
_MARKDOWN_AUTOLINK = re.compile(r"<([A-Za-z][A-Za-z0-9+.-]{1,31}:[^<>\s]*)>")
_RAW_HTML_TAG = re.compile(r"</?[A-Za-z][^>\n]*>")
_RAW_HTML_BLOCK_OPENER = re.compile(r"(?m)^[ \t]{0,3}<(?:/?[A-Za-z]|!|\?)")
_RASTER_SUFFIXES = {
    ".avif",
    ".bmp",
    ".gif",
    ".heic",
    ".jpeg",
    ".jpg",
    ".png",
    ".tif",
    ".tiff",
    ".webp",
}
_MAX_ARTICLES = 2_000
_MAX_FILES = 20_000
_MAX_SOURCE_BYTES = 128 * 1024 * 1024
_MAX_METADATA_BYTES = 1024 * 1024
_MAX_OCR_BYTES = 8 * 1024 * 1024
_MAX_ASSET_BYTES = 25 * 1024 * 1024
_MAX_EXPORT_BYTES = 512 * 1024 * 1024


class ExportError(ValueError):
    """The requested portable export cannot be produced safely."""


def _directory_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


def _normalized_absolute(path: Path, label: str) -> Path:
    if not isinstance(path, Path) or not path.is_absolute() or ".." in path.parts:
        raise ExportError(f"unsafe {label} path")
    normalized = Path(os.path.normpath(os.fspath(path)))
    if normalized != path or normalized == Path(normalized.anchor):
        raise ExportError(f"unsafe {label} path")
    return normalized


def _open_absolute_directory(path: Path, label: str) -> int:
    path = _normalized_absolute(path, label)
    flags = _directory_flags()
    try:
        descriptor = os.open(path.anchor, flags)
    except OSError as exc:
        raise ExportError(f"unsafe {label} path") from exc
    try:
        for part in path.parts[1:]:
            try:
                child = os.open(part, flags, dir_fd=descriptor)
            except OSError as exc:
                raise ExportError(f"unsafe {label} directory") from exc
            try:
                details = os.fstat(child)
                current = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
                if (
                    not stat.S_ISDIR(current.st_mode)
                    or (details.st_dev, details.st_ino)
                    != (current.st_dev, current.st_ino)
                ):
                    raise ExportError(f"unsafe {label} directory")
            except BaseException:
                os.close(child)
                raise
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_child_directory(parent_fd: int, name: str, label: str) -> int:
    if not name or name.startswith(".") or "/" in name or "\\" in name:
        raise ExportError(f"unsafe {label} directory")
    try:
        descriptor = os.open(name, _directory_flags(), dir_fd=parent_fd)
    except OSError as exc:
        raise ExportError(f"unsafe {label} directory") from exc
    try:
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(current.st_mode)
            or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise ExportError(f"unsafe {label} directory")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _read_regular_at(
    directory_fd: int,
    name: str,
    *,
    label: str,
    max_bytes: int,
    optional: bool = False,
) -> bytes | None:
    if not name or name.startswith(".") or "/" in name or "\\" in name:
        raise ExportError(f"unsafe {label} filename")
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
            dir_fd=directory_fd,
        )
    except FileNotFoundError:
        if optional:
            return None
        raise ExportError(f"missing {label}") from None
    except OSError as exc:
        raise ExportError(f"unsafe {label} file") from exc
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
            raise ExportError(f"unsafe {label} file")
        if details.st_size > max_bytes:
            raise ExportError(f"{label} file is too large")
        content = bytearray()
        while len(content) <= max_bytes:
            chunk = os.read(descriptor, min(64 * 1024, max_bytes + 1 - len(content)))
            if not chunk:
                break
            content.extend(chunk)
        if len(content) > max_bytes:
            raise ExportError(f"{label} file is too large")
        return bytes(content)
    finally:
        os.close(descriptor)


def _add_blob(
    plan: dict[PurePosixPath, bytes],
    path: PurePosixPath,
    content: bytes,
    total_bytes: int,
) -> int:
    if path in plan:
        raise ExportError(f"duplicate export path: {path}")
    if len(plan) >= _MAX_FILES:
        raise ExportError(f"too many export files; maximum is {_MAX_FILES}")
    total_bytes += len(content)
    if total_bytes > _MAX_EXPORT_BYTES:
        raise ExportError("export exceeds the aggregate size limit")
    plan[path] = content
    return total_bytes


def _bounded_names(directory_fd: int, limit: int, label: str) -> list[str]:
    names: list[str] = []
    with os.scandir(directory_fd) as entries:
        for entry in entries:
            if len(names) >= limit:
                raise ExportError(f"too many {label} entries")
            names.append(entry.name)
    return sorted(names)


def _collect_article(
    library_fd: int,
    article_id: str,
    plan: dict[PurePosixPath, bytes],
    total_bytes: int,
) -> int:
    article_fd = _open_child_directory(library_fd, article_id, "article")
    try:
        source = _read_regular_at(
            article_fd,
            "source.md",
            label=f"article {article_id} source.md",
            max_bytes=_MAX_SOURCE_BYTES,
        )
        metadata = _read_regular_at(
            article_fd,
            "metadata.md",
            label=f"article {article_id} metadata.md",
            max_bytes=_MAX_METADATA_BYTES,
        )
        ocr = _read_regular_at(
            article_fd,
            "ocr.md",
            label=f"article {article_id} ocr.md",
            max_bytes=_MAX_OCR_BYTES,
            optional=True,
        )
        assert source is not None and metadata is not None
        try:
            values = _load_metadata(metadata.decode("utf-8"))
        except (UnicodeDecodeError, TypeError, ValueError) as exc:
            raise ExportError(f"invalid article {article_id} metadata") from exc
        if values["article_id"] != article_id:
            raise ExportError(f"article {article_id} metadata ID mismatch")
        if values["source_sha256"] != hashlib.sha256(source).hexdigest():
            raise ExportError(f"article {article_id} source hash mismatch")

        for name, content in (
            ("source.md", source),
            ("metadata.md", metadata),
            ("ocr.md", ocr),
        ):
            if content is not None:
                total_bytes = _add_blob(
                    plan,
                    PurePosixPath("articles", article_id, name),
                    content,
                    total_bytes,
                )

        try:
            assets_fd = os.open("assets", _directory_flags(), dir_fd=article_fd)
        except FileNotFoundError:
            return total_bytes
        except OSError as exc:
            raise ExportError(f"unsafe article {article_id} assets directory") from exc
        try:
            current = os.stat("assets", dir_fd=article_fd, follow_symlinks=False)
            opened = os.fstat(assets_fd)
            if (
                not stat.S_ISDIR(current.st_mode)
                or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
            ):
                raise ExportError(f"unsafe article {article_id} assets directory")
            for name in _bounded_names(
                assets_fd,
                _MAX_FILES - len(plan),
                f"article {article_id} asset",
            ):
                if name.startswith("."):
                    raise ExportError(f"hidden article {article_id} asset is not exportable")
                if not _SAFE_ASSET_NAME.fullmatch(name):
                    raise ExportError(f"unsafe article {article_id} asset filename")
                suffix = Path(name).suffix.lower()
                if suffix not in {*_RASTER_SUFFIXES, ".bin"}:
                    raise ExportError(f"article {article_id} asset is not a raster file")
                content = _read_regular_at(
                    assets_fd,
                    name,
                    label=f"article {article_id} asset",
                    max_bytes=_MAX_ASSET_BYTES,
                )
                assert content is not None
                kind = _raster_kind(content)
                expected = {
                    ".jpg": "jpeg",
                    ".jpeg": "jpeg",
                    ".tif": "tiff",
                    ".tiff": "tiff",
                }.get(suffix, suffix.removeprefix("."))
                if kind is None or (suffix != ".bin" and kind != expected):
                    raise ExportError(f"article {article_id} asset raster signature mismatch")
                total_bytes = _add_blob(
                    plan,
                    PurePosixPath("articles", article_id, "assets", name),
                    content,
                    total_bytes,
                )
        finally:
            os.close(assets_fd)
    finally:
        os.close(article_fd)
    return total_bytes


def _link_target(raw: str) -> str:
    target = raw.strip()
    if target.startswith("<") and target.endswith(">"):
        target = target[1:-1].strip()
    # Markdown permits an optional quoted title after a whitespace separator.
    if " \"" in target:
        target = target.split(" \"", 1)[0]
    elif " '" in target:
        target = target.split(" '", 1)[0]
    return target


def _validate_markdown_links(plan: dict[PurePosixPath, bytes]) -> None:
    available = set(plan)
    for markdown_path, content in plan.items():
        if markdown_path.suffix != ".md":
            continue
        try:
            markdown = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ExportError(f"{markdown_path} must be UTF-8 Markdown") from exc
        autolinks = list(_MARKDOWN_AUTOLINK.finditer(markdown))
        without_autolinks = _MARKDOWN_AUTOLINK.sub("", markdown)
        if _RAW_HTML_TAG.search(without_autolinks) or _RAW_HTML_BLOCK_OPENER.search(
            without_autolinks
        ):
            raise ExportError(f"raw HTML is not allowed in {markdown_path}")
        matches = [
            *_MARKDOWN_LINK.finditer(markdown),
            *_MARKDOWN_REFERENCE.finditer(markdown),
            *autolinks,
        ]
        for match in matches:
            target = _link_target(match.group(1))
            if not target:
                raise ExportError(f"unsafe Markdown link in {markdown_path}")
            try:
                parsed = urlsplit(target)
            except ValueError as exc:
                raise ExportError(f"unsafe Markdown link in {markdown_path}") from exc
            if parsed.scheme or parsed.netloc:
                if parsed.scheme == "https" and parsed.netloc:
                    continue
                raise ExportError(f"unsafe Markdown link in {markdown_path}")
            if not parsed.path:
                continue
            decoded = unquote(parsed.path)
            relative = PurePosixPath(decoded)
            if (
                "\\" in decoded
                or relative.is_absolute()
                or any(part in {"", ".", ".."} for part in relative.parts)
            ):
                raise ExportError(f"unsafe Markdown link in {markdown_path}")
            resolved = markdown_path.parent.joinpath(relative)
            if resolved not in available:
                raise ExportError(f"unresolved Markdown link in {markdown_path}")


def _build_plan(article_ids: tuple[str, ...], library_dir: Path) -> dict[PurePosixPath, bytes]:
    if len(article_ids) > _MAX_ARTICLES:
        raise ExportError(f"too many selected articles; maximum is {_MAX_ARTICLES}")
    if len(article_ids) != len(set(article_ids)):
        raise ExportError("duplicate selected article IDs")
    for article_id in article_ids:
        if not isinstance(article_id, str) or not _SAFE_ARTICLE_ID.fullmatch(article_id):
            raise ExportError(f"unsafe article ID: {article_id!r}")

    library_fd = _open_absolute_directory(library_dir, "library")
    plan: dict[PurePosixPath, bytes] = {}
    total_bytes = 0
    try:
        for article_id in article_ids:
            total_bytes = _collect_article(
                library_fd,
                article_id,
                plan,
                total_bytes,
            )
    finally:
        os.close(library_fd)

    lines = ["# Selected WeChat articles", ""]
    lines.extend(
        f"- [{article_id}](articles/{article_id}/source.md)"
        for article_id in article_ids
    )
    _add_blob(
        plan,
        PurePosixPath("index.md"),
        ("\n".join(lines) + "\n").encode(),
        total_bytes,
    )
    _validate_markdown_links(plan)
    return plan


def _mkdir_child(parent_fd: int, name: str) -> int:
    if not name or name.startswith(".") or "/" in name or "\\" in name:
        raise ExportError("unsafe staged export directory")
    try:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
    except FileExistsError:
        pass
    descriptor = os.open(name, _directory_flags(), dir_fd=parent_fd)
    os.fchmod(descriptor, 0o700)
    return descriptor


def _open_or_create_relative_directory(root_fd: int, parts: tuple[str, ...]) -> int:
    descriptor = os.dup(root_fd)
    try:
        for part in parts:
            child = _mkdir_child(descriptor, part)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _write_file_at(directory_fd: int, name: str, content: bytes) -> None:
    if not name or name.startswith(".") or "/" in name or "\\" in name:
        raise ExportError("unsafe staged export filename")
    descriptor = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
        dir_fd=directory_fd,
    )
    try:
        os.fchmod(descriptor, 0o600)
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short export write")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_plan(stage_fd: int, plan: dict[PurePosixPath, bytes]) -> None:
    for path in sorted(plan, key=lambda item: item.as_posix()):
        parent_fd = _open_or_create_relative_directory(stage_fd, path.parts[:-1])
        try:
            _write_file_at(parent_fd, path.name, plan[path])
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    os.fsync(stage_fd)


def _remove_staging_tree(parent_fd: int, name: str) -> None:
    """Remove only a nonce-named staging tree created by this module."""
    if not re.fullmatch(r"\.[A-Za-z0-9_.:-]+\.staging-[0-9a-f]{16}", name):
        raise ExportError("refusing to remove an untrusted staging directory")
    try:
        root_fd = os.open(name, _directory_flags(), dir_fd=parent_fd)
    except FileNotFoundError:
        return
    try:
        _remove_children(root_fd)
    finally:
        os.close(root_fd)
    os.rmdir(name, dir_fd=parent_fd)


def _remove_children(directory_fd: int) -> None:
    for name in os.listdir(directory_fd):
        details = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISREG(details.st_mode):
            os.unlink(name, dir_fd=directory_fd)
        elif stat.S_ISDIR(details.st_mode):
            child = os.open(name, _directory_flags(), dir_fd=directory_fd)
            try:
                _remove_children(child)
            finally:
                os.close(child)
            os.rmdir(name, dir_fd=directory_fd)
        else:
            raise ExportError("unsafe object found in staged export")


def _read_existing_tree(
    directory_fd: int,
    prefix: PurePosixPath | None = None,
    budget: list[int] | None = None,
) -> dict[PurePosixPath, bytes]:
    if prefix is None:
        prefix = PurePosixPath()
    if budget is None:
        budget = [0, 0]
    if stat.S_IMODE(os.fstat(directory_fd).st_mode) != 0o700:
        raise ExportError("unsafe destination directory permissions")
    result: dict[PurePosixPath, bytes] = {}
    for name in _bounded_names(
        directory_fd,
        _MAX_FILES - budget[0] + 1,
        "destination",
    ):
        budget[0] += 1
        if budget[0] > _MAX_FILES:
            raise ExportError("unsafe destination exceeds export bounds")
        if name.startswith(".") or "/" in name or "\\" in name:
            raise ExportError("unsafe destination contains hidden content")
        details = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        path = prefix / name
        if stat.S_ISDIR(details.st_mode):
            child = _open_child_directory(directory_fd, name, "destination")
            try:
                nested = _read_existing_tree(child, path, budget)
            finally:
                os.close(child)
            result.update(nested)
            continue
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_nlink != 1
            or stat.S_IMODE(details.st_mode) != 0o600
        ):
            raise ExportError("unsafe destination contains nonregular content")
        content = _read_regular_at(
            directory_fd,
            name,
            label="destination",
            max_bytes=_MAX_EXPORT_BYTES,
        )
        assert content is not None
        budget[1] += len(content)
        if budget[1] > _MAX_EXPORT_BYTES:
            raise ExportError("unsafe destination exceeds export bounds")
        result[path] = content
    return result


def _destination_state(parent_fd: int, name: str) -> int | None:
    try:
        details = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(details.st_mode):
        raise ExportError("unsafe destination path")
    return _open_child_directory(parent_fd, name, "destination")


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _rename_exclusive_at(parent_fd: int, source: str, destination: str) -> None:
    """Atomically publish without replacing even an empty destination directory."""
    libc = ctypes.CDLL(None, use_errno=True)
    renameatx = getattr(libc, "renameatx_np", None)
    if renameatx is None:
        raise ExportError("exclusive directory publication is unavailable")
    renameatx.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameatx.restype = ctypes.c_int
    # Darwin's RENAME_EXCL prevents a check-to-rename race from replacing data.
    if renameatx(parent_fd, os.fsencode(source), parent_fd, os.fsencode(destination), 0x4):
        error = ctypes.get_errno()
        if error in {errno.EEXIST, errno.ENOTEMPTY}:
            raise ExportError("destination appeared while export was staged")
        raise OSError(error, os.strerror(error), destination)


def export_selected(
    article_ids: tuple[str, ...],
    library_dir: Path,
    destination: Path,
) -> Path:
    """Create a portable, selected-only bundle without overwriting prior exports.

    An exact existing bundle is an idempotent success. A differing destination
    is deliberately left untouched because replacing it would delete user data;
    callers should choose a new run-scoped destination instead.
    """
    if not isinstance(article_ids, tuple):
        raise TypeError("article_ids must be a tuple")
    library_dir = _normalized_absolute(library_dir, "library")
    destination = _normalized_absolute(destination, "destination")
    if not _SAFE_ARTICLE_ID.fullmatch(destination.name):
        raise ExportError("unsafe destination name")
    if _is_within(destination, library_dir) or _is_within(library_dir, destination):
        raise ExportError("library and destination must be separate")

    plan = _build_plan(article_ids, library_dir)
    try:
        parent = ensure_safe_directory(destination.parent, label="export destination")
    except ValueError as exc:
        raise ExportError("unsafe destination parent") from exc
    parent_fd = _open_absolute_directory(parent, "destination parent")
    stage_name = f".{destination.name}.staging-{secrets.token_hex(8)}"
    stage_created = False
    try:
        existing_fd = _destination_state(parent_fd, destination.name)
        if existing_fd is not None:
            try:
                existing = _read_existing_tree(existing_fd)
            finally:
                os.close(existing_fd)
            if existing == plan:
                return destination
            raise ExportError(
                "destination already exists with different content; use a new destination"
            )

        os.mkdir(stage_name, 0o700, dir_fd=parent_fd)
        stage_created = True
        stage_fd = os.open(stage_name, _directory_flags(), dir_fd=parent_fd)
        try:
            os.fchmod(stage_fd, 0o700)
            _write_plan(stage_fd, plan)
        finally:
            os.close(stage_fd)

        appeared_fd = _destination_state(parent_fd, destination.name)
        if appeared_fd is not None:
            os.close(appeared_fd)
            raise ExportError("destination appeared while export was staged")
        _rename_exclusive_at(parent_fd, stage_name, destination.name)
        stage_created = False
        os.fsync(parent_fd)
        return destination
    finally:
        if stage_created:
            _remove_staging_tree(parent_fd, stage_name)
            os.fsync(parent_fd)
        os.close(parent_fd)
