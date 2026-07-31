from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path


class WorkspaceError(ValueError):
    pass


INTERESTS_TEMPLATE = """# Long-term interests

## Positive signals

- Add durable topics that should rank higher.

## Negative signals

- Add recurring low-value patterns.
"""

RUBRIC_TEMPLATE = """# Ranking rubric

- Relevance: relationship to the user's interests.
- Information value: novelty, specificity, and evidence density.
- Confidence: completeness of captured text and image evidence.
"""


@dataclass(frozen=True)
class WorkspacePaths:
    root: Path
    profile_dir: Path
    interests: Path
    rubric: Path
    focus_dir: Path
    browser_dir: Path
    library_dir: Path
    runs_dir: Path
    briefings_dir: Path
    exports_dir: Path
    state_db: Path

    @classmethod
    def from_root(cls, root: Path) -> WorkspacePaths:
        root = root.expanduser().resolve()
        profile = root / "profile"
        return cls(
            root=root,
            profile_dir=profile,
            interests=profile / "interests.md",
            rubric=profile / "rubric.md",
            focus_dir=profile / "focus",
            browser_dir=root / "browser",
            library_dir=root / "library",
            runs_dir=root / "runs",
            briefings_dir=root / "briefings",
            exports_dir=root / "exports",
            state_db=root / "state.db",
        )


def _inside_git_repository(path: Path) -> bool:
    return any((candidate / ".git").exists() for candidate in (path, *path.parents))


def _managed_directories(paths: WorkspacePaths) -> tuple[Path, ...]:
    return (
        paths.root,
        paths.profile_dir,
        paths.focus_dir,
        paths.browser_dir,
        paths.library_dir,
        paths.runs_dir,
        paths.briefings_dir,
        paths.exports_dir,
    )


def _managed_files(paths: WorkspacePaths) -> tuple[Path, ...]:
    return (paths.interests, paths.rubric, paths.state_db)


def _validate_managed_paths(paths: WorkspacePaths) -> None:
    root = paths.root.resolve(strict=False)
    for path in (*_managed_directories(paths), *_managed_files(paths)):
        if path.is_symlink():
            raise WorkspaceError(f"managed workspace path is a symbolic link: {path}")
        try:
            resolved = path.resolve(strict=False)
        except RuntimeError as exc:
            raise WorkspaceError(f"cannot safely resolve managed workspace path: {path}") from exc
        if not resolved.is_relative_to(root):
            raise WorkspaceError(f"managed workspace path escapes the workspace root: {path}")


def _open_private_root(path: Path) -> int:
    try:
        path.mkdir(mode=0o700, parents=True)
    except FileExistsError:
        pass
    except OSError as exc:
        raise WorkspaceError(f"cannot create managed workspace directory: {path}") from exc

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise WorkspaceError(f"managed workspace directory is unsafe: {path}") from exc
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise WorkspaceError(f"managed workspace path is not a directory: {path}")
        os.fchmod(descriptor, 0o700)
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def _open_private_directory_at(parent_fd: int, name: str, display_path: Path) -> int:
    try:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
    except FileExistsError:
        pass
    except OSError as exc:
        raise WorkspaceError(
            f"cannot create managed workspace directory: {display_path}"
        ) from exc

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        raise WorkspaceError(f"managed workspace directory is unsafe: {display_path}") from exc
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise WorkspaceError(f"managed workspace path is not a directory: {display_path}")
        os.fchmod(descriptor, 0o700)
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def _open_private_file_at(
    parent_fd: int, name: str, display_path: Path, content: str
) -> int:
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    create_flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | no_follow
    created = False
    try:
        descriptor = os.open(name, create_flags, 0o600, dir_fd=parent_fd)
        created = True
    except FileExistsError:
        try:
            descriptor = os.open(name, os.O_RDONLY | no_follow, dir_fd=parent_fd)
        except OSError as exc:
            raise WorkspaceError(f"managed workspace file is unsafe: {display_path}") from exc
    except OSError as exc:
        raise WorkspaceError(f"cannot create managed workspace file: {display_path}") from exc

    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise WorkspaceError(f"managed workspace path is not a file: {display_path}")
        os.fchmod(descriptor, 0o600)
        if created:
            with os.fdopen(os.dup(descriptor), "w", encoding="utf-8") as private_file:
                private_file.write(content)
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def _verify_opened_path(
    parent_fd: int,
    name: str,
    descriptor: int,
    display_path: Path,
    expected_kind: int,
) -> None:
    try:
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        opened = os.fstat(descriptor)
    except OSError as exc:
        raise WorkspaceError(f"managed workspace path changed during initialization: {display_path}") from exc
    if (
        stat.S_IFMT(current.st_mode) != expected_kind
        or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
    ):
        raise WorkspaceError(f"managed workspace path changed during initialization: {display_path}")


def _verify_root_identity(path: Path, descriptor: int) -> None:
    try:
        current = os.stat(path, follow_symlinks=False)
        opened = os.fstat(descriptor)
    except OSError as exc:
        raise WorkspaceError("workspace root changed during initialization") from exc
    if (
        not stat.S_ISDIR(current.st_mode)
        or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
    ):
        raise WorkspaceError("workspace root changed during initialization")


def initialize_workspace(root: Path) -> WorkspacePaths:
    if root.expanduser().is_symlink():
        raise WorkspaceError(f"managed workspace path is a symbolic link: {root}")
    paths = WorkspacePaths.from_root(root)
    if _inside_git_repository(paths.root):
        raise WorkspaceError(
            "refusing to initialize a private workspace inside a Git repository"
        )
    _validate_managed_paths(paths)
    descriptors: list[int] = []
    root_fd = _open_private_root(paths.root)
    descriptors.append(root_fd)
    try:
        root_directories = {
            "profile": paths.profile_dir,
            "browser": paths.browser_dir,
            "library": paths.library_dir,
            "runs": paths.runs_dir,
            "briefings": paths.briefings_dir,
            "exports": paths.exports_dir,
        }
        opened_root_directories: dict[str, int] = {}
        for name, display_path in root_directories.items():
            descriptor = _open_private_directory_at(root_fd, name, display_path)
            descriptors.append(descriptor)
            opened_root_directories[name] = descriptor

        profile_fd = opened_root_directories["profile"]
        focus_fd = _open_private_directory_at(profile_fd, "focus", paths.focus_dir)
        descriptors.append(focus_fd)
        interests_fd = _open_private_file_at(
            profile_fd, "interests.md", paths.interests, INTERESTS_TEMPLATE
        )
        descriptors.append(interests_fd)
        rubric_fd = _open_private_file_at(
            profile_fd, "rubric.md", paths.rubric, RUBRIC_TEMPLATE
        )
        descriptors.append(rubric_fd)

        for name, descriptor in opened_root_directories.items():
            _verify_opened_path(
                root_fd,
                name,
                descriptor,
                root_directories[name],
                stat.S_IFDIR,
            )
        _verify_opened_path(
            profile_fd, "focus", focus_fd, paths.focus_dir, stat.S_IFDIR
        )
        _verify_opened_path(
            profile_fd,
            "interests.md",
            interests_fd,
            paths.interests,
            stat.S_IFREG,
        )
        _verify_opened_path(
            profile_fd,
            "rubric.md",
            rubric_fd,
            paths.rubric,
            stat.S_IFREG,
        )
        _verify_root_identity(paths.root, root_fd)
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
    return paths
