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


def _ensure_private_directory(path: Path, *, parents: bool = False) -> None:
    try:
        path.mkdir(mode=0o700, parents=parents)
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
    finally:
        os.close(descriptor)


def _ensure_private_file(path: Path, content: str) -> None:
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    create_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | no_follow
    try:
        descriptor = os.open(path, create_flags, 0o600)
    except FileExistsError:
        try:
            descriptor = os.open(path, os.O_RDONLY | no_follow)
        except OSError as exc:
            raise WorkspaceError(f"managed workspace file is unsafe: {path}") from exc
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise WorkspaceError(f"managed workspace path is not a file: {path}")
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)
        return
    except OSError as exc:
        raise WorkspaceError(f"cannot create managed workspace file: {path}") from exc

    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as private_file:
            descriptor = -1
            private_file.write(content)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def initialize_workspace(root: Path) -> WorkspacePaths:
    if root.expanduser().is_symlink():
        raise WorkspaceError(f"managed workspace path is a symbolic link: {root}")
    paths = WorkspacePaths.from_root(root)
    if _inside_git_repository(paths.root):
        raise WorkspaceError(
            "refusing to initialize a private workspace inside a Git repository"
        )
    _validate_managed_paths(paths)
    for index, directory in enumerate(_managed_directories(paths)):
        _ensure_private_directory(directory, parents=index == 0)
    _ensure_private_file(paths.interests, INTERESTS_TEMPLATE)
    _ensure_private_file(paths.rubric, RUBRIC_TEMPLATE)
    return paths
