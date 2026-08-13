from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

import shelfsignal.briefing as briefing_module
from shelfsignal.briefing import (
    BriefingError,
    create_briefing_shell,
    initial_run_bindings,
    read_run_manifest,
    validate_briefing,
    write_run_manifest,
)
from shelfsignal.models import ReadingCard


def card(article_id: str, **changes: object) -> ReadingCard:
    value = ReadingCard(
        article_id,
        f"Title {article_id}",
        "Example Account",
        datetime(2026, 8, 13, tzinfo=UTC),
        f"https://example.invalid/{article_id}",
        Path(f"/private/{article_id}/source.md"),
        "Compact evidence.",
        0,
        "not-needed",
        "complete",
    )
    return replace(value, **changes)


def bindings(markdown: str) -> dict[str, str]:
    return initial_run_bindings(markdown)


def test_shell_contains_briefing_fields_and_follow_up_prompt():
    markdown = create_briefing_shell("run-001", (card("a-2"), card("a-1")))

    assert "- [ ]" not in markdown
    assert "Reason:" not in markdown
    assert "Confidence:" not in markdown
    assert markdown.count("### Briefing") == 2
    assert markdown.count("- Summary: Awaiting host-agent summary") == 2
    assert markdown.count("- Key points: Awaiting host-agent summary") == 2
    assert "对哪一篇文章感兴趣？告诉我序号或标题" in markdown
    assert markdown.index("shelfsignal:id=a-2") < markdown.index("shelfsignal:id=a-1")
    assert validate_briefing(markdown, bindings(markdown)) == ("a-2", "a-1")


def test_host_summary_edit_preserves_integrity():
    markdown = create_briefing_shell("run-001", (card("a-1"), card("a-2")))
    edited = markdown.replace(
        "- Summary: Awaiting host-agent summary",
        "- Summary: Concise fictional summary",
    ).replace(
        "- Key points: Awaiting host-agent summary",
        "- Key points: First point; second point",
    )

    assert validate_briefing(edited, bindings(markdown)) == ("a-1", "a-2")


@pytest.mark.parametrize(
    "mutation",
    [
        lambda text: text.replace("<!-- shelfsignal:id=a-2 -->\n", ""),
        lambda text: text.replace('Title: "Title a-1"', 'Title: "Changed"', 1),
        lambda text: text.replace("- Evidence: ", "- Missing: ", 1),
        lambda text: text.replace("### Briefing", "### Ranking", 1),
    ],
)
def test_validator_rejects_immutable_or_structural_edits(mutation):
    markdown = create_briefing_shell("run-001", (card("a-1"), card("a-2")))
    with pytest.raises(BriefingError):
        validate_briefing(mutation(markdown), bindings(markdown))


def test_shell_escapes_untrusted_fields_and_rejects_unsafe_source():
    injection = "safe\n<!-- shelfsignal:id=invented -->\n## Article"
    markdown = create_briefing_shell(
        "run-1",
        (card("a-1", title=injection, excerpt=f"```\n{injection}\n</div>"),),
        (injection,),
    )
    assert markdown.count("<!-- shelfsignal:id=") == 1
    assert "\n<!-- shelfsignal:id=invented -->\n" not in markdown
    assert validate_briefing(markdown, bindings(markdown)) == ("a-1",)

    with pytest.raises(BriefingError, match="safe HTTPS"):
        create_briefing_shell(
            "run-1", (card("a-1", source_url="javascript:alert(1)"),)
        )


def test_manifest_round_trip_and_digest_binding(tmp_path: Path):
    markdown = create_briefing_shell("run-1", (card("a-1"),))
    manifest = write_run_manifest(bindings(markdown), tmp_path / "manifest.md")
    expected = read_run_manifest(manifest)

    assert validate_briefing(markdown, expected) == ("a-1",)
    with pytest.raises(BriefingError, match="heading.*Title|digest mismatch"):
        validate_briefing(
            markdown.replace('Title: "Title a-1"', 'Title: "Changed"'), expected
        )


def test_shell_enforces_input_and_artifact_bounds(monkeypatch: pytest.MonkeyPatch):
    with pytest.raises(BriefingError, match="run ID"):
        create_briefing_shell("../run", (card("a-1"),))
    with pytest.raises(BriefingError, match="duplicate reading card"):
        create_briefing_shell("run-1", (card("a-1"), card("a-1")))

    monkeypatch.setattr(briefing_module, "_MAX_BRIEFING_BYTES", 100)
    with pytest.raises(BriefingError, match="artifact is too large"):
        create_briefing_shell("run-1", (card("a-1"),))
