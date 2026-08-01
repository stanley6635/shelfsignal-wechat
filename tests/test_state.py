import sqlite3
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

import shelfsignal.state as state_module
from shelfsignal.models import ArticleStatus, RemoteArticle
from shelfsignal.state import StateError, StateStore


def article() -> RemoteArticle:
    return RemoteArticle(
        article_id="MP_WXS_demo_001",
        account_id="account-demo",
        account_name="Example Account",
        title="Example article",
        source_url="https://example.invalid/article",
        published_at=datetime(2026, 7, 31, tzinfo=UTC),
    )


def test_state_round_trip_and_idempotency(tmp_path: Path):
    store = StateStore(tmp_path / "state.db")
    store.initialize()
    store.start_run("run-001", auth_policy="fresh")
    store.upsert_article(article(), "abc123", ArticleStatus.COMPLETE, "run-001")
    assert store.is_complete("MP_WXS_demo_001", "abc123")
    assert not store.is_complete("MP_WXS_demo_001", "changed")
    assert store.is_known_url("https://example.invalid/article")
    store.finish_run("run-001", "complete")
    assert store.run_status("run-001") == "complete"
    assert store.run_details("run-001") == ("complete", "fresh")


def test_resume_run_preserves_original_auth_policy_and_reopens_failed_run(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state.db")
    store.initialize()
    store.start_run("run-001", "fresh")
    store.finish_run("run-001", "failed")

    with pytest.raises(StateError, match="authentication policy"):
        store.resume_run("run-001", "reuse")
    assert store.run_details("run-001") == ("failed", "fresh")

    store.resume_run("run-001", "fresh")
    assert store.run_details("run-001") == ("running", "fresh")


def test_resume_run_rejects_missing_and_complete_runs(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    store.initialize()

    with pytest.raises(StateError, match="does not exist"):
        store.resume_run("missing", "fresh")

    store.start_run("run-001", "fresh")
    store.finish_run("run-001", "complete")
    with pytest.raises(StateError, match="not eligible"):
        store.resume_run("run-001", "fresh")


def test_initialize_rejects_database_symlink_without_touching_target(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside_database = tmp_path / "outside.db"
    outside_database.touch()
    database = workspace / "state.db"
    database.symlink_to(outside_database)

    with pytest.raises(StateError, match="not a regular file"):
        StateStore(database).initialize()

    assert outside_database.read_bytes() == b""


def test_initialize_rejects_non_regular_database_path(tmp_path: Path):
    database = tmp_path / "state.db"
    database.mkdir()

    with pytest.raises(StateError, match="not a regular file"):
        StateStore(database).initialize()


def test_initialize_creates_private_database(tmp_path: Path):
    database = tmp_path / "state.db"

    StateStore(database).initialize()

    assert stat.S_IMODE(database.stat().st_mode) == 0o600


def test_initialize_tightens_existing_database_mode(tmp_path: Path):
    database = tmp_path / "state.db"
    store = StateStore(database)
    store.initialize()
    database.chmod(0o644)

    store.initialize()

    assert stat.S_IMODE(database.stat().st_mode) == 0o600


def test_state_operations_close_connections_deterministically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    original_connect = sqlite3.connect

    class TrackingConnection(sqlite3.Connection):
        close_calls = 0

        def close(self) -> None:
            self.close_calls += 1
            super().close()

    connections: list[TrackingConnection] = []

    def tracked_connect(database: Path) -> TrackingConnection:
        connection = original_connect(database, factory=TrackingConnection)
        connections.append(connection)
        return connection

    monkeypatch.setattr(state_module.sqlite3, "connect", tracked_connect)
    store = StateStore(tmp_path / "state.db")
    store.initialize()
    store.start_run("run-001", auth_policy="fresh")
    for _ in range(300):
        assert store.run_status("run-001") == "running"
    with pytest.raises(sqlite3.IntegrityError):
        store.start_run("run-001", auth_policy="fresh")

    assert len(connections) == 303
    assert all(connection.close_calls == 1 for connection in connections)
    for connection in connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            connection.execute("SELECT 1")
