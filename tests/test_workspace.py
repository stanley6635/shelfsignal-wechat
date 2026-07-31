import stat
from pathlib import Path

import pytest

import shelfsignal.workspace as workspace_module
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


def test_initialize_workspace_rejects_directory_symlink_escape(tmp_path: Path):
    root = tmp_path / "ShelfSignal-Data"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "profile").symlink_to(outside, target_is_directory=True)

    with pytest.raises(WorkspaceError, match="symbolic link"):
        initialize_workspace(root)

    assert list(outside.iterdir()) == []


def test_initialize_workspace_does_not_follow_profile_swapped_after_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = tmp_path / "ShelfSignal-Data"
    profile = root / "profile"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    original_open = workspace_module.os.open
    swapped = False

    def open_and_swap_profile(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
        is_profile = path == profile or (path == "profile" and dir_fd is not None)
        if is_profile and not swapped:
            swapped = True
            profile.rmdir()
            profile.symlink_to(outside, target_is_directory=True)
        return descriptor

    monkeypatch.setattr(workspace_module.os, "open", open_and_swap_profile)

    with pytest.raises(WorkspaceError):
        initialize_workspace(root)

    assert swapped
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("filename", ["interests.md", "rubric.md"])
def test_initialize_workspace_rejects_dangling_file_symlink(
    tmp_path: Path, filename: str
):
    root = tmp_path / "ShelfSignal-Data"
    profile = root / "profile"
    outside_file = tmp_path / "outside" / filename
    profile.mkdir(parents=True)
    (profile / filename).symlink_to(outside_file)

    with pytest.raises(WorkspaceError, match="symbolic link"):
        initialize_workspace(root)

    assert not outside_file.exists()


def test_initialize_workspace_sets_private_modes(tmp_path: Path):
    paths = initialize_workspace(tmp_path / "ShelfSignal-Data")

    directories = (
        paths.root,
        paths.profile_dir,
        paths.focus_dir,
        paths.browser_dir,
        paths.library_dir,
        paths.runs_dir,
        paths.briefings_dir,
        paths.exports_dir,
    )
    for directory in directories:
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    for private_file in (paths.interests, paths.rubric):
        assert stat.S_IMODE(private_file.stat().st_mode) == 0o600


def test_initialize_workspace_idempotently_tightens_modes_without_changing_content(
    tmp_path: Path,
):
    root = tmp_path / "ShelfSignal-Data"
    paths = initialize_workspace(root)
    interests_content = "# Custom interests\n\nKeep this private.\n"
    rubric_content = "# Custom rubric\n\nKeep this too.\n"
    paths.interests.write_text(interests_content, encoding="utf-8")
    paths.rubric.write_text(rubric_content, encoding="utf-8")
    directories = (
        paths.root,
        paths.profile_dir,
        paths.focus_dir,
        paths.browser_dir,
        paths.library_dir,
        paths.runs_dir,
        paths.briefings_dir,
        paths.exports_dir,
    )
    for directory in directories:
        directory.chmod(0o755)
    paths.interests.chmod(0o644)
    paths.rubric.chmod(0o644)

    rerun_paths = initialize_workspace(root)

    assert rerun_paths == paths
    assert paths.interests.read_text(encoding="utf-8") == interests_content
    assert paths.rubric.read_text(encoding="utf-8") == rubric_content
    for directory in directories:
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(paths.interests.stat().st_mode) == 0o600
    assert stat.S_IMODE(paths.rubric.stat().st_mode) == 0o600
