from pathlib import Path

import pytest

from shelfsignal.workspace import WorkspaceError, WorkspacePaths, initialize_workspace


def test_initialize_workspace_creates_private_layout(tmp_path: Path):
    root = tmp_path / "ShelfSignal-Data"
    paths = initialize_workspace(root)
    assert paths == WorkspacePaths.from_root(root)
    assert paths.interests.read_text(encoding="utf-8").startswith(
        "# Long-term interests"
    )
    assert paths.rubric.exists()
    assert paths.focus_dir.is_dir()
    assert paths.library_dir.is_dir()
    assert paths.briefings_dir.is_dir()
    assert paths.exports_dir.is_dir()


def test_initialize_workspace_rejects_git_repository(tmp_path: Path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    with pytest.raises(WorkspaceError, match="inside a Git repository"):
        initialize_workspace(root)
