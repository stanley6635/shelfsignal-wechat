from __future__ import annotations

import argparse
from collections.abc import Sequence

from . import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="shelfsignal")
    parser.add_argument("--version", action="version", version=f"shelfsignal {__version__}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        build_parser().parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)
    return 0


def console_main() -> None:
    raise SystemExit(main())
