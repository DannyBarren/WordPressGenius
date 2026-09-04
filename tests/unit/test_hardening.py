from __future__ import annotations

import json

import responses

from core.memory import SiteMemory
from core.models import ChangeOperation, PlannedAction, RiskLevel
from core.preview import build_change_previews, render_previews
from core.rate_limit import SlidingWindowRateLimiter
from core.safety import SafetyLayer
from core.security import AuthenticatedUser, PromptGuard, SecurityAuditLog
from tests.helpers import post_payload


# --- Prompt guardrails -------------------------------------------------------


def test_prompt_guard_blocks_mass_destruction() -> None:
    guard = PromptGuard(max_length=200)
    result = guard.validate("Please delete all posts on the site right now")
    assert result.allowed is False
    assert any("mass deletion" in w for w in result.warnings)


def test_prompt_guard_strips_zero_width_and_normalizes() -> None:
    guard = PromptGuard(max_length=200)
    result = guard.validate("Create\u200b a draft\ufeff post about creatine")
    assert result.allowed is True
    assert "\u200b" not in result.sanitized_text
    assert "\ufeff" not in result.sanitized_text


def test_prompt_guard_warns_but_allows_suspicious_markup() -> None:
    guard = PromptGuard(max_length=200)
    result = guard.validate("Add a banner with <script>alert(1)</script> styling")
    assert result.allowed is True
    assert result.warnings


def test_prompt_guard_rejects_empty_after_sanitization() -> None:
    guard = PromptGuard(max_length=200)
    assert guard.validate("\u200b\u200b").allowed is False


# --- Tamper-evident audit log ------------------------------------------------


def test_audit_log_chain_verifies(tmp_path) -> None:
    log = SecurityAuditLog(tmp_path / "audit.jsonl")
    user = AuthenticatedUser(username="alice", role="admin")
    log.append(user=user, event_type="run_started", message="start")
    log.append(user=user, event_type="run_completed", message="done")

    records = log.read()
    assert len(records) == 2
    assert records[0]["seq"] == 1 and records[1]["seq"] == 2
    assert records[1]["prev_hash"] == records[0]["hash"]

    report = log.verify()
    assert report["ok"] is True
    assert report["count"] == 2


def test_audit_log_detects_tampering(tmp_path) -> None:
    path = tmp_path / "audit.jsonl"
    log = SecurityAuditLog(path)
    user = AuthenticatedUser(username="alice", role="admin")
    log.append(user=user, event_type="run_started", message="start")
    log.append(user=user, event_type="actions_executed", message="deleted a page")

    lines = path.read_text(encoding="utf-8").splitlines()
    tampered = json.loads(lines[1])
    tampered["message"] = "did nothing"
    lines[1] = json.dumps(tampered, sort_keys=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    report = log.verify()
    assert report["ok"] is False
    assert report["problems"]


# --- Capability-aware permissions -------------------------------------------


def _update_post_action() -> PlannedAction:
    return PlannedAction(
        operation=ChangeOperation.UPDATE_POST,
        title="Update post",
        description="Update content.",
        payload={"id": 1, "content": "x"},
        risk=RiskLevel.MEDIUM,
    )


def test_capability_blocks_when_required_capability_missing() -> None:
    layer = SafetyLayer(require_confirmation_for_major_changes=True)
    decision = layer.evaluate(
        [_update_post_action()],
        user_roles=["administrator"],
        app_role="admin",
        user_capabilities=["read"],
    )
    assert decision.allowed is False
    assert any("edit_posts" in blocker for blocker in decision.blockers)


def test_capability_allows_with_required_capability() -> None:
    layer = SafetyLayer(require_confirmation_for_major_changes=True)
    decision = layer.evaluate(
        [_update_post_action()],
        user_roles=["administrator"],
        app_role="admin",
        user_capabilities=["edit_posts"],
    )
    assert decision.allowed is True


def test_manage_options_capability_is_superuser() -> None:
    layer = SafetyLayer(require_confirmation_for_major_changes=True)
    decision = layer.evaluate(
        [_update_post_action()],
        user_roles=["administrator"],
        app_role="admin",
        user_capabilities=["manage_options"],
    )
    assert decision.allowed is True


def test_no_capabilities_falls_back_to_roles() -> None:
    layer = SafetyLayer(require_confirmation_for_major_changes=True)
    decision = layer.evaluate(
        [_update_post_action()],
        user_roles=["administrator"],
        app_role="admin",
        user_capabilities=[],
    )
    assert decision.allowed is True


# --- Rate limiting -----------------------------------------------------------


def test_rate_limiter_blocks_after_max_and_recovers() -> None:
    limiter = SlidingWindowRateLimiter(max_events=3, window_seconds=60)
    assert limiter.check("alice", now=0).allowed is True
    assert limiter.check("alice", now=1).allowed is True
    assert limiter.check("alice", now=2).allowed is True
    blocked = limiter.check("alice", now=3)
    assert blocked.allowed is False
    assert blocked.retry_after_seconds > 0
    # After the window passes the earliest event expires.
    assert limiter.check("alice", now=61).allowed is True


def test_rate_limiter_isolated_per_key() -> None:
    limiter = SlidingWindowRateLimiter(max_events=1, window_seconds=60)
    assert limiter.check("alice", now=0).allowed is True
    assert limiter.check("bob", now=0).allowed is True
    assert limiter.check("alice", now=1).allowed is False


# --- Long-term memory retrieval ---------------------------------------------


def test_site_memory_search_ranks_relevant(tmp_path) -> None:
    memory = SiteMemory(tmp_path / "memory.json")
    memory.remember_conversation("Write a guide about creatine monohydrate", "drafted creatine guide")
    memory.remember_conversation("Update the shipping and returns policy", "updated policy")

    results = memory.search("creatine dosage", limit=3)
    assert results
    assert "creatine" in results[0]["text"].lower()


def test_site_memory_search_empty_query(tmp_path) -> None:
    memory = SiteMemory(tmp_path / "memory.json")
    memory.remember_conversation("hello world", "hi")
    assert memory.search("the and for") == []


# --- Staged change previews --------------------------------------------------


@responses.activate
def test_preview_update_post_shows_field_diff(wp_base_url, wp_tools) -> None:
    responses.add(
        responses.GET,
        f"{wp_base_url}/wp-json/wp/v2/posts/123",
        json=post_payload(123, status="draft"),
        status=200,
    )
    action = PlannedAction(
        operation=ChangeOperation.UPDATE_POST,
        title="Update post",
        description="Publish and retitle.",
        payload={"id": 123, "title": "Brand New Title", "status": "publish", "content": "body"},
        risk=RiskLevel.MEDIUM,
    )

    previews = build_change_previews(wp_tools, [action])
    rendered = render_previews(previews)

    assert previews
    assert "Brand New Title" in rendered
    assert "publish" in rendered


@responses.activate
def test_preview_handles_unreadable_resource_gracefully(wp_base_url, wp_tools) -> None:
    responses.add(
        responses.GET,
        f"{wp_base_url}/wp-json/wp/v2/posts/999",
        json={"code": "rest_post_invalid_id"},
        status=404,
    )
    action = PlannedAction(
        operation=ChangeOperation.UPDATE_POST,
        title="Update missing post",
        description="x",
        payload={"id": 999, "content": "body"},
        risk=RiskLevel.MEDIUM,
    )
    assert build_change_previews(wp_tools, [action]) == []


# --- Enhanced undo / rollback ------------------------------------------------


@responses.activate
def test_undo_restores_woocommerce_product(wp_base_url, wp_client, backup_manager) -> None:
    snapshot = {"id": 55, "name": "Old Name", "regular_price": "19.99", "status": "publish"}
    responses.add(responses.GET, f"{wp_base_url}/wp-json/wc/v3/products/55", json=snapshot, status=200)
    action = PlannedAction(
        operation=ChangeOperation.WOOCOMMERCE_WRITE,
        title="Update product price",
        description="Lower the price.",
        payload={"action": "update_product", "id": 55, "fields": {"regular_price": "9.99"}},
        risk=RiskLevel.MEDIUM,
    )
    backup_manager.backup_for_actions(wp_client, [action])

    responses.add(responses.PUT, f"{wp_base_url}/wp-json/wc/v3/products/55", json=snapshot, status=200)
    result = backup_manager.undo_latest(wp_client)

    assert result["restored"][0]["id"] == 55
    assert result["unsupported"] == []


@responses.activate
def test_undo_restores_plugin_status(wp_base_url, wp_client, backup_manager) -> None:
    plugins = [{"plugin": "akismet/akismet", "status": "active", "name": "Akismet"}]
    responses.add(responses.GET, f"{wp_base_url}/wp-json/wp/v2/plugins", json=plugins, status=200)
    action = PlannedAction(
        operation=ChangeOperation.UPDATE_PLUGIN,
        title="Deactivate Akismet",
        description="Turn it off.",
        payload={"plugin_slug": "akismet", "status": "inactive"},
        risk=RiskLevel.HIGH,
    )
    backup_manager.backup_for_actions(wp_client, [action])

    responses.add(
        responses.POST,
        f"{wp_base_url}/wp-json/wp/v2/plugins/akismet/akismet",
        json={"plugin": "akismet/akismet", "status": "active"},
        status=200,
    )
    result = backup_manager.undo_latest(wp_client)

    assert result["restored"][0]["status"] == "active"
    assert result["unsupported"] == []
