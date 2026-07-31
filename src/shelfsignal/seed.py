from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from .state import StateStore

URL_PATTERN = re.compile(r"https?://[^\s)>]+")
SENTENCE_SUFFIXES = ".,;，。；：！？"
EMPHASIS_DELIMITERS = ("**", "__", "~~", "*", "_", "~")


class SeedError(ValueError):
    pass


@dataclass(frozen=True)
class SeedResult:
    scanned_files: int
    discovered: int
    imported: int


def _require_contained(path: Path, root: Path) -> None:
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SeedError(f"cannot safely resolve archive path: {path}") from exc
    if not resolved.is_relative_to(root):
        raise SeedError(f"archive path escapes selected archive: {path}")


def _archive_files(path: Path) -> list[Path]:
    path = path.expanduser()
    if path.is_symlink():
        raise SeedError(f"archive path is a symbolic link: {path}")
    try:
        mode = path.lstat().st_mode
        root = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SeedError(f"cannot read archive path: {path}") from exc

    if stat.S_ISREG(mode):
        return [path]
    if not stat.S_ISDIR(mode):
        raise SeedError(f"archive path is not a regular file or directory: {path}")

    files: list[Path] = []
    for current, directory_names, file_names in os.walk(path, followlinks=False):
        current_path = Path(current)
        directory_names.sort()
        file_names.sort()
        for directory_name in directory_names:
            directory = current_path / directory_name
            if directory.is_symlink():
                raise SeedError(f"archive path is a symbolic link: {directory}")
            _require_contained(directory, root)
        for file_name in file_names:
            markdown_path = current_path / file_name
            if markdown_path.suffix.lower() != ".md":
                continue
            if markdown_path.is_symlink():
                raise SeedError(f"archive path is a symbolic link: {markdown_path}")
            _require_contained(markdown_path, root)
            files.append(markdown_path)
    return files


def _read_markdown(path: Path) -> str:
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise SeedError(f"archive path is not a regular file: {path}")
        with os.fdopen(descriptor, encoding="utf-8") as markdown_file:
            descriptor = -1
            return markdown_file.read()
    except (OSError, UnicodeError) as exc:
        raise SeedError(f"cannot read Markdown archive: {path}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _normalized_urls(text: str) -> tuple[str, ...]:
    urls: list[str] = []
    for match in URL_PATTERN.finditer(text):
        url = match.group().rstrip(SENTENCE_SUFFIXES)
        prefix = text[: match.start()]
        for delimiter in EMPHASIS_DELIMITERS:
            if prefix.endswith(delimiter) and url.endswith(delimiter):
                url = url[: -len(delimiter)]
                break
        url = url.rstrip(SENTENCE_SUFFIXES)
        if url:
            urls.append(url)
    return tuple(dict.fromkeys(urls))


def seed_markdown_archive(path: Path, store: StateStore) -> SeedResult:
    files = _archive_files(path)
    discovered = 0
    imported = 0
    for markdown_path in files:
        text = _read_markdown(markdown_path)
        for url in _normalized_urls(text):
            discovered += 1
            imported += int(store.seed_url(url))
    return SeedResult(len(files), discovered, imported)
