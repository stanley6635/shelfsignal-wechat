from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from shelfsignal.ocr import (
    ImageEvidence,
    ensure_helper,
    image_evidence,
    image_sha256,
    ocr_article,
    run_vision_ocr,
    should_run_ocr,
    slice_ranges,
)


def test_image_heavy_article_triggers_ocr():
    images = (ImageEvidence(Path("long.jpg"), 1200, 8000),)
    assert should_run_ocr(text_length=120, images=images)


def test_text_article_with_small_icon_does_not_trigger_ocr():
    images = (ImageEvidence(Path("icon.png"), 64, 64),)
    assert not should_run_ocr(text_length=1600, images=images)


def test_long_image_slices_have_bounded_overlap():
    assert slice_ranges(height=5000, chunk=2000, overlap=100) == (
        (0, 2000),
        (1900, 3900),
        (3800, 5000),
    )


@pytest.mark.parametrize(
    ("height", "chunk", "overlap"),
    [
        (0, 2000, 100),
        (100_001, 2000, 100),
        (5000, 0, 0),
        (5000, 100, -1),
        (5000, 100, 100),
    ],
)
def test_slice_ranges_reject_invalid_or_unbounded_inputs(
    height: int, chunk: int, overlap: int
):
    with pytest.raises(ValueError):
        slice_ranges(height=height, chunk=chunk, overlap=overlap)


def test_image_dimensions_use_local_sips(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    image = tmp_path / "image.jpg"
    image.write_bytes(b"fictional")

    class Result:
        stdout = "pixelWidth: 1200\npixelHeight: 8000\n"

    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(command: list[str], **kwargs: object) -> Result:
        calls.append((command, kwargs))
        return Result()

    monkeypatch.setattr("shelfsignal.ocr.subprocess.run", fake_run)
    assert image_evidence(image) == ImageEvidence(image, 1200, 8000)
    assert calls == [
        (
            ["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(image)],
            {"check": True, "capture_output": True, "text": True, "timeout": 30},
        )
    ]


def test_image_dimensions_reject_symlink_and_huge_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    image = tmp_path / "image.jpg"
    image.write_bytes(b"fictional")
    linked = tmp_path / "linked.jpg"
    linked.symlink_to(image)
    with pytest.raises(ValueError, match="unsafe image"):
        image_evidence(linked)

    class Result:
        stdout = "pixelWidth: 1200\npixelHeight: 100001\n"

    monkeypatch.setattr("shelfsignal.ocr.subprocess.run", lambda *args, **kwargs: Result())
    with pytest.raises(ValueError, match="dimensions"):
        image_evidence(image)


def test_image_hash_rejects_symlink(tmp_path: Path):
    image = tmp_path / "image.jpg"
    image.write_bytes(b"fictional-image")
    linked = tmp_path / "linked.jpg"
    linked.symlink_to(image)
    with pytest.raises(ValueError, match="unsafe image"):
        image_sha256(linked)


def test_run_vision_ocr_uses_bounded_subprocess(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    helper = tmp_path / "helper"
    helper.write_bytes(b"binary")
    helper.chmod(0o700)
    image = tmp_path / "image.jpg"
    image.write_bytes(b"fictional")

    class Result:
        stdout = " recognized text \n"

    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(command: list[str], **kwargs: object) -> Result:
        calls.append((command, kwargs))
        return Result()

    monkeypatch.setattr("shelfsignal.ocr.subprocess.run", fake_run)
    assert run_vision_ocr(helper, image) == "recognized text"
    assert calls[0][0] == [str(helper), str(image)]
    assert calls[0][1]["timeout"] == 120
    assert calls[0][1]["check"] is True


def test_ensure_helper_compiles_once_and_rejects_symlink_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    build_dir = tmp_path / "build"
    calls = 0

    def fake_compile(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        output = Path(command[command.index("-o") + 1])
        output.write_bytes(b"fictional-binary")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("shelfsignal.ocr.subprocess.run", fake_compile)
    first = ensure_helper(build_dir)
    second = ensure_helper(build_dir)
    assert first == second
    assert calls == 1
    assert first.stat().st_mode & 0o111

    first.unlink()
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside")
    first.symlink_to(outside)
    with pytest.raises(ValueError, match="unsafe OCR helper"):
        ensure_helper(build_dir)


def test_ocr_cache_prevents_second_runner_call(tmp_path: Path):
    article_dir = tmp_path / "article"
    article_dir.mkdir()
    image = tmp_path / "long.jpg"
    image.write_bytes(b"fictional-image")
    calls = 0

    def runner(path: Path) -> str:
        nonlocal calls
        calls += 1
        return "recognized text"

    evidence = (ImageEvidence(image, 1200, 8000),)
    first = ocr_article(article_dir, evidence, 10, tmp_path / "cache", runner)
    assert first is not None
    first_text = first.read_text(encoding="utf-8")
    second = ocr_article(article_dir, evidence, 10, tmp_path / "cache", runner)
    assert second is not None
    assert second.read_text(encoding="utf-8") == first_text
    assert calls == 1


def test_ocr_records_partial_failure_and_continues(tmp_path: Path):
    article_dir = tmp_path / "article"
    article_dir.mkdir()
    failed = tmp_path / "failed.jpg"
    successful = tmp_path / "successful.jpg"
    failed.write_bytes(b"failed-image")
    successful.write_bytes(b"successful-image")

    def runner(path: Path) -> str:
        if path == failed:
            raise subprocess.TimeoutExpired("fictional-helper", 120)
        return "visible evidence"

    result = ocr_article(
        article_dir,
        (
            ImageEvidence(failed, 1200, 8000),
            ImageEvidence(successful, 1200, 8000),
        ),
        10,
        tmp_path / "cache",
        runner,
    )
    assert result is not None
    rendered = result.read_text(encoding="utf-8")
    assert "OCR incomplete: TimeoutExpired" in rendered
    assert "visible evidence" in rendered


def test_ocr_does_not_swallow_cancellation_or_replace_existing_result(tmp_path: Path):
    article_dir = tmp_path / "article"
    article_dir.mkdir()
    existing = article_dir / "ocr.md"
    existing.write_text("previous result\n", encoding="utf-8")
    image = tmp_path / "long.jpg"
    image.write_bytes(b"fictional-image")

    def cancel(path: Path) -> str:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        ocr_article(
            article_dir,
            (ImageEvidence(image, 1200, 8000),),
            10,
            tmp_path / "cache",
            cancel,
        )
    assert existing.read_text(encoding="utf-8") == "previous result\n"


def test_ocr_rejects_symlink_cache_and_article_directories(tmp_path: Path):
    image = tmp_path / "long.jpg"
    image.write_bytes(b"fictional-image")
    evidence = (ImageEvidence(image, 1200, 8000),)
    outside = tmp_path / "outside"
    outside.mkdir()
    linked_cache = tmp_path / "cache"
    linked_cache.symlink_to(outside, target_is_directory=True)
    article_dir = tmp_path / "article"
    article_dir.mkdir()

    with pytest.raises(ValueError, match="unsafe OCR cache"):
        ocr_article(article_dir, evidence, 10, linked_cache, lambda path: "text")
    assert list(outside.iterdir()) == []

    linked_article = tmp_path / "linked-article"
    linked_article.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="unsafe article OCR"):
        ocr_article(linked_article, evidence, 10, tmp_path / "safe-cache", lambda path: "text")
    assert list(outside.iterdir()) == []


def test_ocr_cache_file_symlink_is_visible_failure_without_outside_read(tmp_path: Path):
    article_dir = tmp_path / "article"
    article_dir.mkdir()
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    image = tmp_path / "long.jpg"
    image.write_bytes(b"fictional-image")
    digest = image_sha256(image)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside sentinel text\n", encoding="utf-8")
    (cache_dir / f"{digest}.txt").symlink_to(outside)

    result = ocr_article(
        article_dir,
        (ImageEvidence(image, 1200, 8000),),
        10,
        cache_dir,
        lambda path: "must not run",
    )
    assert result is not None
    rendered = result.read_text(encoding="utf-8")
    assert "OCR incomplete: ValueError" in rendered
    assert "outside sentinel text" not in rendered


def test_ocr_skips_non_image_heavy_article_without_creating_directories(tmp_path: Path):
    article_dir = tmp_path / "missing-article"
    cache_dir = tmp_path / "missing-cache"
    assert ocr_article(article_dir, (), 1600, cache_dir, lambda path: "unused") is None
    assert not article_dir.exists()
    assert not cache_dir.exists()
