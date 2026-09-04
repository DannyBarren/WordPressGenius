from __future__ import annotations

from core.cache import TTLCache
from core.observability import UsageAnalytics
from core.security import AuthenticatedUser


def test_ttl_cache_returns_cached_value_and_clears() -> None:
    cache = TTLCache(ttl_seconds=60)
    cache.set(("posts", 1), {"items": [1]})

    assert cache.get(("posts", 1)) == {"items": [1]}
    cache.clear()
    assert cache.get(("posts", 1)) is None


def test_usage_analytics_requires_consent(tmp_path) -> None:
    analytics = UsageAnalytics(tmp_path / "usage.jsonl", enabled=True)
    user = AuthenticatedUser(username="alice", role="editor")

    analytics.record(user=user, event_type="agent_run", consent=False)
    analytics.record(
        user=user,
        event_type="agent_run",
        properties={"api_key": "secret", "approved": True},
        consent=True,
    )

    summary = analytics.summary()
    raw = (tmp_path / "usage.jsonl").read_text(encoding="utf-8")
    assert summary["total_events"] == 1
    assert "secret" not in raw
    assert "[REDACTED]" in raw
