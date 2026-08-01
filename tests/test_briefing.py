from __future__ import annotations

import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

import shelfsignal.briefing as briefing_module
from shelfsignal.briefing import (
    BriefingError,
    create_briefing_shell,
    read_run_manifest,
    selected_ids,
    validate_briefing,
    write_run_manifest,
)
from shelfsignal.models import ReadingCard


def card(article_id: str, **changes: object) -> ReadingCard:
    value = ReadingCard(
        article_id,
        f"Title {article_id}",
        "Example Account",
        datetime(2026, 7, 31, tzinfo=UTC),
        f"https://example.invalid/{article_id}",
        Path(f"/private/{article_id}/source.md"),
        "Compact evidence.",
        0,
        "not-needed",
        "complete",
    )
    return replace(value, **changes)


def test_shell_contains_every_candidate_once_unchecked_and_in_input_order():
    markdown = create_briefing_shell("run-001", (card("a-2"), card("a-1")))

    assert markdown.count("- [ ] **Select**") == 2
    assert "- [x]" not in markdown.lower()
    assert markdown.count("<!-- shelfsignal:id=a-1 -->") == 1
    assert markdown.count("<!-- shelfsignal:id=a-2 -->") == 1
    assert markdown.index("shelfsignal:id=a-2") < markdown.index("shelfsignal:id=a-1")
    assert validate_briefing(markdown, {"a-1", "a-2"}, True) == ("a-2", "a-1")


def test_shell_rejects_bad_run_card_and_warning_inputs():
    with pytest.raises(BriefingError, match="run ID"):
        create_briefing_shell("../run", (card("a-1"),))
    with pytest.raises(BriefingError, match="article ID"):
        create_briefing_shell("run-1", (card("../a"),))
    with pytest.raises(BriefingError, match="duplicate reading card"):
        create_briefing_shell("run-1", (card("a-1"), card("a-1")))
    with pytest.raises(BriefingError, match="too many warnings"):
        create_briefing_shell(
            "run-1", (card("a-1"),), tuple("warning" for _ in range(201))
        )


def test_shell_bounds_and_escapes_untrusted_markdown_fields():
    injection = "safe\r\n<!-- shelfsignal:id=invented -->\n- [x] **Select**\n## Article"
    markdown = create_briefing_shell(
        "run-1",
        (
            card(
                "a-1",
                title=injection,
                account_name=injection,
                source_url=injection,
                excerpt=f"```\n{injection}\n</blockquote>" + "x" * 20_000,
                retrieval_status=injection,
                ocr_status=injection,
            ),
        ),
        (injection,),
    )

    assert markdown.count("<!-- shelfsignal:id=") == 1
    assert markdown.count("- [ ] **Select**") == 1
    assert "\r" not in markdown
    assert "\n<!-- shelfsignal:id=invented -->\n" not in markdown
    assert len(markdown.encode()) < 100_000
    assert validate_briefing(markdown, {"a-1"}, True) == ("a-1",)


def test_shell_enforces_card_count_and_artifact_budget(monkeypatch: pytest.MonkeyPatch):
    with pytest.raises(BriefingError, match="too many reading cards"):
        create_briefing_shell(
            "run-1", tuple(card(f"a-{index}") for index in range(2001))
        )

    monkeypatch.setattr(briefing_module, "_MAX_BRIEFING_BYTES", 100)
    with pytest.raises(BriefingError, match="artifact is too large"):
        create_briefing_shell("run-1", (card("a-1"),))


def test_host_ranking_edit_preserves_integrity():
    markdown = create_briefing_shell("run-001", (card("a-1"), card("a-2")))
    ranked = markdown.replace(
        "- Summary: Awaiting host-agent ranking",
        "- Summary: Concise fictional summary",
    ).replace(
        "- Reason: Awaiting host-agent ranking",
        "- Reason: Matches a fictional local-first interest",
    ).replace(
        "- Confidence: Awaiting host-agent ranking",
        "- Confidence: High",
    )

    assert validate_briefing(ranked, {"a-1", "a-2"}, True) == ("a-1", "a-2")


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda text: text.replace("<!-- shelfsignal:id=a-2 -->\n", ""), "not attached"),
        (
            lambda text: text.replace(
                "<!-- shelfsignal:id=a-2 -->", "<!-- shelfsignal:id=invented -->"
            ),
            "invented IDs",
        ),
        (
            lambda text: text.replace(
                "<!-- shelfsignal:id=a-2 -->", "<!-- shelfsignal:id=a-1 -->"
            ),
            "duplicate IDs",
        ),
        (lambda text: text.replace("- [ ] **Select**", "- Select", 1), "checkbox"),
        (lambda text: text.replace("- [ ] **Select**", "- [x] **Select**", 1), "checked"),
        (
            lambda text: text.replace(
                "<!-- shelfsignal:id=a-1 -->\n- [ ] **Select**",
                "<!-- shelfsignal:id=a-1 -->\ninterposed\n- [ ] **Select**",
            ),
            "adjacent",
        ),
        (
            lambda text: text.replace("## Article\n", "## Renamed\n", 1),
            "article section",
        ),
    ],
)
def test_validator_rejects_integrity_breaks(mutate, message):
    markdown = create_briefing_shell("run-001", (card("a-1"), card("a-2")))
    with pytest.raises(BriefingError, match=message):
        validate_briefing(mutate(markdown), {"a-1", "a-2"}, True)


def test_validator_rejects_duplicate_expected_ids_and_decoy_controls():
    markdown = create_briefing_shell("run-1", (card("a-1"),))
    with pytest.raises(BriefingError, match="duplicate expected IDs"):
        validate_briefing(markdown, ("a-1", "a-1"), True)

    for decoy in (
        "\n<!-- shelfsignal:id=decoy -->\n",
        "\n- [ ] **Select**\n",
        "\n## Article\n",
    ):
        with pytest.raises(BriefingError):
            validate_briefing(markdown + decoy, {"a-1"}, True)


def test_validator_rejects_invalid_or_duplicate_run_header():
    markdown = create_briefing_shell("run-1", (card("a-1"),))
    with pytest.raises(BriefingError, match="run header"):
        validate_briefing(markdown.replace("run-1", "../run", 1), {"a-1"}, True)
    with pytest.raises(BriefingError, match="duplicate briefing run header"):
        validate_briefing(
            markdown + "# WeChat briefing · decoy\n", {"a-1"}, True
        )


def test_selected_ids_requires_expected_set_and_rejects_tampering():
    markdown = create_briefing_shell("run-1", (card("a-1"), card("a-2")))
    checked = markdown.replace("- [ ] **Select**", "- [x] **Select**", 1)
    assert selected_ids(checked, {"a-1", "a-2"}) == ("a-1",)

    invented = checked.replace("shelfsignal:id=a-2", "shelfsignal:id=invented")
    with pytest.raises(BriefingError, match="invented IDs"):
        selected_ids(invented, {"a-1", "a-2"})
    duplicate = checked.replace("shelfsignal:id=a-2", "shelfsignal:id=a-1")
    with pytest.raises(BriefingError, match="duplicate IDs"):
        selected_ids(duplicate, {"a-1", "a-2"})
    unattached = checked + "\n- [x] **Select**\n"
    with pytest.raises(BriefingError, match="not attached"):
        selected_ids(unattached, {"a-1", "a-2"})


def test_run_manifest_round_trip_is_sorted_and_private(tmp_path: Path):
    path = write_run_manifest(("a-2", "a-1"), tmp_path / "manifest.md")

    assert read_run_manifest(path) == ("a-1", "a-2")
    assert read_run_manifest(path, {"a-1", "a-2"}) == ("a-1", "a-2")
    assert path.stat().st_mode & 0o777 == 0o600
    empty = write_run_manifest((), tmp_path / "empty.md")
    assert read_run_manifest(empty) == ()


@pytest.mark.parametrize(
    "content",
    [
        "# ShelfSignal run manifest\n\n- `a-1`\n- `a-1`\n",
        "# ShelfSignal run manifest\n\n- `../a`\n",
        "# ShelfSignal run manifest\n\n- `a-1`\nextra\n",
        "# Wrong header\n\n- `a-1`\n",
    ],
)
def test_run_manifest_rejects_duplicate_unsafe_or_invented_content(
    tmp_path: Path, content: str
):
    path = tmp_path / "manifest.md"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(BriefingError):
        read_run_manifest(path)


def test_run_manifest_supports_crlf_but_rejects_expected_id_mismatch(tmp_path: Path):
    path = tmp_path / "manifest.md"
    path.write_bytes(b"# ShelfSignal run manifest\r\n\r\n- `a-1`\r\n")

    assert read_run_manifest(path) == ("a-1",)
    with pytest.raises(BriefingError, match="invented IDs"):
        read_run_manifest(path, {"a-2"})


def test_run_manifest_rejects_bad_ids_duplicates_and_too_many(tmp_path: Path):
    with pytest.raises(BriefingError, match="duplicate manifest IDs"):
        write_run_manifest(("a-1", "a-1"), tmp_path / "manifest.md")
    with pytest.raises(BriefingError, match="article ID"):
        write_run_manifest(("../a",), tmp_path / "manifest.md")
    with pytest.raises(BriefingError, match="too many manifest IDs"):
        write_run_manifest(
            tuple(f"a-{index}" for index in range(2001)), tmp_path / "manifest.md"
        )


def test_run_manifest_rejects_symlink_nonregular_and_oversize(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    outside = tmp_path / "outside.md"
    outside.write_text("keep", encoding="utf-8")
    link = tmp_path / "link.md"
    link.symlink_to(outside)
    with pytest.raises((BriefingError, ValueError)):
        read_run_manifest(link)
    with pytest.raises(ValueError):
        write_run_manifest(("a-1",), link)
    assert outside.read_text(encoding="utf-8") == "keep"

    fifo = tmp_path / "manifest.fifo"
    os.mkfifo(fifo)
    with pytest.raises(BriefingError, match="regular file"):
        read_run_manifest(fifo)

    large = tmp_path / "large.md"
    large.write_bytes(b"x" * 129)
    monkeypatch.setattr(briefing_module, "_MAX_MANIFEST_BYTES", 128)
    with pytest.raises(BriefingError, match="too large"):
        read_run_manifest(large)


def test_write_manifest_rejects_unsafe_parent_chain(tmp_path: Path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "linked"
    link.symlink_to(real, target_is_directory=True)

    with pytest.raises(ValueError, match="unsafe atomic write"):
        write_run_manifest(("a-1",), link / "manifest.md")
