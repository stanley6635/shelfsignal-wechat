from __future__ import annotations

import os
import stat
import subprocess
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

FORBIDDEN_BYTE_MARKERS = (
    b"/" + b"Users" + b"/",
    b"Coo" + b"kie:",
    b"Authori" + b"zation: Bearer",
    b"T" + b"ARS",
    b"Stan" + b"ley Sun",
)
EXCLUDED_FALLBACK_PARTS = {
    ".git",
    ".venv",
    "dist",
    "build",
    ".build",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
}
RUNTIME_DIRECTORY_NAMES = {
    ".shelfsignal",
    "browser",
    "briefings",
    "exports",
    "library",
    "profile",
    "runs",
    "shelfsignal-data",
    "user-data",
}
RUNTIME_FILE_NAMES = {
    ".env",
    "auth.json",
    "cookies",
    "credentials.json",
    "local state",
    "state.db",
    "storage-state.json",
    "storage_state.json",
    "token.json",
}
DATABASE_SUFFIXES = {".db", ".sqlite", ".sqlite3"}
MAX_MEMBER_BYTES = 128 * 1024 * 1024
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024


def _forbidden_markers(value: bytes) -> tuple[bytes, ...]:
    return tuple(marker for marker in FORBIDDEN_BYTE_MARKERS if marker in value)


def _unsafe_runtime_path(name: str) -> bool:
    if "\\" in name:
        return True
    normalized = name.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts or normalized != path.as_posix():
        return True
    lowered = tuple(part.casefold() for part in path.parts)
    if any(part in RUNTIME_DIRECTORY_NAMES for part in lowered):
        return True
    if lowered and lowered[-1] in RUNTIME_FILE_NAMES:
        return True
    return bool(path.suffix.casefold() in DATABASE_SUFFIXES)


def _fallback_files(root: Path) -> tuple[Path, ...]:
    files = []
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if any(
            part in EXCLUDED_FALLBACK_PARTS or part.endswith(".egg-info")
            for part in relative.parts
        ):
            continue
        if path.is_file() or path.is_symlink():
            files.append(relative)
    return tuple(sorted(files, key=lambda item: item.as_posix()))


def repository_files(root: Path) -> tuple[Path, ...]:
    if (root / ".git").exists():
        completed = subprocess.run(
            ("git", "ls-files", "-z"),
            cwd=root,
            check=True,
            capture_output=True,
        )
        names = tuple(item for item in completed.stdout.split(b"\0") if item)
        return tuple(Path(os.fsdecode(item)) for item in names)
    return _fallback_files(root)


def audit_repository(root: Path) -> tuple[str, ...]:
    violations: list[str] = []
    for relative in repository_files(root):
        encoded_name = os.fsencode(relative.as_posix())
        for marker in _forbidden_markers(encoded_name):
            violations.append(f"forbidden marker {marker!r} in tracked filename {relative}")
        if _unsafe_runtime_path(relative.as_posix()):
            violations.append(f"tracked runtime or credential artifact: {relative}")

        path = root / relative
        try:
            details = path.lstat()
        except OSError as exc:
            violations.append(f"unreadable tracked path {relative}: {type(exc).__name__}")
            continue
        if not stat.S_ISREG(details.st_mode):
            violations.append(f"tracked path is not a regular file: {relative}")
            continue
        try:
            content = path.read_bytes()
        except OSError as exc:
            violations.append(f"unreadable tracked file {relative}: {type(exc).__name__}")
            continue
        for marker in _forbidden_markers(content):
            violations.append(f"forbidden marker {marker!r} in tracked file {relative}")
    return tuple(violations)


def _audit_member(
    name: str,
    content: bytes | None,
    violations: list[str],
) -> None:
    encoded_name = name.encode("utf-8", errors="surrogateescape")
    for marker in _forbidden_markers(encoded_name):
        violations.append(f"forbidden marker {marker!r} in archive member name {name}")
    if _unsafe_runtime_path(name):
        violations.append(f"runtime or credential artifact in archive: {name}")
    if content is not None:
        for marker in _forbidden_markers(content):
            violations.append(f"forbidden marker {marker!r} in archive member {name}")


def _audit_zip(path: Path, violations: list[str]) -> set[str]:
    names: set[str] = set()
    total = 0
    with zipfile.ZipFile(path) as archive:
        for item in archive.infolist():
            names.add(item.filename)
            mode = item.external_attr >> 16
            if mode and stat.S_ISLNK(mode):
                violations.append(f"symbolic link in archive: {item.filename}")
                continue
            if item.is_dir():
                _audit_member(item.filename, None, violations)
                continue
            if item.file_size > MAX_MEMBER_BYTES:
                violations.append(f"oversized archive member: {item.filename}")
                continue
            total += item.file_size
            if total > MAX_ARCHIVE_BYTES:
                violations.append("archive uncompressed size exceeds release limit")
                break
            _audit_member(item.filename, archive.read(item), violations)
    return names


def _audit_tar(path: Path, violations: list[str]) -> set[str]:
    names: set[str] = set()
    total = 0
    with tarfile.open(path, "r:gz") as archive:
        for item in archive.getmembers():
            names.add(item.name)
            if item.isdir():
                _audit_member(item.name, None, violations)
                continue
            if not item.isfile():
                violations.append(f"non-regular archive member: {item.name}")
                continue
            if item.size > MAX_MEMBER_BYTES:
                violations.append(f"oversized archive member: {item.name}")
                continue
            total += item.size
            if total > MAX_ARCHIVE_BYTES:
                violations.append("archive uncompressed size exceeds release limit")
                break
            extracted = archive.extractfile(item)
            if extracted is None:
                violations.append(f"unreadable archive member: {item.name}")
                continue
            _audit_member(item.name, extracted.read(), violations)
    return names


def audit_distribution(path: Path) -> tuple[str, ...]:
    violations: list[str] = []
    if path.suffix == ".whl":
        names = _audit_zip(path, violations)
        required_suffixes = {
            "shelfsignal/__init__.py",
            "shelfsignal/resources/vision_ocr.swift",
            ".dist-info/licenses/LICENSE",
        }
    elif path.name.endswith(".tar.gz"):
        names = _audit_tar(path, violations)
        required_suffixes = {
            "/LICENSE",
            "/README.md",
            "/pyproject.toml",
            "/src/shelfsignal/__init__.py",
            "/src/shelfsignal/resources/vision_ocr.swift",
            "/tests/release_audit.py",
        }
    else:
        return (f"unsupported distribution archive: {path.name}",)

    for required in required_suffixes:
        if not any(name == required or name.endswith(required) for name in names):
            violations.append(f"required release member is missing: {required}")
    return tuple(violations)
