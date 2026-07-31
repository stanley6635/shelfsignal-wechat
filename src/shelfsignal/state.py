from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
from datetime import UTC, datetime
from pathlib import Path

from .models import ArticleStatus, RemoteArticle

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    auth_policy TEXT NOT NULL,
    status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS articles (
    article_id TEXT PRIMARY KEY,
    source_sha256 TEXT NOT NULL,
    source_url_hash TEXT NOT NULL,
    account_id TEXT NOT NULL,
    published_at TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    status TEXT NOT NULL,
    run_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_articles_source_url_hash
ON articles(source_url_hash);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _url_hash(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


class StateError(ValueError):
    pass


class StateStore:
    def __init__(self, path: Path):
        self.path = path

    def _prepare_database_file(self) -> None:
        no_follow = getattr(os, "O_NOFOLLOW", 0)
        try:
            mode = self.path.lstat().st_mode
        except FileNotFoundError:
            try:
                descriptor = os.open(
                    self.path,
                    os.O_RDWR | os.O_CREAT | os.O_EXCL | no_follow,
                    0o600,
                )
            except OSError as exc:
                raise StateError(f"cannot securely create state database: {self.path}") from exc
        else:
            if not stat.S_ISREG(mode):
                raise StateError(f"state database path is not a regular file: {self.path}")
            try:
                descriptor = os.open(self.path, os.O_RDWR | no_follow)
            except OSError as exc:
                raise StateError(f"state database path is unsafe: {self.path}") from exc

        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise StateError(f"state database path is not a regular file: {self.path}")
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)

    def _connect(self) -> sqlite3.Connection:
        self._prepare_database_file()
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(SCHEMA)

    def start_run(self, run_id: str, auth_policy: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO runs VALUES (?, ?, NULL, ?, ?)",
                (run_id, _now(), auth_policy, "running"),
            )

    def finish_run(self, run_id: str, status: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE runs SET finished_at = ?, status = ? WHERE run_id = ?",
                (_now(), status, run_id),
            )

    def run_status(self, run_id: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT status FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        return None if row is None else str(row["status"])

    def upsert_article(
        self,
        article: RemoteArticle,
        source_sha256: str,
        status: ArticleStatus,
        run_id: str,
    ) -> None:
        now = _now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO articles (
                    article_id, source_sha256, source_url_hash, account_id,
                    published_at, first_seen_at, last_seen_at, status, run_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(article_id) DO UPDATE SET
                    source_sha256 = excluded.source_sha256,
                    source_url_hash = excluded.source_url_hash,
                    last_seen_at = excluded.last_seen_at,
                    status = excluded.status,
                    run_id = excluded.run_id
                """,
                (
                    article.article_id,
                    source_sha256,
                    _url_hash(article.source_url),
                    article.account_id,
                    article.published_at.isoformat(),
                    now,
                    now,
                    status.value,
                    run_id,
                ),
            )

    def is_complete(self, article_id: str, source_sha256: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT source_sha256, status FROM articles WHERE article_id = ?",
                (article_id,),
            ).fetchone()
        return bool(
            row
            and row["source_sha256"] == source_sha256
            and row["status"] == ArticleStatus.COMPLETE.value
        )

    def is_known_url(self, source_url: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM articles WHERE source_url_hash = ? AND status = ?",
                (_url_hash(source_url), ArticleStatus.COMPLETE.value),
            ).fetchone()
        return row is not None

    def article_ids_for_run(self, run_id: str) -> tuple[str, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT article_id FROM articles WHERE run_id = ? ORDER BY article_id",
                (run_id,),
            ).fetchall()
        return tuple(str(row["article_id"]) for row in rows)
