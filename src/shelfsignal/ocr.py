from __future__ import annotations

import hashlib
import os
import re
import secrets
import stat
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from importlib.resources import as_file, files
from pathlib import Path

from .content import _open_directory, atomic_write, ensure_safe_directory

_MAX_IMAGE_DIMENSION = 100_000
_MAX_IMAGE_BYTES = 512 * 1024 * 1024
_MAX_HELPER_BYTES = 128 * 1024 * 1024
_MAX_OCR_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class ImageEvidence:
    path: Path
    width: int
    height: int

    def __post_init__(self) -> None:
        if not isinstance(self.path, Path):
            raise TypeError("image path must be a Path")
        for value in (self.width, self.height):
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or value <= 0
                or value > _MAX_IMAGE_DIMENSION
            ):
                raise ValueError("image dimensions are outside the supported range")

    @property
    def area(self) -> int:
        return self.width * self.height


def should_run_ocr(text_length: int, images: tuple[ImageEvidence, ...]) -> bool:
    if not isinstance(text_length, int) or isinstance(text_length, bool) or text_length < 0:
        raise ValueError("text length must be a non-negative integer")
    meaningful = tuple(item for item in images if item.area >= 320 * 320)
    total_area = sum(item.area for item in meaningful)
    return bool(meaningful and (text_length < 300 or total_area >= 4_000_000))


def slice_ranges(
    height: int, chunk: int = 2000, overlap: int = 100
) -> tuple[tuple[int, int], ...]:
    values = (height, chunk, overlap)
    if any(not isinstance(value, int) or isinstance(value, bool) for value in values):
        raise ValueError("slice dimensions must be integers")
    if height <= 0 or height > _MAX_IMAGE_DIMENSION:
        raise ValueError("image height is outside the supported range")
    if chunk <= 0 or chunk > _MAX_IMAGE_DIMENSION:
        raise ValueError("slice chunk is outside the supported range")
    if overlap < 0 or overlap >= chunk:
        raise ValueError("slice overlap must be smaller than the chunk")
    if height <= chunk:
        return ((0, height),)
    ranges: list[tuple[int, int]] = []
    start = 0
    while start < height:
        end = min(start + chunk, height)
        ranges.append((start, end))
        if end == height:
            break
        start = end - overlap
    return tuple(ranges)


def _open_regular_file(
    path: Path,
    *,
    label: str,
    max_bytes: int,
    require_executable: bool = False,
) -> int:
    if not path.is_absolute() or ".." in path.parts or path.name in {"", ".", ".."}:
        raise ValueError(f"unsafe {label}")
    parent_fd, normalized_parent = _open_directory(path.parent, label)
    normalized_path = normalized_parent / path.name
    if normalized_path != path:
        os.close(parent_fd)
        raise ValueError(f"unsafe {label}")
    try:
        descriptor = os.open(
            path.name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
    except OSError as exc:
        os.close(parent_fd)
        raise ValueError(f"unsafe {label}") from exc
    try:
        details = os.fstat(descriptor)
        current = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(details.st_mode)
            or (details.st_dev, details.st_ino) != (current.st_dev, current.st_ino)
            or details.st_size > max_bytes
            or (require_executable and not details.st_mode & 0o111)
        ):
            raise ValueError(f"unsafe {label}")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise
    finally:
        os.close(parent_fd)


def _validate_regular_file(
    path: Path,
    *,
    label: str,
    max_bytes: int,
    require_executable: bool = False,
) -> None:
    descriptor = _open_regular_file(
        path,
        label=label,
        max_bytes=max_bytes,
        require_executable=require_executable,
    )
    os.close(descriptor)


def image_sha256(path: Path) -> str:
    descriptor = _open_regular_file(path, label="image", max_bytes=_MAX_IMAGE_BYTES)
    digest = hashlib.sha256()
    try:
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def image_evidence(path: Path) -> ImageEvidence:
    _validate_regular_file(path, label="image", max_bytes=_MAX_IMAGE_BYTES)
    result = subprocess.run(
        ["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(path)],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    width_match = re.search(r"^\s*pixelWidth:\s*(\d+)\s*$", result.stdout, re.MULTILINE)
    height_match = re.search(r"^\s*pixelHeight:\s*(\d+)\s*$", result.stdout, re.MULTILINE)
    if width_match is None or height_match is None:
        raise ValueError(f"unable to read image dimensions: {path.name}")
    try:
        return ImageEvidence(path, int(width_match.group(1)), int(height_match.group(1)))
    except ValueError as exc:
        raise ValueError(f"unsupported image dimensions: {path.name}") from exc


def run_vision_ocr(helper: Path, image: Path) -> str:
    _validate_regular_file(
        helper,
        label="OCR helper",
        max_bytes=_MAX_HELPER_BYTES,
        require_executable=True,
    )
    _validate_regular_file(image, label="image", max_bytes=_MAX_IMAGE_BYTES)
    result = subprocess.run(
        [str(helper), str(image)],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    output = result.stdout.strip()
    if len(output.encode("utf-8")) > _MAX_OCR_BYTES:
        raise ValueError("OCR output exceeds the supported size")
    return output


def _existing_helper(helper: Path) -> bool:
    try:
        details = os.lstat(helper)
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(details.st_mode) or not details.st_mode & 0o111:
        raise ValueError("unsafe OCR helper cache")
    _validate_regular_file(
        helper,
        label="OCR helper cache",
        max_bytes=_MAX_HELPER_BYTES,
        require_executable=True,
    )
    return True


def ensure_helper(build_dir: Path) -> Path:
    build_dir = ensure_safe_directory(build_dir, label="OCR helper build")
    helper = build_dir / "shelfsignal-vision-ocr"
    if _existing_helper(helper):
        return helper

    temporary = build_dir / f".shelfsignal-vision-ocr.{secrets.token_hex(8)}"
    resource = files("shelfsignal").joinpath("resources/vision_ocr.swift")
    try:
        with as_file(resource) as source:
            subprocess.run(
                ["swiftc", str(source), "-o", str(temporary)],
                check=True,
                capture_output=True,
                text=True,
                timeout=120,
            )
        _validate_regular_file(
            temporary,
            label="compiled OCR helper",
            max_bytes=_MAX_HELPER_BYTES,
        )
        os.chmod(temporary, 0o700, follow_symlinks=False)
        if _existing_helper(helper):
            return helper
        os.replace(temporary, helper)
        return helper
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _read_cached_text(path: Path) -> str:
    descriptor = _open_regular_file(path, label="OCR cache file", max_bytes=_MAX_OCR_BYTES)
    chunks: list[bytes] = []
    try:
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
    finally:
        os.close(descriptor)
    try:
        return b"".join(chunks).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("invalid OCR cache text") from exc


def _bounded_ocr_text(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("OCR runner must return text")
    if len(value.encode("utf-8")) > _MAX_OCR_BYTES:
        raise ValueError("OCR output exceeds the supported size")
    return value


def _safe_heading(path: Path) -> str:
    name = path.name.replace("\r", " ").replace("\n", " ").strip()
    return name[:255] or "image"


def ocr_article(
    article_dir: Path,
    images: tuple[ImageEvidence, ...],
    text_length: int,
    cache_dir: Path,
    runner: Callable[[Path], str],
) -> Path | None:
    if not should_run_ocr(text_length, images):
        return None
    article_dir = ensure_safe_directory(article_dir, label="article OCR")
    cache_dir = ensure_safe_directory(cache_dir, label="OCR cache")
    sections = ["# OCR-derived evidence", ""]
    for image in images:
        sections.extend([f"## {_safe_heading(image.path)}", ""])
        try:
            digest = image_sha256(image.path)
            cache = cache_dir / f"{digest}.txt"
            if os.path.lexists(cache):
                text = _read_cached_text(cache)
            else:
                text = _bounded_ocr_text(runner(image.path))
                atomic_write(cache, (text.rstrip() + "\n").encode("utf-8"))
            sections.extend([text.strip(), ""])
        except Exception as exc:  # noqa: BLE001 - one image failure remains visible and nonfatal
            sections.extend([f"OCR incomplete: {type(exc).__name__}", ""])
    destination = article_dir / "ocr.md"
    atomic_write(destination, ("\n".join(sections).rstrip() + "\n").encode("utf-8"))
    return destination
