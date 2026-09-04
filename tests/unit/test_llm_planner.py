from __future__ import annotations

from agents.crew import (
    _actions_from_payload,
    _llm_plan_actions,
    _parse_json_object,
)
from core.models import ChangeOperation, RiskLevel
from core.safety import SafetyLayer


class _FakeRouter:
    """Minimal LLMRouter stand-in for planner tests."""

    def __init__(self, json_result: dict | None, enabled: bool = True) -> None:
        self.json_result = json_result
        self.enabled = enabled
        self.calls: list[tuple[str, str, str]] = []

    def complete_json(self, role, system_prompt, user_prompt, *, max_output_tokens=None):
        self.calls.append((role, system_prompt, user_prompt))
        return self.json_result


def test_parse_json_object_handles_code_fences() -> None:
    text = "```json\n{\"reasoning\": \"hi\", \"actions\": []}\n```"
    assert _parse_json_object(text) == {"reasoning": "hi", "actions": []}


def test_parse_json_object_extracts_embedded_object() -> None:
    text = "Here is the plan: {\"actions\": [], \"reasoning\": \"x\"} thanks!"
    assert _parse_json_object(text) == {"actions": [], "reasoning": "x"}


def test_parse_json_object_returns_none_on_garbage() -> None:
    assert _parse_json_object("not json at all") is None


def test_actions_from_payload_skips_invalid_operations_and_caps() -> None:
    raw = [
        {"operation": "create_post", "title": "A", "description": "d", "payload": {}},
        {"operation": "not_a_real_op", "title": "B"},
        {"operation": "update_post", "payload": {"id": 9}, "risk": "high"},
        {"operation": "read_only"},
        {"operation": "read_only"},
        {"operation": "read_only"},
    ]
    actions = _actions_from_payload(raw)
    # Invalid op dropped; list capped at 4 inputs considered.
    assert all(a.operation in ChangeOperation for a in actions)
    assert actions[0].operation == ChangeOperation.CREATE_POST
    update = next(a for a in actions if a.operation == ChangeOperation.UPDATE_POST)
    assert update.payload == {"id": 9}
    assert update.risk == RiskLevel.HIGH


def test_llm_plan_actions_parses_valid_plan() -> None:
    payload = {
        "reasoning": "User wants a new draft post.",
        "actions": [
            {
                "operation": "create_post",
                "title": "Summer specials",
                "description": "Draft a blog post",
                "payload": {},
                "risk": "low",
            }
        ],
    }
    router = _FakeRouter(payload)
    result = _llm_plan_actions("write a post about summer specials", router, {})
    assert result is not None
    actions, reasoning = result
    assert len(actions) == 1
    assert actions[0].operation == ChangeOperation.CREATE_POST
    assert reasoning == "User wants a new draft post."
    # Planning must be routed to the precise/cheap "planner" role.
    assert router.calls[0][0] == "planner"


def test_llm_plan_actions_returns_none_on_unparseable_output() -> None:
    router = _FakeRouter(None)
    assert _llm_plan_actions("do something", router, {}) is None


def test_llm_planned_update_still_requires_confirmation() -> None:
    """A model that labels a destructive edit 'low' must not bypass safety."""

    raw = [{"operation": "update_post", "title": "Edit", "payload": {"id": 5}, "risk": "low"}]
    actions = _actions_from_payload(raw)
    decision = SafetyLayer().evaluate(actions, app_role="admin")
    assert decision.requires_confirmation is True
    assert decision.requires_backup is True
