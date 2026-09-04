from __future__ import annotations

from core.models import ChangeOperation, PlannedAction, RiskLevel
from core.safety import SafetyLayer


def test_draft_creation_does_not_require_confirmation() -> None:
    action = PlannedAction(
        operation=ChangeOperation.CREATE_POST,
        title="Create draft",
        description="Create a safe draft.",
        payload={"status": "draft"},
        risk=RiskLevel.LOW,
    )

    decision = SafetyLayer().evaluate([action])

    assert decision.requires_confirmation is False
    assert decision.requires_backup is False


def test_publish_requires_confirmation_even_for_new_content() -> None:
    action = PlannedAction(
        operation=ChangeOperation.CREATE_PAGE,
        title="Publish page",
        description="Publish a new page.",
        payload={"status": "publish"},
        risk=RiskLevel.LOW,
    )

    decision = SafetyLayer().evaluate([action])

    assert decision.requires_confirmation is True
    assert action.requires_confirmation is True
    assert "publish content" in " ".join(decision.reasons)


def test_update_delete_bulk_and_undo_require_confirmation() -> None:
    actions = [
        PlannedAction(
            operation=ChangeOperation.UPDATE_POST,
            title="Update post",
            description="Update an existing post.",
            payload={"id": 1},
            risk=RiskLevel.MEDIUM,
        ),
        PlannedAction(
            operation=ChangeOperation.DELETE_PAGE,
            title="Delete page",
            description="Delete a page.",
            payload={"id": 2},
            risk=RiskLevel.HIGH,
        ),
        PlannedAction(
            operation=ChangeOperation.BULK_UPDATE_PRODUCTS,
            title="Bulk prices",
            description="Change prices.",
            payload={"percent": 10},
            risk=RiskLevel.HIGH,
        ),
        PlannedAction(
            operation=ChangeOperation.UNDO_LAST_CHANGE,
            title="Undo",
            description="Undo last change.",
            payload={},
            risk=RiskLevel.HIGH,
        ),
    ]

    decision = SafetyLayer().evaluate(actions)

    assert decision.requires_confirmation is True
    assert decision.requires_backup is True
    assert decision.highest_risk == RiskLevel.HIGH
    assert all(action.requires_confirmation for action in actions)


def test_role_permission_blocks_admin_only_action_for_editor() -> None:
    action = PlannedAction(
        operation=ChangeOperation.UPDATE_PLUGIN,
        title="Deactivate plugin",
        description="Plugin changes require an administrator.",
        payload={"plugin_slug": "hello-dolly", "fields": {"status": "inactive"}},
        risk=RiskLevel.HIGH,
    )

    decision = SafetyLayer().evaluate([action], user_roles=["editor"])

    assert decision.allowed is False
    assert decision.blockers
    assert "administrator" in decision.blockers[0]


def test_app_role_blocks_viewer_from_content_changes() -> None:
    action = PlannedAction(
        operation=ChangeOperation.CREATE_POST,
        title="Create post",
        description="Create content.",
        payload={"status": "draft"},
        risk=RiskLevel.LOW,
    )

    decision = SafetyLayer().evaluate([action], app_role="viewer")

    assert decision.allowed is False
    assert "WordPressGenius role 'editor'" in decision.blockers[0]
