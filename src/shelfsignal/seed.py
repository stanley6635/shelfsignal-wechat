from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .state import StateStore

URL_PATTERN = re.compile(r"https?://[^\s)>]+")


@dataclass(frozen=True)
class SeedResult:
    scanned_files: int
    discovered: int
    imported: int


def seed_markdown_archive(path: Path, store: StateStore) -> SeedResult:
    files = sorted(path.rglob("*.md")) if path.is_dir() else [path]
    discovered = 0
    imported = 0
    for markdown_path in files:
        text = markdown_path.read_text(encoding="utf-8")
        for url in dict.fromkeys(URL_PATTERN.findall(text)):
            discovered += 1
            imported += int(store.seed_url(url.rstrip(".,").strip()))
    return SeedResult(len(files), discovered, imported)
