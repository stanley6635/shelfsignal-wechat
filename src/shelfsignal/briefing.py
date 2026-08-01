from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import stat
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from .content import atomic_write
from .models import ReadingCard

_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_ID_LINE = re.compile(r"<!-- shelfsignal:id=([A-Za-z0-9][A-Za-z0-9_.:-]{0,127}) -->\Z")
_DIGEST_LINE = re.compile(r"<!-- shelfsignal:digest=([0-9a-f]{64}) -->\Z")
_CHECK_LINE = re.compile(r"- \[([ xX])\] \*\*Select\*\*\Z")
_MANIFEST_ITEM = re.compile(
    r"- `([A-Za-z0-9][A-Za-z0-9_.:-]{0,127})` sha256:([0-9a-f]{64})\Z"
)
_RUN_HEADER = re.compile(r"# WeChat briefing · ([A-Za-z0-9][A-Za-z0-9_.:-]{0,127})\Z")
_ARTICLE_HEADER = re.compile(r"## Article · `([A-Za-z0-9][A-Za-z0-9_.:-]{0,127})`\Z")
_FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,}).*$")
_DNS_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")
_MAX_CARDS = 2_000
_MAX_WARNINGS = 200
_MAX_WARNING_CHARACTERS = 1_000
_MAX_TITLE_CHARACTERS = 512
_MAX_ACCOUNT_CHARACTERS = 256
_MAX_URL_CHARACTERS = 4_096
_MAX_EXCERPT_CHARACTERS = 10_000
_MAX_STATUS_CHARACTERS = 256
_MAX_BRIEFING_BYTES = 16 * 1024 * 1024
_MAX_MANIFEST_IDS = 2_000
_MAX_MANIFEST_BYTES = 512 * 1024
_MANIFEST_HEADER = "# ShelfSignal run manifest"


class BriefingError(ValueError):
    pass


def _validate_id(value: object, label: str) -> str:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        raise BriefingError(f"unsafe {label}: {value!r}")
    return value


def _bounded_text(value: object, limit: int, label: str) -> str:
    if not isinstance(value, str):
        raise BriefingError(f"{label} must be text")
    compact = re.sub(r"\s+", " ", value).strip()
    if len(compact) > limit:
        compact = compact[: limit - 1].rstrip() + "…"
    return compact


def _markdown_string(value: str) -> str:
    # JSON quoting makes line breaks and Markdown controls inert. Escaping the
    # HTML delimiters also prevents raw HTML blocks from untrusted fields.
    return (
        json.dumps(value, ensure_ascii=False)
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )


def _utc_instant(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise BriefingError("published_at must be timezone-aware")
    return value.astimezone(UTC)


def _canonical_digest(article_id: str, values: Mapping[str, str], evidence: str) -> str:
    payload = [article_id]
    payload.extend(values[label] for label, _ in _VISIBLE_FIELDS)
    payload.append(evidence)
    serialized = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def _safe_source_url(value: str) -> bool:
    if "\\" in value or any(character.isspace() for character in value):
        return False
    try:
        parsed = urlparse(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return False
    host = (hostname or "").rstrip(".")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return False
    return bool(
        parsed.scheme == "https"
        and host
        and host.lower() != "localhost"
        and len(host) <= 253
        and "." in host
        and all(_DNS_LABEL.fullmatch(label) for label in host.split("."))
        and parsed.username is None
        and parsed.password is None
        and port is None
    )


def create_briefing_shell(
    run_id: str,
    cards: tuple[ReadingCard, ...],
    warnings: tuple[str, ...] = (),
) -> str:
    run_id = _validate_id(run_id, "run ID")
    if len(cards) > _MAX_CARDS:
        raise BriefingError(f"too many reading cards; maximum is {_MAX_CARDS}")
    if len(warnings) > _MAX_WARNINGS:
        raise BriefingError(f"too many warnings; maximum is {_MAX_WARNINGS}")

    article_ids = [_validate_id(card.article_id, "article ID") for card in cards]
    if len(article_ids) != len(set(article_ids)):
        raise BriefingError("duplicate reading card article ID")

    lines = [
        f"# WeChat briefing · {run_id}",
        "",
        "Every candidate is visible. Ranking never preselects an article.",
        "",
    ]
    if warnings:
        lines.extend(["## Collection warnings", ""])
        lines.extend(
            f"- {_markdown_string(_bounded_text(item, _MAX_WARNING_CHARACTERS, 'warning'))}"
            for item in warnings
        )
        lines.append("")

    for card in cards:
        title = _bounded_text(card.title, _MAX_TITLE_CHARACTERS, "title")
        account = _bounded_text(
            card.account_name, _MAX_ACCOUNT_CHARACTERS, "account name"
        )
        if not isinstance(card.source_url, str):
            raise BriefingError("source URL must be text")
        if len(card.source_url) > _MAX_URL_CHARACTERS:
            raise BriefingError("Source URL is too long")
        source_url = card.source_url
        if not _safe_source_url(source_url):
            raise BriefingError("Source must be a safe HTTPS URL")
        retrieval = _bounded_text(
            card.retrieval_status, _MAX_STATUS_CHARACTERS, "retrieval status"
        )
        ocr = _bounded_text(card.ocr_status, _MAX_STATUS_CHARACTERS, "OCR status")
        excerpt = _bounded_text(card.excerpt, _MAX_EXCERPT_CHARACTERS, "excerpt")
        published = _utc_instant(card.published_at).isoformat()
        values = {
            "Title": title,
            "Account": account,
            "Published": published,
            "Source": source_url,
            "Retrieval": retrieval,
            "OCR": ocr,
        }
        digest = _canonical_digest(card.article_id, values, excerpt)
        lines.extend(
            [
                f"## Article · `{card.article_id}`",
                f"<!-- shelfsignal:id={card.article_id} -->",
                f"<!-- shelfsignal:digest={digest} -->",
                "- [ ] **Select**",
                f"- Title: {_markdown_string(title)}",
                f"- Account: {_markdown_string(account)}",
                f"- Published: {_markdown_string(published)}",
                f"- Source: {_markdown_string(source_url)}",
                f"- Retrieval: {_markdown_string(retrieval)}",
                f"- OCR: {_markdown_string(ocr)}",
                "",
                f"- Evidence: {_markdown_string(excerpt)}",
                "",
                "### Agent ranking",
                "",
                "- Summary: Awaiting host-agent ranking",
                "- Reason: Awaiting host-agent ranking",
                "- Confidence: Awaiting host-agent ranking",
                "",
            ]
        )

    result = "\n".join(lines).rstrip() + "\n"
    if len(result.encode("utf-8")) > _MAX_BRIEFING_BYTES:
        raise BriefingError("briefing artifact is too large")
    return result


def _expected_bindings(expected: Mapping[str, str]) -> dict[str, str]:
    if not isinstance(expected, Mapping):
        raise BriefingError("expected manifest bindings must be an ID-to-digest mapping")
    if len(expected) > _MAX_CARDS:
        raise BriefingError(f"too many expected IDs; maximum is {_MAX_CARDS}")
    result: dict[str, str] = {}
    for article_id, digest in expected.items():
        article_id = _validate_id(article_id, "expected article ID")
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise BriefingError(f"invalid expected digest for {article_id}")
        result[article_id] = digest
    return result


_VISIBLE_FIELDS = (
    ("Title", _MAX_TITLE_CHARACTERS),
    ("Account", _MAX_ACCOUNT_CHARACTERS),
    ("Published", 64),
    ("Source", _MAX_URL_CHARACTERS),
    ("Retrieval", _MAX_STATUS_CHARACTERS),
    ("OCR", _MAX_STATUS_CHARACTERS),
)
_ARTICLE_CONTROL_PREFIXES = (
    "## Article",
    "<!-- shelfsignal:id=",
    "<!-- shelfsignal:digest=",
    "- Evidence",
    *tuple(f"- {label}" for label, _ in _VISIBLE_FIELDS),
)


def _looks_like_article_control(line: str) -> bool:
    stripped = line.lstrip()
    return bool(
        stripped.startswith(_ARTICLE_CONTROL_PREFIXES)
        or _CHECK_LINE.fullmatch(stripped)
    )


def _reject_wrapped_controls(lines: list[str]) -> None:
    fence: tuple[str, int] | None = None
    in_comment = False
    for line in lines:
        if fence is not None:
            if _looks_like_article_control(line):
                raise BriefingError("article control is inside fenced code")
            character, length = fence
            if re.fullmatch(rf" {{0,3}}{re.escape(character)}{{{length},}}\s*", line):
                fence = None
            continue
        if in_comment:
            if _looks_like_article_control(line):
                raise BriefingError("article control is inside an outer HTML comment")
            if "-->" in line:
                in_comment = False
            continue

        opening = _FENCE_OPEN.match(line)
        if opening:
            marker = opening.group(1)
            fence = (marker[0], len(marker))
            continue

        if (
            "<!--" in line
            and _ID_LINE.fullmatch(line) is None
            and _DIGEST_LINE.fullmatch(line) is None
        ):
            if _looks_like_article_control(line):
                raise BriefingError("article control is inside an outer HTML comment")
            after_open = line.split("<!--", 1)[1]
            if "-->" not in after_open:
                in_comment = True

    if fence is not None:
        raise BriefingError("unterminated fenced code block")
    if in_comment:
        raise BriefingError("unterminated outer HTML comment")


def _reject_raw_html(lines: list[str]) -> None:
    for line in lines:
        if _ID_LINE.fullmatch(line) or _DIGEST_LINE.fullmatch(line):
            continue
        if "<" in line or ">" in line:
            raise BriefingError("raw HTML delimiter is not allowed in a briefing")


def _parse_json_field(line: str, label: str, limit: int) -> str:
    prefix = f"- {label}: "
    if not line.startswith(prefix):
        raise BriefingError(f"missing or misplaced {label} field")
    suffix = line[len(prefix) :]
    try:
        value = json.loads(suffix)
    except json.JSONDecodeError as exc:
        raise BriefingError(f"malformed {label} field") from exc
    if not isinstance(value, str) or len(value) > limit:
        raise BriefingError(f"malformed {label} field")
    if suffix != _markdown_string(value):
        raise BriefingError(f"non-canonical {label} field encoding")
    return value


def _parse_evidence(line: str) -> str:
    prefix = "- Evidence: "
    if not line.startswith(prefix):
        raise BriefingError("missing or misplaced Evidence field")
    suffix = line[len(prefix) :]
    try:
        value = json.loads(suffix)
    except json.JSONDecodeError as exc:
        raise BriefingError("malformed Evidence field") from exc
    if not isinstance(value, str) or len(value) > _MAX_EXCERPT_CHARACTERS:
        raise BriefingError("malformed Evidence field")
    if suffix != _markdown_string(value):
        raise BriefingError("non-canonical Evidence field encoding")
    return value


def _id_checks(markdown: str) -> tuple[tuple[str, str, str], ...]:
    if not isinstance(markdown, str):
        raise BriefingError("briefing must be text")
    if len(markdown.encode("utf-8")) > _MAX_BRIEFING_BYTES:
        raise BriefingError("briefing artifact is too large")

    lines = markdown.splitlines()
    if not lines or _RUN_HEADER.fullmatch(lines[0]) is None:
        raise BriefingError("invalid briefing run header")
    if sum(line.startswith("# WeChat briefing ·") for line in lines) != 1:
        raise BriefingError("duplicate briefing run header")
    _reject_raw_html(lines)
    _reject_wrapped_controls(lines)

    pairs: list[tuple[str, str, str]] = []
    canonical_indices: set[int] = set()
    for index, line in enumerate(lines):
        if not line.startswith("## Article"):
            continue
        header = _ARTICLE_HEADER.fullmatch(line)
        if header is None:
            raise BriefingError("malformed article section header")
        if index + 13 >= len(lines):
            raise BriefingError("incomplete canonical article section")
        hidden = _ID_LINE.fullmatch(lines[index + 1])
        if hidden is None:
            raise BriefingError("article header is not followed by a hidden ID")
        if header.group(1) != hidden.group(1):
            raise BriefingError("article header and hidden ID differ")
        digest_match = _DIGEST_LINE.fullmatch(lines[index + 2])
        if digest_match is None:
            raise BriefingError("article ID is not followed by a canonical digest")
        check = _CHECK_LINE.fullmatch(lines[index + 3])
        if check is None:
            raise BriefingError("article digest and checkbox must be adjacent")

        values: dict[str, str] = {}
        for offset, (label, limit) in enumerate(_VISIBLE_FIELDS, start=4):
            values[label] = _parse_json_field(lines[index + offset], label, limit)
        try:
            published = datetime.fromisoformat(values["Published"])
        except ValueError as exc:
            raise BriefingError("malformed Published field") from exc
        _utc_instant(published)
        if not _safe_source_url(values["Source"]):
            raise BriefingError("malformed Source field: expected safe HTTPS URL")
        if lines[index + 10] != "":
            raise BriefingError("missing separator before Evidence field")
        evidence = _parse_evidence(lines[index + 11])
        recomputed = _canonical_digest(hidden.group(1), values, evidence)
        if digest_match.group(1) != recomputed:
            raise BriefingError(f"canonical payload digest mismatch: {hidden.group(1)}")
        if lines[index + 12] != "" or lines[index + 13] != "### Agent ranking":
            raise BriefingError("missing canonical Agent ranking section")

        canonical_indices.update(range(index, index + 12))
        pairs.append((hidden.group(1), check.group(1), digest_match.group(1)))
        if len(pairs) > _MAX_CARDS:
            raise BriefingError(f"too many article controls; maximum is {_MAX_CARDS}")

    for index, line in enumerate(lines):
        if index in canonical_indices:
            continue
        if _CHECK_LINE.fullmatch(line) or _ID_LINE.fullmatch(line) or _DIGEST_LINE.fullmatch(line):
            raise BriefingError("article control is not attached to a canonical article")
        if _looks_like_article_control(line):
            raise BriefingError("decoy or duplicate article field/control")
    return tuple(pairs)


def validate_briefing(
    markdown: str,
    expected_bindings: Mapping[str, str],
    require_unchecked: bool,
) -> tuple[str, ...]:
    expected = _expected_bindings(expected_bindings)
    pairs = _id_checks(markdown)
    ids = [article_id for article_id, _, _ in pairs]
    duplicates = sorted(item for item, count in Counter(ids).items() if count > 1)
    missing = sorted(set(expected) - set(ids))
    invented = sorted(set(ids) - set(expected))
    if duplicates:
        raise BriefingError(f"duplicate IDs: {duplicates}")
    if invented:
        raise BriefingError(f"invented IDs: {invented}")
    if missing:
        raise BriefingError(f"missing IDs: {missing}")
    mismatched = sorted(
        article_id
        for article_id, _, digest in pairs
        if expected.get(article_id) != digest
    )
    if mismatched:
        raise BriefingError(f"manifest digest mismatch: {mismatched}")
    if require_unchecked and any(mark.lower() == "x" for _, mark, _ in pairs):
        raise BriefingError("initial briefing contains a checked item")
    return tuple(ids)


def selected_ids(markdown: str, expected_bindings: Mapping[str, str]) -> tuple[str, ...]:
    """Return checked IDs only after validating against manifest-derived IDs."""
    validate_briefing(markdown, expected_bindings, require_unchecked=False)
    return tuple(
        article_id
        for article_id, mark, _ in _id_checks(markdown)
        if mark.lower() == "x"
    )


def initial_run_bindings(markdown: str) -> dict[str, str]:
    """Extract bindings from the pristine shell before any host-agent editing.

    Task 13 integration contract: call this immediately after
    ``create_briefing_shell``, persist the result with ``write_run_manifest``,
    and later pass only ``read_run_manifest`` output to ``validate_briefing``
    and ``selected_ids``. Never regenerate bindings from an edited briefing.
    """
    pairs = _id_checks(markdown)
    ids = [article_id for article_id, _, _ in pairs]
    duplicates = sorted(item for item, count in Counter(ids).items() if count > 1)
    if duplicates:
        raise BriefingError(f"duplicate IDs: {duplicates}")
    return {article_id: digest for article_id, _, digest in pairs}


def write_run_manifest(bindings: Mapping[str, str], path: Path) -> Path:
    values = _expected_bindings(bindings)
    if len(values) > _MAX_MANIFEST_IDS:
        raise BriefingError(f"too many manifest IDs; maximum is {_MAX_MANIFEST_IDS}")
    lines = [_MANIFEST_HEADER, ""]
    lines.extend(
        f"- `{article_id}` sha256:{values[article_id]}" for article_id in sorted(values)
    )
    content = ("\n".join(lines) + "\n").encode("utf-8")
    if len(content) > _MAX_MANIFEST_BYTES:
        raise BriefingError("run manifest is too large")
    atomic_write(path, content)
    return path


def _open_parent_directory(path: Path) -> tuple[int, str]:
    if not path.is_absolute() or ".." in path.parts:
        raise BriefingError("unsafe run manifest path")
    normalized = Path(os.path.normpath(os.fspath(path)))
    if normalized != path or normalized == Path(normalized.anchor):
        raise BriefingError("unsafe run manifest path")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path.anchor, flags)
    except OSError as exc:
        raise BriefingError("unsafe run manifest path") from exc
    try:
        for part in path.parent.parts[1:]:
            try:
                child = os.open(part, flags, dir_fd=descriptor)
            except OSError as exc:
                raise BriefingError("unsafe run manifest parent") from exc
            os.close(descriptor)
            descriptor = child
        return descriptor, path.name
    except BaseException:
        os.close(descriptor)
        raise


def _read_manifest_bytes(path: Path) -> bytes:
    parent_fd, name = _open_parent_directory(path)
    try:
        try:
            descriptor = os.open(
                name,
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0),
                dir_fd=parent_fd,
            )
        except OSError as exc:
            raise BriefingError("unsafe run manifest file") from exc
        try:
            details = os.fstat(descriptor)
            if not stat.S_ISREG(details.st_mode):
                raise BriefingError("run manifest must be a regular file")
            if details.st_size > _MAX_MANIFEST_BYTES:
                raise BriefingError("run manifest is too large")
            content = bytearray()
            while len(content) <= _MAX_MANIFEST_BYTES:
                chunk = os.read(
                    descriptor,
                    min(64 * 1024, _MAX_MANIFEST_BYTES + 1 - len(content)),
                )
                if not chunk:
                    break
                content.extend(chunk)
            if len(content) > _MAX_MANIFEST_BYTES:
                raise BriefingError("run manifest is too large")
            return bytes(content)
        finally:
            os.close(descriptor)
    finally:
        os.close(parent_fd)


def read_run_manifest(path: Path) -> dict[str, str]:
    try:
        text = _read_manifest_bytes(path).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BriefingError("run manifest must be UTF-8 Markdown") from exc
    text = text.replace("\r\n", "\n")
    if "\r" in text or not text.endswith("\n"):
        raise BriefingError("invalid run manifest line endings")
    lines = text[:-1].split("\n")
    if len(lines) < 2 or lines[:2] != [_MANIFEST_HEADER, ""]:
        raise BriefingError("invalid run manifest header")
    bindings: dict[str, str] = {}
    ids: list[str] = []
    for line in lines[2:]:
        match = _MANIFEST_ITEM.fullmatch(line)
        if match is None:
            raise BriefingError("invalid run manifest content")
        article_id, digest = match.groups()
        ids.append(article_id)
        if article_id in bindings:
            raise BriefingError("duplicate manifest IDs")
        bindings[article_id] = digest
    if len(ids) > _MAX_MANIFEST_IDS:
        raise BriefingError(f"too many manifest IDs; maximum is {_MAX_MANIFEST_IDS}")
    if len(ids) != len(set(ids)):
        raise BriefingError("duplicate manifest IDs")
    if ids != sorted(ids):
        raise BriefingError("run manifest IDs are not sorted")
    return bindings
