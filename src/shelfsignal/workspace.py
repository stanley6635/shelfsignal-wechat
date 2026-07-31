from __future__ import annotations

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


def initialize_workspace(root: Path) -> WorkspacePaths:
    paths = WorkspacePaths.from_root(root)
    if _inside_git_repository(paths.root):
        raise WorkspaceError(
            "refusing to initialize a private workspace inside a Git repository"
        )
    for directory in (
        paths.profile_dir,
        paths.focus_dir,
        paths.browser_dir,
        paths.library_dir,
        paths.runs_dir,
        paths.briefings_dir,
        paths.exports_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    if not paths.interests.exists():
        paths.interests.write_text(INTERESTS_TEMPLATE, encoding="utf-8")
    if not paths.rubric.exists():
        paths.rubric.write_text(RUBRIC_TEMPLATE, encoding="utf-8")
    return paths
