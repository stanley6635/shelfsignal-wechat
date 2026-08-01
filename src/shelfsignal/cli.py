from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import shutil
import stat
import sys
from collections.abc import Callable, Sequence
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
from .content import atomic_write, ensure_safe_directory, load_stored_article
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


class RedactingFilter(logging.Filter):
    """Redact HTTP authentication headers after logging arguments are formatted."""

    _secret = re.compile(
        r"(?is)\b(cookie|authorization)\s*:\s*"
        r"(?:(?!\b(?:cookie|authorization)\s*:|\r?\n).)*"
    )

    def filter(self, record: logging.LogRecord) -> bool:
        rendered = record.getMessage()
        redacted = self._secret.sub(r"\1: [REDACTED]", rendered)
        if redacted != rendered:
            record.msg = redacted
            record.args = ()
        return True


def _install_redacting_filter() -> None:
    logger = logging.getLogger("shelfsignal")
    if not any(isinstance(item, RedactingFilter) for item in logger.filters):
        logger.addFilter(RedactingFilter())


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
    return " ".join(str(value).split())[:_MAX_OMISSION_TEXT]


def write_omissions(run_dir: Path, omissions: list[CollectionOmission]) -> Path:
    run_dir = ensure_safe_directory(run_dir, label="run")
    path = run_dir / "omissions.md"
    lines = ["# Visible partial failures", ""]
    for item in omissions[:_MAX_OMISSIONS]:
        lines.append(
            f"- {_one_line(item.scope)} `{_one_line(item.identifier)}`: "
            f"{_one_line(item.reason)}"
        )
    if len(omissions) > _MAX_OMISSIONS:
        lines.append(f"- run `omissions`: {len(omissions) - _MAX_OMISSIONS} more omitted")
    content = ("\n".join(lines).rstrip() + "\n").encode("utf-8")
    if len(content) > _MAX_OMISSIONS_BYTES:
        raise ValueError("omissions artifact is too large")
    atomic_write(path, content)
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
    return [line[2:] for line in text.splitlines() if line.startswith("- ")]


def prepare_run(paths: WorkspacePaths, store: StateStore, run_id: str) -> Path:
    run_id = _safe_run_id(run_id)
    run_dir = ensure_safe_directory(paths.runs_dir / run_id, label="run")
    warnings = _existing_warnings(paths, run_dir)
    cards = []
    for article_id in store.article_ids_for_run(run_id):
        try:
            cards.append(build_card(load_stored_article(paths.library_dir / article_id)))
        except Exception as exc:  # noqa: BLE001 - corrupt articles remain visible as omissions
            warnings.append(f"article `{article_id}`: {type(exc).__name__}")
    card_tuple = tuple(cards)
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
    run_id = _safe_run_id(run_id)
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
    run_id = _safe_run_id(run_id)
    if type(lookback_days) is not int or not 0 <= lookback_days <= MAX_LOOKBACK_DAYS:
        raise ValueError(f"lookback days must be between 0 and {MAX_LOOKBACK_DAYS}")
    store = StateStore(paths.state_db)
    store.initialize()
    if store.run_status(run_id) is None:
        store.start_run(run_id, auth_policy.value)
    try:
        async with authenticated_context(paths.browser_dir, run_id, auth_policy) as context:
            page = context.pages[0] if context.pages else await context.new_page()
            client = PlaywrightWeReadClient(context, page)
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
        run_id = _safe_run_id(args.run_id or new_run_id())
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
        run_id = _safe_run_id(args.run_id or new_run_id())
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
        run_id = _safe_run_id(args.run)
        store.initialize()
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
        print(f"{exc.__class__.__name__}: {exc}", file=sys.stderr)
        return exc.exit_code if isinstance(exc, ShelfSignalError) else 1


def console_main() -> None:
    raise SystemExit(main())
