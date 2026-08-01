from __future__ import annotations

import os
from pathlib import Path

import pytest

from shelfsignal.profile import load_profile


def test_profile_is_plain_markdown_and_focus_is_optional(tmp_path: Path):
    interests = tmp_path / "interests.md"
    rubric = tmp_path / "rubric.md"
    interests.write_text("# Interests\n\n- Local OCR\n", encoding="utf-8")
    rubric.write_text("# Rubric\n\n- Evidence density\n", encoding="utf-8")

    profile = load_profile(interests, rubric, None)

    assert "Local OCR" in profile.interests
    assert profile.focus == ""


def test_profile_loading_is_read_only(tmp_path: Path):
    interests = tmp_path / "interests.md"
    rubric = tmp_path / "rubric.md"
    interests.write_text("interests", encoding="utf-8")
    rubric.write_text("rubric", encoding="utf-8")
    interests.chmod(0o640)
    rubric.chmod(0o600)
    before = (
        interests.read_bytes(),
        rubric.read_bytes(),
        interests.stat().st_mode,
        rubric.stat().st_mode,
    )

    load_profile(interests, rubric, None)

    after = (
        interests.read_bytes(),
        rubric.read_bytes(),
        interests.stat().st_mode,
        rubric.stat().st_mode,
    )
    assert after == before


def test_profile_rejects_symlink_and_nonregular_files(tmp_path: Path):
    real = tmp_path / "real.md"
    real.write_text("private", encoding="utf-8")
    symlink = tmp_path / "interests.md"
    symlink.symlink_to(real)
    rubric = tmp_path / "rubric.md"
    rubric.write_text("rubric", encoding="utf-8")

    with pytest.raises(ValueError, match="unsafe profile file"):
        load_profile(symlink, rubric, None)
    with pytest.raises(ValueError, match="unsafe profile file"):
        load_profile(tmp_path, rubric, None)


def test_profile_rejects_symlinked_ancestor(tmp_path: Path):
    private = tmp_path / "private"
    private.mkdir()
    interests = private / "interests.md"
    rubric = private / "rubric.md"
    interests.write_text("interests", encoding="utf-8")
    rubric.write_text("rubric", encoding="utf-8")
    alias = tmp_path / "alias"
    alias.symlink_to(private, target_is_directory=True)

    with pytest.raises(ValueError, match="unsafe profile path"):
        load_profile(alias / "interests.md", rubric, None)


def test_profile_rejects_oversized_markdown(tmp_path: Path):
    interests = tmp_path / "interests.md"
    rubric = tmp_path / "rubric.md"
    interests.write_bytes(b"x" * (1024 * 1024 + 1))
    rubric.write_text("rubric", encoding="utf-8")

    with pytest.raises(ValueError, match="too large"):
        load_profile(interests, rubric, None)


def test_profile_rejects_invalid_utf8(tmp_path: Path):
    interests = tmp_path / "interests.md"
    rubric = tmp_path / "rubric.md"
    interests.write_bytes(b"\xff")
    rubric.write_text("rubric", encoding="utf-8")

    with pytest.raises(ValueError, match="UTF-8"):
        load_profile(interests, rubric, None)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO unsupported")
def test_profile_rejects_fifo_without_blocking(tmp_path: Path):
    interests = tmp_path / "interests.md"
    rubric = tmp_path / "rubric.md"
    os.mkfifo(interests)
    rubric.write_text("rubric", encoding="utf-8")

    with pytest.raises(ValueError, match="unsafe profile file"):
        load_profile(interests, rubric, None)
