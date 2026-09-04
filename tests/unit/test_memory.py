from __future__ import annotations

from core.memory import ActivityLog, SiteMemory


def test_activity_log_redacts_secret_fields(tmp_path) -> None:
    log = ActivityLog(tmp_path / "activity.jsonl")

    log.append(
        "credential_test",
        "application_password=super-secret",
        {"authorization": "Basic abc123", "safe": "value"},
    )

    events = log.recent()
    assert len(events) == 1
    assert "super-secret" not in events[0].message
    assert events[0].details["authorization"] == "[REDACTED]"
    assert events[0].details["safe"] == "value"


def test_site_memory_persists_recent_context_and_redacts(tmp_path) -> None:
    memory = SiteMemory(tmp_path / "memory.json")

    memory.remember_conversation(
        "Update homepage application_password=secret",
        "I updated the homepage.",
        {"token": "abc", "operation": "update_page"},
    )
    memory.remember_site_event("execution", "Changed page 42", {"page_id": 42})

    snapshot = memory.snapshot()

    assert snapshot["conversations"][0]["request"] != ""
    assert "secret" not in snapshot["conversations"][0]["request"]
    assert snapshot["conversations"][0]["metadata"]["token"] == "[REDACTED]"
    assert snapshot["site_history"][0]["details"]["page_id"] == 42


def test_site_memory_clear_resets_context(tmp_path) -> None:
    memory = SiteMemory(tmp_path / "memory.json")
    memory.remember_conversation("Create a draft", "Done")

    memory.clear()

    assert memory.snapshot()["conversations"] == []
    assert memory.snapshot()["site_history"] == []
