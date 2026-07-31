from datetime import UTC, datetime
from pathlib import Path

from shelfsignal.models import ArticleStatus, RemoteArticle
from shelfsignal.state import StateStore


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
