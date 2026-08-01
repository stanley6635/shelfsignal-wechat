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
    initial_run_bindings,
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


def bindings(markdown: str) -> dict[str, str]:
    return initial_run_bindings(markdown)


def test_shell_contains_every_candidate_once_unchecked_and_in_input_order():
    markdown = create_briefing_shell("run-001", (card("a-2"), card("a-1")))

    assert markdown.count("- [ ] **Select**") == 2
    assert "- [x]" not in markdown.lower()
    assert markdown.count("<!-- shelfsignal:id=a-1 -->") == 1
    assert markdown.count("<!-- shelfsignal:id=a-2 -->") == 1
    assert markdown.count("<!-- shelfsignal:digest=") == 2
    assert markdown.count("## Article · `a-1`") == 1
    assert markdown.count("## Article · `a-2`") == 1
    assert markdown.index("shelfsignal:id=a-2") < markdown.index("shelfsignal:id=a-1")
    assert validate_briefing(markdown, bindings(markdown), True) == ("a-2", "a-1")


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
                source_url="https://example.invalid/a-1",
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
    assert validate_briefing(markdown, bindings(markdown), True) == ("a-1",)


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

    assert validate_briefing(ranked, bindings(markdown), True) == ("a-1", "a-2")


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda text: text.replace("<!-- shelfsignal:id=a-2 -->\n", ""), "hidden ID"),
        (
            lambda text: text.replace("## Article · `a-2`", "## Article · `invented`").replace(
                "<!-- shelfsignal:id=a-2 -->", "<!-- shelfsignal:id=invented -->"
            ),
            "digest mismatch",
        ),
        (
            lambda text: text.replace("## Article · `a-2`", "## Article · `a-1`").replace(
                "<!-- shelfsignal:id=a-2 -->", "<!-- shelfsignal:id=a-1 -->"
            ),
            "digest mismatch",
        ),
        (lambda text: text.replace("- [ ] **Select**", "- Select", 1), "checkbox"),
        (lambda text: text.replace("- [ ] **Select**", "- [x] **Select**", 1), "checked"),
        (
            lambda text: text.replace("- [ ] **Select**", "interposed\n- [ ] **Select**", 1),
            "adjacent",
        ),
        (
            lambda text: text.replace("## Article · `a-1`\n", "## Renamed\n", 1),
            "not attached",
        ),
    ],
)
def test_validator_rejects_integrity_breaks(mutate, message):
    markdown = create_briefing_shell("run-001", (card("a-1"), card("a-2")))
    with pytest.raises(BriefingError, match=message):
        validate_briefing(mutate(markdown), bindings(markdown), True)


@pytest.mark.parametrize(
    "field",
    ["Title", "Account", "Published", "Source", "Retrieval", "OCR"],
)
def test_validator_rejects_deleted_or_duplicate_visible_field(field: str):
    markdown = create_briefing_shell("run-1", (card("a-1"),))
    expected = bindings(markdown)
    expected = bindings(markdown)
    line = next(item for item in markdown.splitlines() if item.startswith(f"- {field}: "))

    with pytest.raises(BriefingError, match=f"{field} field"):
        validate_briefing(markdown.replace(f"{line}\n", "", 1), expected, True)
    with pytest.raises(BriefingError, match="decoy or duplicate article field"):
        validate_briefing(markdown + f"{line}\n", expected, True)


def test_validator_rejects_deleted_or_duplicate_evidence():
    markdown = create_briefing_shell("run-1", (card("a-1"),))
    expected = bindings(markdown)
    evidence = next(item for item in markdown.splitlines() if item.startswith("- Evidence: "))
    with pytest.raises(BriefingError, match="Evidence field"):
        validate_briefing(markdown.replace(f"{evidence}\n", "", 1), expected, True)
    with pytest.raises(BriefingError, match="decoy or duplicate article field"):
        validate_briefing(markdown + f"{evidence}\n", expected, True)


def test_validator_rejects_header_hidden_id_mismatch():
    markdown = create_briefing_shell("run-1", (card("a-1"), card("a-2")))
    expected = bindings(markdown)
    mismatched = markdown.replace("## Article · `a-2`", "## Article · `a-1`", 1)
    with pytest.raises(BriefingError, match="header and hidden ID differ"):
        validate_briefing(mismatched, expected, True)


@pytest.mark.parametrize(
    "replacement",
    [
        "- Source: \"http://example.invalid/a-1\"",
        "- Source: \"https://user@example.invalid/a-1\"",
        "- Source: not-json",
        "- Source: \"https://example.invalid/a-1\" trailing",
    ],
)
def test_validator_rejects_missing_or_malformed_source(replacement: str):
    markdown = create_briefing_shell("run-1", (card("a-1"),))
    expected = bindings(markdown)
    original = next(
        item for item in markdown.splitlines() if item.startswith("- Source: ")
    )
    with pytest.raises(BriefingError, match="Source field"):
        validate_briefing(markdown.replace(original, replacement), expected, True)
    with pytest.raises(BriefingError, match="Source field"):
        validate_briefing(markdown.replace(f"{original}\n", "", 1), expected, True)

    with pytest.raises(BriefingError, match="decoy or duplicate article field"):
        validate_briefing(markdown + "- Source : https://example.invalid/decoy\n", expected, True)


@pytest.mark.parametrize(
    ("opening", "closing"),
    [
        ("```", "```"),
        ("`````python", "`````"),
        ("~~~ markdown", "~~~"),
        ("<!-- outer comment", "-->"),
    ],
)
def test_validator_rejects_article_controls_wrapped_in_fence_or_comment(
    opening: str, closing: str
):
    markdown = create_briefing_shell("run-1", (card("a-1"),))
    expected = bindings(markdown)
    lines = markdown.splitlines()
    start = lines.index("## Article · `a-1`")
    wrapped = lines[:start] + [opening] + lines[start:] + [closing]
    tampered = "\n".join(wrapped) + "\n"

    with pytest.raises(BriefingError, match="fenced code|HTML comment|raw HTML"):
        validate_briefing(tampered, expected, True)
    checked = tampered.replace("- [ ] **Select**", "- [x] **Select**", 1)
    with pytest.raises(BriefingError, match="fenced code|HTML comment|raw HTML"):
        selected_ids(checked, expected)


def test_validator_rejects_checked_decoy_in_fence_or_multiline_comment():
    markdown = create_briefing_shell("run-1", (card("a-1"),))
    expected = bindings(markdown)
    for decoy in (
        "\n```\n## Article · `decoy`\n<!-- shelfsignal:id=decoy -->\n- [x] **Select**\n```\n",
        "\n<!-- outer\n## Article · `decoy`\n<!-- shelfsignal:id=decoy -->\n- [x] **Select**\n-->\n",
    ):
        with pytest.raises(BriefingError, match="fenced code|HTML comment|raw HTML"):
            selected_ids(markdown + decoy, expected)


@pytest.mark.parametrize(
    "raw_html",
    [
        "<div hidden>",
        "</div>",
        "<div hidden>same line</div>",
        "<script>alert(1)</script>",
        "<style>body{display:none}</style>",
        "<details open>",
        "<table><tr><td>hidden</td></tr></table>",
    ],
)
def test_validator_rejects_raw_html_anywhere(raw_html: str):
    markdown = create_briefing_shell("run-1", (card("a-1"),))
    expected = bindings(markdown)
    tampered = markdown.replace("## Article", f"{raw_html}\n## Article", 1)

    with pytest.raises(BriefingError, match="raw HTML"):
        validate_briefing(tampered, expected, True)
    with pytest.raises(BriefingError, match="raw HTML"):
        selected_ids(tampered.replace("- [ ] **Select**", "- [x] **Select**"), expected)


@pytest.mark.parametrize("raw", ["<div hidden", "<![CDATA[hidden]]>", "<!ENTITY x>", "stray >"])
def test_validator_rejects_incomplete_html_and_stray_delimiters(raw: str):
    markdown = create_briefing_shell("run-1", (card("a-1"),))
    expected = bindings(markdown)

    with pytest.raises(BriefingError, match="raw HTML delimiter"):
        validate_briefing(markdown + raw + "\n", expected, True)


@pytest.mark.parametrize(
    ("original", "noncanonical"),
    [
        ('- Title: "Title a-1"', '- Title: "Title \\u0061-1"'),
        (
            '- Source: "https://example.invalid/a-1"',
            '- Source: "https:\\/\\/example.invalid\\/a-1"',
        ),
        ('- Evidence: "Compact evidence."', '- Evidence: "\\u0043ompact evidence."'),
    ],
)
def test_validator_rejects_noncanonical_json_encodings(
    original: str, noncanonical: str
):
    markdown = create_briefing_shell("run-1", (card("a-1"),))
    expected = bindings(markdown)

    with pytest.raises(BriefingError, match="non-canonical"):
        validate_briefing(markdown.replace(original, noncanonical, 1), expected, True)


def test_normal_unicode_renderer_output_is_canonical():
    markdown = create_briefing_shell(
        "run-1",
        (card("a-1", title="中文标题", account_name="示例账号", excerpt="中文证据。"),),
    )

    assert validate_briefing(markdown, bindings(markdown), True) == ("a-1",)


def test_manifest_digest_binds_visible_payload_and_id_markers():
    markdown = create_briefing_shell("run-1", (card("a-1"), card("a-2")))
    expected = bindings(markdown)

    altered = markdown.replace('"Title a-1"', '"Altered title"', 1)
    with pytest.raises(BriefingError, match="digest mismatch"):
        validate_briefing(altered, expected, True)

    swapped_ids = (
        markdown.replace("`a-1`", "`temporary`", 1)
        .replace("`a-2`", "`a-1`", 1)
        .replace("`temporary`", "`a-2`", 1)
        .replace("shelfsignal:id=a-1", "shelfsignal:id=temporary", 1)
        .replace("shelfsignal:id=a-2", "shelfsignal:id=a-1", 1)
        .replace("shelfsignal:id=temporary", "shelfsignal:id=a-2", 1)
    )
    with pytest.raises(BriefingError, match="digest mismatch"):
        validate_briefing(swapped_ids, expected, True)

    digest_lines = [
        line for line in markdown.splitlines() if line.startswith("<!-- shelfsignal:digest=")
    ]
    swapped_digests = (
        markdown.replace(digest_lines[0], "DIGEST-TEMP", 1)
        .replace(digest_lines[1], digest_lines[0], 1)
        .replace("DIGEST-TEMP", digest_lines[1], 1)
    )
    with pytest.raises(BriefingError, match="digest mismatch"):
        validate_briefing(swapped_digests, expected, True)


def test_block_reordering_checkbox_and_ranking_edits_preserve_bindings():
    markdown = create_briefing_shell("run-1", (card("a-1"), card("a-2")))
    expected = bindings(markdown)
    first = markdown.index("## Article · `a-1`")
    second = markdown.index("## Article · `a-2`")
    reordered = markdown[:first] + markdown[second:] + markdown[first:second]
    edited = (
        reordered.replace("- [ ] **Select**", "- [x] **Select**", 1)
        .replace("- Summary: Awaiting host-agent ranking", "- Summary: Ranked", 1)
        .replace("- Reason: Awaiting host-agent ranking", "- Reason: Relevant", 1)
        .replace("- Confidence: Awaiting host-agent ranking", "- Confidence: High", 1)
    )

    assert validate_briefing(edited, expected, False) == ("a-2", "a-1")
    assert selected_ids(edited, expected) == ("a-2",)


def test_renderer_rejects_unsafe_source_url():
    with pytest.raises(BriefingError, match="safe HTTPS"):
        create_briefing_shell(
            "run-1", (card("a-1", source_url="javascript:alert(1)"),)
        )
    with pytest.raises(BriefingError, match="too long"):
        create_briefing_shell(
            "run-1",
            (card("a-1", source_url="https://example.invalid/" + "x" * 5_000),),
        )
    for source in ("https://localhost/a-1", "https://127.0.0.1/a-1"):
        with pytest.raises(BriefingError, match="safe HTTPS"):
            create_briefing_shell("run-1", (card("a-1", source_url=source),))


def test_selected_ids_contract_requires_manifest_expected_ids(tmp_path: Path):
    markdown = create_briefing_shell("run-1", (card("a-1"), card("a-2")))
    checked = markdown.replace("- [ ] **Select**", "- [x] **Select**", 1)
    manifest = write_run_manifest(bindings(markdown), tmp_path / "manifest.md")
    expected = read_run_manifest(manifest)

    assert selected_ids(checked, expected) == ("a-1",)


def test_validator_requires_manifest_bindings_and_rejects_decoy_controls():
    markdown = create_briefing_shell("run-1", (card("a-1"),))
    expected = bindings(markdown)
    with pytest.raises(BriefingError, match="ID-to-digest mapping"):
        validate_briefing(markdown, ("a-1",), True)  # type: ignore[arg-type]

    for decoy in (
        "\n<!-- shelfsignal:id=decoy -->\n",
        "\n- [ ] **Select**\n",
        "\n## Article\n",
    ):
        with pytest.raises(BriefingError):
            validate_briefing(markdown + decoy, expected, True)


def test_validator_rejects_invalid_or_duplicate_run_header():
    markdown = create_briefing_shell("run-1", (card("a-1"),))
    expected = bindings(markdown)
    with pytest.raises(BriefingError, match="run header"):
        validate_briefing(markdown.replace("run-1", "../run", 1), expected, True)
    with pytest.raises(BriefingError, match="duplicate briefing run header"):
        validate_briefing(
            markdown + "# WeChat briefing · decoy\n", expected, True
        )


def test_selected_ids_requires_manifest_bindings_and_rejects_tampering():
    markdown = create_briefing_shell("run-1", (card("a-1"), card("a-2")))
    expected = bindings(markdown)
    checked = markdown.replace("- [ ] **Select**", "- [x] **Select**", 1)
    assert selected_ids(checked, expected) == ("a-1",)

    invented = checked.replace("`a-2`", "`invented`", 1).replace(
        "shelfsignal:id=a-2", "shelfsignal:id=invented"
    )
    with pytest.raises(BriefingError, match="digest mismatch"):
        selected_ids(invented, expected)
    duplicate = checked.replace("`a-2`", "`a-1`", 1).replace(
        "shelfsignal:id=a-2", "shelfsignal:id=a-1"
    )
    with pytest.raises(BriefingError, match="digest mismatch"):
        selected_ids(duplicate, expected)
    unattached = checked + "\n- [x] **Select**\n"
    with pytest.raises(BriefingError, match="not attached"):
        selected_ids(unattached, expected)


def test_run_manifest_round_trip_is_sorted_and_private(tmp_path: Path):
    source = create_briefing_shell("run-1", (card("a-2"), card("a-1")))
    expected = bindings(source)
    path = write_run_manifest(expected, tmp_path / "manifest.md")

    assert read_run_manifest(path) == {
        "a-1": expected["a-1"],
        "a-2": expected["a-2"],
    }
    assert path.stat().st_mode & 0o777 == 0o600
    empty = write_run_manifest({}, tmp_path / "empty.md")
    assert read_run_manifest(empty) == {}


@pytest.mark.parametrize(
    "content",
    [
        "# ShelfSignal run manifest\n\n- `a-1` sha256:" + "0" * 64 + "\n- `a-1` sha256:" + "1" * 64 + "\n",
        "# ShelfSignal run manifest\n\n- `../a` sha256:" + "0" * 64 + "\n",
        "# ShelfSignal run manifest\n\n- `a-1` sha256:bad\n",
        "# ShelfSignal run manifest\n\n- `a-1` sha256:" + "0" * 64 + "\nextra\n",
        "# Wrong header\n\n- `a-1` sha256:" + "0" * 64 + "\n",
    ],
)
def test_run_manifest_rejects_duplicate_unsafe_or_invented_content(
    tmp_path: Path, content: str
):
    path = tmp_path / "manifest.md"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(BriefingError):
        read_run_manifest(path)


def test_run_manifest_supports_crlf(tmp_path: Path):
    path = tmp_path / "manifest.md"
    digest = "0" * 64
    path.write_bytes(
        f"# ShelfSignal run manifest\r\n\r\n- `a-1` sha256:{digest}\r\n".encode()
    )

    assert read_run_manifest(path) == {"a-1": digest}


def test_run_manifest_rejects_bad_ids_duplicates_and_too_many(tmp_path: Path):
    digest = "0" * 64
    with pytest.raises(BriefingError, match="expected article ID"):
        write_run_manifest({"../a": digest}, tmp_path / "manifest.md")
    with pytest.raises(BriefingError, match="expected digest"):
        write_run_manifest({"a-1": "bad"}, tmp_path / "manifest.md")
    with pytest.raises(BriefingError, match="too many expected IDs"):
        write_run_manifest(
            {f"a-{index}": digest for index in range(2001)}, tmp_path / "manifest.md"
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
        write_run_manifest({"a-1": "0" * 64}, link)
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
        write_run_manifest({"a-1": "0" * 64}, link / "manifest.md")
