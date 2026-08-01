from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

_MAX_PROFILE_BYTES = 1024 * 1024


@dataclass(frozen=True)
class InterestProfile:
    interests: str
    rubric: str
    focus: str


def _normalized_absolute(path: Path) -> Path:
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("unsafe profile path")
    normalized = Path(os.path.normpath(os.fspath(path)))
    if normalized != path or normalized == Path(normalized.anchor):
        raise ValueError("unsafe profile path")
    return normalized


def _read(path: Path | None) -> str:
    if path is None:
        return ""

    path = _normalized_absolute(path)
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        directory_fd = os.open(path.anchor, directory_flags)
    except OSError as exc:
        raise ValueError("unsafe profile path") from exc
    try:
        for part in path.parts[1:-1]:
            try:
                child_fd = os.open(part, directory_flags, dir_fd=directory_fd)
            except OSError as exc:
                raise ValueError("unsafe profile path") from exc
            os.close(directory_fd)
            directory_fd = child_fd

        try:
            descriptor = os.open(
                path.name,
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0),
                dir_fd=directory_fd,
            )
        except OSError as exc:
            raise ValueError("unsafe profile file") from exc
        try:
            details = os.fstat(descriptor)
            if not stat.S_ISREG(details.st_mode):
                raise ValueError("unsafe profile file")
            if details.st_size > _MAX_PROFILE_BYTES:
                raise ValueError("profile file is too large")
            content = bytearray()
            while len(content) <= _MAX_PROFILE_BYTES:
                chunk = os.read(
                    descriptor,
                    min(64 * 1024, _MAX_PROFILE_BYTES + 1 - len(content)),
                )
                if not chunk:
                    break
                content.extend(chunk)
            if len(content) > _MAX_PROFILE_BYTES:
                raise ValueError("profile file is too large")
        finally:
            os.close(descriptor)
    finally:
        os.close(directory_fd)

    try:
        return bytes(content).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("profile file must be UTF-8 Markdown") from exc


def load_profile(
    interests_path: Path,
    rubric_path: Path,
    focus_path: Path | None,
) -> InterestProfile:
    return InterestProfile(
        interests=_read(interests_path),
        rubric=_read(rubric_path),
        focus=_read(focus_path),
    )
