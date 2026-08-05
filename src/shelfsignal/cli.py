from __future__ import annotations

import argparse
import asyncio
import fcntl
import logging
import os
import re
import shutil
import stat
import sys
import traceback
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from . import __version__
from .auth import AuthPolicy, authenticated_context
from .briefing import (
    BriefingError,
    create_briefing_shell,
    initial_run_bindings,
    read_run_manifest,
    selected_ids,
    validate_briefing,
    write_run_manifest,
)
from .cards import build_card, write_cards
from .collector import MAX_LOOKBACK_DAYS, collect_articles
from .content import (
    atomic_write,
    article_dir_name,
    ensure_safe_directory,
    load_stored_article,
)
from .errors import ShelfSignalError
from .exporter import ExportError, export_selected
from .models import ArticleStatus, CollectionOmission, StoredArticle
from .ocr import ImageEvidence, ensure_helper, image_evidence, ocr_article, run_vision_ocr
from .seed import SeedError, seed_markdown_archive
from .state import StateError, StateStore
from .weread import ArticleClient, PlaywrightWeReadClient
from .workspace import (
    WorkspaceError,
    WorkspacePaths,
    initialize_workspace,
    validate_existing_workspace,
)

_SAFE_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_SAFE_ACCOUNT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_MAX_BRIEFING_BYTES = 16 * 1024 * 1024
_MAX_OMISSIONS_BYTES = 512 * 1024
_MAX_SOURCE_PROBE_BYTES = 4096
_MAX_OCR_BYTES = 8 * 1024 * 1024
_MAX_OMISSIONS = 2_000
_MAX_OMISSION_TEXT = 500
_MAX_BRIEFING_WARNINGS = 200
_MAX_PUBLIC_ERROR_TEXT = 1_000
_RESERVED_RUN_IDS = {"historical-seed"}
_RUN_LOCK_NAME = ".shelfsignal.lock"
_SECRET_HEADER = re.compile(
    r"(?is)\b(cookie|authorization)\s*:\s*"
    r"(?:(?!\b(?:cookie|authorization)\s*:|\r?\n).)*"
)


def redact_text(
    value: object,
    *,
    one_line: bool = False,
    max_characters: int | None = None,
) -> str:
    """Return authentication-safe text without changing unrelated content."""
    redacted = _SECRET_HEADER.sub(r"\1: [REDACTED]", str(value))
    if one_line:
        redacted = " ".join(redacted.split())
    if max_characters is not None:
        redacted = redacted[:max_characters]
    return redacted


class RedactingFilter(logging.Filter):
    """Redact HTTP authentication headers after logging arguments are formatted."""

    def filter(self, record: logging.LogRecord) -> bool:
        _redact_record(record)
        return True


def _redact_record(record: logging.LogRecord) -> None:
    """Sanitize every formatter-visible field on a ShelfSignal log record."""
    if record.exc_info is not None:
        record.exc_text = redact_text("".join(traceback.format_exception(*record.exc_info)))
    elif record.exc_text is not None:
        record.exc_text = redact_text(record.exc_text)
    if record.stack_info is not None:
        record.stack_info = redact_text(record.stack_info)
    try:
        rendered = record.getMessage()
    except Exception:  # noqa: BLE001 - privacy filtering must never break logging
        # Logging's own formatter will report a malformed message; never hide
        # the original operational failure by raising from the privacy filter.
        return
    redacted = redact_text(rendered)
    if redacted != rendered:
        record.msg = redacted
        record.args = ()


def _install_redacting_filter() -> None:
    current_factory = logging.getLogRecordFactory()
    if getattr(current_factory, "_shelfsignal_redacting_factory", False):
        return

    def redacting_factory(*args: object, **kwargs: object) -> logging.LogRecord:
        record = current_factory(*args, **kwargs)
        if record.name == "shelfsignal" or record.name.startswith("shelfsignal."):
            _redact_record(record)
        return record

    redacting_factory._shelfsignal_redacting_factory = True  # type: ignore[attr-defined]
    logging.setLogRecordFactory(redacting_factory)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="shelfsignal")
    parser.add_argument("--version", action="version", version=f"shelfsignal {__version__}")
    commands = parser.add_subparsers(dest="command")

    init_parser = commands.add_parser("init")
    init_parser.add_argument("workspace", type=Path)

    doctor = commands.add_parser("doctor")
    doctor.add_argument("--workspace", type=Path, required=True)

    list_accounts = commands.add_parser("list-accounts")
    list_accounts.add_argument("--workspace", type=Path, required=True)
    list_accounts.add_argument("--auth", choices=("fresh", "reuse"), default="fresh")
    list_accounts.add_argument("--run-id")

    seed = commands.add_parser("seed")
    seed.add_argument("--workspace", type=Path, required=True)
    seed.add_argument("archive", type=Path)

    collect = commands.add_parser("collect")
    collect.add_argument("--workspace", type=Path, required=True)
    collect.add_argument("--auth", choices=("fresh", "reuse"), default="fresh")
    collect.add_argument("--lookback-days", type=int, default=7)
    collect.add_argument("--run-id")
    collect.add_argument("--account", action="append", default=[])

    prepare = commands.add_parser("prepare-briefing")
    prepare.add_argument("--workspace", type=Path, required=True)
    prepare.add_argument("--run", required=True)

    validate = commands.add_parser("validate-briefing")
    validate.add_argument("--workspace", type=Path, required=True)
    validate.add_argument("briefing", type=Path)

    export = commands.add_parser("export")
    export.add_argument("--workspace", type=Path, required=True)
    export.add_argument("--briefing", type=Path, required=True)
    return parser


def new_run_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{uuid4().hex[:8]}"


def _safe_run_id(value: object) -> str:
    if not isinstance(value, str) or _SAFE_RUN_ID.fullmatch(value) is None:
        raise ValueError("run ID must be a safe single path component")
    return value


def _usable_run_id(value: object) -> str:
    run_id = _safe_run_id(value)
    if run_id in _RESERVED_RUN_IDS:
        raise ValueError("run ID is reserved for internal state")
    return run_id


def _safe_account_ids(values: Sequence[str]) -> set[str] | None:
    if len(values) > 2_000:
        raise ValueError("too many account filters")
    result: set[str] = set()
    for value in values:
        if not isinstance(value, str) or _SAFE_ACCOUNT_ID.fullmatch(value) is None:
            raise ValueError("account ID must be a safe identifier")
        result.add(value)
    return result or None


def _directory_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


def _open_directory_chain(path: Path, label: str) -> int:
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError(f"unsafe {label} path")
    normalized = Path(os.path.normpath(os.fspath(path)))
    if normalized != path or normalized == Path(normalized.anchor):
        raise ValueError(f"unsafe {label} path")
    try:
        descriptor = os.open(path.anchor, _directory_flags())
    except OSError as exc:
        raise ValueError(f"unsafe {label} path") from exc
    try:
        for part in path.parts[1:]:
            try:
                child = os.open(part, _directory_flags(), dir_fd=descriptor)
            except OSError as exc:
                raise ValueError(f"unsafe {label} directory") from exc
            try:
                current = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
                opened = os.fstat(child)
                if (
                    not stat.S_ISDIR(current.st_mode)
                    or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
                ):
                    raise ValueError(f"unsafe {label} directory")
            except BaseException:
                os.close(child)
                raise
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _verify_run_lock(directory_fd: int, descriptor: int) -> None:
    try:
        current = os.stat(_RUN_LOCK_NAME, dir_fd=directory_fd, follow_symlinks=False)
        opened = os.fstat(descriptor)
    except OSError as exc:
        raise StateError("run lease changed during acquisition") from exc
    if (
        not stat.S_ISREG(current.st_mode)
        or not stat.S_ISREG(opened.st_mode)
        or current.st_nlink != 1
        or opened.st_nlink != 1
        or opened.st_uid != os.getuid()
        or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
    ):
        raise StateError("run lease file is unsafe")


@contextmanager
def _run_lease(paths: WorkspacePaths, run_id: str) -> Iterator[None]:
    """Hold a process-exclusive lease for one public run lifecycle."""
    run_id = _usable_run_id(run_id)
    run_dir = ensure_safe_directory(paths.runs_dir / run_id, label="run")
    directory_fd = _open_directory_chain(run_dir, "run lease")
    descriptor: int | None = None
    try:
        try:
            descriptor = os.open(
                _RUN_LOCK_NAME,
                os.O_RDWR
                | os.O_CREAT
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0),
                0o600,
                dir_fd=directory_fd,
            )
        except OSError as exc:
            raise StateError("run lease file is unsafe") from exc
        _verify_run_lock(directory_fd, descriptor)
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise StateError("another process is already operating on this run") from exc
        except OSError as exc:
            raise StateError("run lease could not be acquired") from exc
        _verify_run_lock(directory_fd, descriptor)
        yield
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(directory_fd)


def _read_private_bytes(
    path: Path,
    boundary: Path,
    *,
    label: str,
    max_bytes: int,
    allow_truncate: bool = False,
) -> tuple[bytes, bool]:
    if not path.is_absolute() or not boundary.is_absolute() or ".." in path.parts:
        raise ValueError(f"unsafe {label} path")
    if Path(os.path.normpath(os.fspath(path))) != path:
        raise ValueError(f"unsafe {label} path")
    try:
        relative = path.relative_to(boundary)
    except ValueError as exc:
        raise ValueError(f"{label} path is outside its workspace boundary") from exc
    if not relative.parts or path == boundary:
        raise ValueError(f"unsafe {label} path")

    directory_fd = _open_directory_chain(boundary.joinpath(*relative.parts[:-1]), label)
    try:
        try:
            descriptor = os.open(
                relative.name,
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0),
                dir_fd=directory_fd,
            )
        except OSError as exc:
            raise ValueError(f"unsafe {label} file") from exc
        try:
            details = os.fstat(descriptor)
            if not stat.S_ISREG(details.st_mode):
                raise ValueError(f"{label} must be a regular file")
            oversized = details.st_size > max_bytes
            if oversized and not allow_truncate:
                raise ValueError(f"{label} is too large")
            content = bytearray()
            while len(content) < max_bytes:
                chunk = os.read(descriptor, min(64 * 1024, max_bytes - len(content)))
                if not chunk:
                    break
                content.extend(chunk)
            if not oversized:
                oversized = bool(os.read(descriptor, 1))
            if oversized and not allow_truncate:
                raise ValueError(f"{label} is too large")
            return bytes(content), oversized
        finally:
            os.close(descriptor)
    finally:
        os.close(directory_fd)


def _read_private_text(
    path: Path, boundary: Path, *, label: str, max_bytes: int
) -> str:
    content, _ = _read_private_bytes(
        path, boundary, label=label, max_bytes=max_bytes
    )
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{label} must be UTF-8 Markdown") from exc


def _source_text_length(item: StoredArticle) -> int:
    content, truncated = _read_private_bytes(
        item.source_path,
        item.directory,
        label="article source",
        max_bytes=_MAX_SOURCE_PROBE_BYTES,
        allow_truncate=True,
    )
    if truncated:
        return 300
    try:
        return len(content.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise ValueError("article source must be UTF-8 Markdown") from exc


def _briefing_context(paths: WorkspacePaths, briefing: Path) -> tuple[str, Path]:
    candidate = briefing.expanduser()
    if not candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError("briefing path must be an absolute workspace path")
    normalized = Path(os.path.normpath(os.fspath(candidate)))
    if normalized != candidate or candidate.parent != paths.briefings_dir:
        raise ValueError("briefing path is outside this workspace")
    if candidate.suffix != ".md":
        raise ValueError("briefing must be a Markdown file")
    run_id = _safe_run_id(candidate.stem)
    if candidate != paths.briefings_dir / f"{run_id}.md":
        raise ValueError("briefing path does not match its run ID")
    return run_id, candidate


def _ensure_unpublished_run(paths: WorkspacePaths, run_id: str) -> None:
    ensure_safe_directory(paths.runs_dir / run_id, label="run")
    ensure_safe_directory(paths.briefings_dir, label="briefing")
    artifacts = (
        paths.briefings_dir / f"{run_id}.md",
        paths.runs_dir / run_id / "manifest.md",
    )
    if any(os.path.lexists(path) for path in artifacts):
        raise StateError("run already has a published briefing or manifest")


def _require_preparable_run(store: StateStore, run_id: str) -> tuple[str, str]:
    details = store.run_details(run_id)
    if details is None:
        raise StateError("run does not exist")
    if details[0] not in {"running", "failed"}:
        raise StateError("run is not eligible for briefing preparation")
    return details


def _validate_briefing_run_header(markdown: str, run_id: str) -> None:
    first_line = markdown.split("\n", 1)[0]
    if first_line != f"# WeChat briefing · {run_id}":
        raise BriefingError("briefing run header does not match its filename")


def doctor_workspace(paths: WorkspacePaths) -> None:
    missing = [name for name in ("swiftc", "sips") if shutil.which(name) is None]
    if missing:
        raise ShelfSignalError(f"missing local tools: {', '.join(missing)}")
    StateStore(paths.state_db).initialize()


async def list_accounts_run(
    paths: WorkspacePaths,
    auth_policy: AuthPolicy,
    run_id: str,
) -> tuple[tuple[str, str], ...]:
    run_id = _safe_run_id(run_id)
    async with authenticated_context(paths.browser_dir, run_id, auth_policy) as context:
        page = context.pages[0] if context.pages else await context.new_page()
        accounts = await PlaywrightWeReadClient(context, page).shelf()
    return tuple((account.account_id, account.name) for account in accounts)


def _one_line(value: object) -> str:
    return redact_text(
        value, one_line=True, max_characters=_MAX_OMISSION_TEXT
    ).replace("`", "'")


def write_omissions(run_dir: Path, omissions: list[CollectionOmission]) -> Path:
    run_dir = ensure_safe_directory(run_dir, label="run")
    path = run_dir / "omissions.md"
    content = bytearray(b"# Visible partial failures\n\n")
    included = 0
    summary_reserve = 128
    for item in omissions[:_MAX_OMISSIONS]:
        line = (
            f"- {_one_line(item.scope)} `{_one_line(item.identifier)}`: "
            f"{_one_line(item.reason)}\n"
        ).encode()
        if len(content) + len(line) + summary_reserve > _MAX_OMISSIONS_BYTES:
            break
        content.extend(line)
        included += 1
    omitted = len(omissions) - included
    if omitted:
        summary = f"- run `omissions`: {omitted} more omitted\n".encode()
        if len(content) + len(summary) > _MAX_OMISSIONS_BYTES:
            raise AssertionError("omission summary reserve is insufficient")
        content.extend(summary)
    atomic_write(path, bytes(content))
    return path


def _existing_warnings(paths: WorkspacePaths, run_dir: Path) -> list[str]:
    omissions_path = run_dir / "omissions.md"
    try:
        text = _read_private_text(
            omissions_path,
            paths.runs_dir,
            label="omissions artifact",
            max_bytes=_MAX_OMISSIONS_BYTES,
        )
    except ValueError:
        if os.path.lexists(omissions_path):
            raise
        return []
    warnings = [line[2:] for line in text.splitlines() if line.startswith("- ")]
    if len(warnings) <= _MAX_BRIEFING_WARNINGS:
        return warnings
    visible = warnings[: _MAX_BRIEFING_WARNINGS - 1]
    visible.append(
        f"{len(warnings) - len(visible)} more collection warnings; see omissions.md"
    )
    return visible


def prepare_run(paths: WorkspacePaths, store: StateStore, run_id: str) -> Path:
    run_id = _usable_run_id(run_id)
    run_dir = ensure_safe_directory(paths.runs_dir / run_id, label="run")
    warnings = _existing_warnings(paths, run_dir)
    stored = tuple(
        load_stored_article(paths.library_dir / article_dir_name(article_id))
        for article_id in store.article_ids_for_run(run_id)
    )
    card_tuple = tuple(build_card(item) for item in stored)
    write_cards(card_tuple, run_dir / "cards.md")
    shell = create_briefing_shell(run_id, card_tuple, tuple(warnings))
    bindings = initial_run_bindings(shell)
    write_run_manifest(bindings, run_dir / "manifest.md")
    briefing = paths.briefings_dir / f"{run_id}.md"
    atomic_write(briefing, shell.encode("utf-8"))
    return briefing


async def process_client_run(
    paths: WorkspacePaths,
    store: StateStore,
    client: ArticleClient,
    lookback_days: int,
    run_id: str,
    helper: Path,
    evidence_probe: Callable[[Path], ImageEvidence] = image_evidence,
    ocr_runner: Callable[[Path], str] | None = None,
    account_ids: set[str] | None = None,
) -> Path:
    run_id = _usable_run_id(run_id)
    details = store.run_details(run_id)
    if details is None or details[0] != "running":
        raise StateError("article processing requires a running run")
    ensure_safe_directory(paths.runs_dir / run_id, label="run")

    def checkpoint(item: StoredArticle) -> None:
        status = (
            item.status
            if item.status is ArticleStatus.BODY_UNAVAILABLE
            else ArticleStatus.DISCOVERED
        )
        store.upsert_article(item.article, item.source_sha256, status, run_id)

    result = await collect_articles(
        client,
        paths.library_dir,
        lookback_days,
        run_id,
        is_known=store.is_known_url,
        on_stored=checkpoint,
        account_ids=account_ids,
    )
    omissions = list(result.omissions)
    runner = ocr_runner or (lambda image: run_vision_ocr(helper, image))
    for item in result.stored:
        evidence_items: list[ImageEvidence] = []
        for path in item.asset_paths:
            try:
                evidence_items.append(evidence_probe(path))
            except Exception as exc:  # noqa: BLE001 - one asset remains a visible omission
                omissions.append(
                    CollectionOmission("asset", item.article.article_id, type(exc).__name__)
                )
        ocr_path = ocr_article(
            item.directory,
            tuple(evidence_items),
            _source_text_length(item),
            paths.runs_dir / "ocr-cache",
            runner=runner,
        )
        updated = replace(item, ocr_path=ocr_path)
        status = item.status
        if status is ArticleStatus.COMPLETE and ocr_path is not None:
            ocr_text = _read_private_text(
                ocr_path,
                item.directory,
                label="OCR artifact",
                max_bytes=_MAX_OCR_BYTES,
            )
            if "OCR incomplete:" in ocr_text:
                status = ArticleStatus.OCR_INCOMPLETE
        store.upsert_article(updated.article, updated.source_sha256, status, run_id)
    write_omissions(paths.runs_dir / run_id, omissions)
    return prepare_run(paths, store, run_id)


async def collect_run(
    paths: WorkspacePaths,
    auth_policy: AuthPolicy,
    lookback_days: int,
    run_id: str,
    account_ids: set[str] | None = None,
) -> Path:
    run_id = _usable_run_id(run_id)
    if type(lookback_days) is not int or not 0 <= lookback_days <= MAX_LOOKBACK_DAYS:
        raise ValueError(f"lookback days must be between 0 and {MAX_LOOKBACK_DAYS}")
    with _run_lease(paths, run_id):
        return await _collect_run_locked(
            paths, auth_policy, lookback_days, run_id, account_ids
        )


async def _collect_run_locked(
    paths: WorkspacePaths,
    auth_policy: AuthPolicy,
    lookback_days: int,
    run_id: str,
    account_ids: set[str] | None,
) -> Path:
    store = StateStore(paths.state_db)
    store.initialize()
    details = store.run_details(run_id)
    if details is None:
        _ensure_unpublished_run(paths, run_id)
        store.start_run(run_id, auth_policy.value)
    else:
        status, original_policy = details
        if status not in {"running", "failed"}:
            raise StateError("run is not eligible for resume")
        if original_policy != auth_policy.value:
            raise StateError("run authentication policy does not match its original policy")
        _ensure_unpublished_run(paths, run_id)
        store.resume_run(run_id, auth_policy.value)
    try:
        async with authenticated_context(paths.browser_dir, run_id, auth_policy) as context:
            page = context.pages[0] if context.pages else await context.new_page()
            client = PlaywrightWeReadClient(context, page, lookback_days=lookback_days)
            briefing = await process_client_run(
                paths,
                store,
                client,
                lookback_days,
                run_id,
                ensure_helper(paths.runs_dir / "bin"),
                account_ids=account_ids,
            )
    except BaseException as primary_error:
        try:
            store.finish_run(run_id, "failed")
        except BaseException as finish_error:  # noqa: BLE001 - preserve the original failure
            primary_error.add_note(f"run status also failed to update: {finish_error}")
        raise
    store.finish_run(run_id, "complete")
    return briefing


def _safe_output_field(value: object) -> str:
    return " ".join(str(value).split())[:512]


def dispatch(args: argparse.Namespace) -> int:
    if args.command == "init":
        print(initialize_workspace(args.workspace).root)
        return 0
    paths = validate_existing_workspace(args.workspace)
    store = StateStore(paths.state_db)
    if args.command == "doctor":
        doctor_workspace(paths)
        print(f"workspace={paths.root} state=ok")
        return 0
    if args.command == "list-accounts":
        run_id = _usable_run_id(args.run_id or new_run_id())
        accounts = asyncio.run(list_accounts_run(paths, AuthPolicy(args.auth), run_id))
        for account_id, name in accounts:
            print(f"{_safe_output_field(account_id)}\t{_safe_output_field(name)}")
        return 0
    if args.command == "seed":
        store.initialize()
        result = seed_markdown_archive(args.archive, store)
        print(
            f"scanned={result.scanned_files} "
            f"discovered={result.discovered} imported={result.imported}"
        )
        return 0
    if args.command == "collect":
        run_id = _usable_run_id(args.run_id or new_run_id())
        account_ids = _safe_account_ids(args.account)
        briefing = asyncio.run(
            collect_run(
                paths,
                AuthPolicy(args.auth),
                args.lookback_days,
                run_id,
                account_ids,
            )
        )
        print(f"run={run_id} briefing={briefing}")
        return 0
    if args.command == "prepare-briefing":
        run_id = _usable_run_id(args.run)
        store.initialize()
        with _run_lease(paths, run_id):
            _require_preparable_run(store, run_id)
            _ensure_unpublished_run(paths, run_id)
            print(prepare_run(paths, store, run_id))
        return 0
    if args.command == "validate-briefing":
        run_id, briefing = _briefing_context(paths, args.briefing)
        bindings = read_run_manifest(paths.runs_dir / run_id / "manifest.md")
        markdown = _read_private_text(
            briefing,
            paths.briefings_dir,
            label="briefing",
            max_bytes=_MAX_BRIEFING_BYTES,
        )
        _validate_briefing_run_header(markdown, run_id)
        validate_briefing(markdown, bindings, require_unchecked=False)
        print("valid")
        return 0
    if args.command == "export":
        run_id, briefing = _briefing_context(paths, args.briefing)
        bindings = read_run_manifest(paths.runs_dir / run_id / "manifest.md")
        markdown = _read_private_text(
            briefing,
            paths.briefings_dir,
            label="briefing",
            max_bytes=_MAX_BRIEFING_BYTES,
        )
        _validate_briefing_run_header(markdown, run_id)
        validate_briefing(markdown, bindings, require_unchecked=False)
        destination = paths.exports_dir / f"{run_id}-selected"
        print(export_selected(selected_ids(markdown, bindings), paths.library_dir, destination))
        return 0
    raise ValueError("a command is required")


_PUBLIC_ERRORS = (
    ShelfSignalError,
    BriefingError,
    ExportError,
    SeedError,
    StateError,
    WorkspaceError,
    OSError,
    ValueError,
)


def main(argv: Sequence[str] | None = None) -> int:
    _install_redacting_filter()
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)
    if args.command is None:
        parser.print_help()
        return 2
    try:
        return dispatch(args)
    except _PUBLIC_ERRORS as exc:
        safe_message = redact_text(
            exc, one_line=True, max_characters=_MAX_PUBLIC_ERROR_TEXT
        )
        print(f"{exc.__class__.__name__}: {safe_message}", file=sys.stderr)
        return exc.exit_code if isinstance(exc, ShelfSignalError) else 1


def console_main() -> None:
    raise SystemExit(main())
