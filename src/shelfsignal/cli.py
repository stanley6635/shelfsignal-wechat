from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from . import __version__
from .workspace import initialize_workspace


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="shelfsignal")
    parser.add_argument("--version", action="version", version=f"shelfsignal {__version__}")
    subparsers = parser.add_subparsers(dest="command")
    init_parser = subparsers.add_parser("init")
    init_parser.add_argument("workspace", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)
    if args.command == "init":
        paths = initialize_workspace(args.workspace)
        print(paths.root)
    return 0


def console_main() -> None:
    raise SystemExit(main())
