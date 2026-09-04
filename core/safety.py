"""Safety guardrails and role-aware permission checks for WordPress changes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from core.models import ChangeOperation, PlannedAction, RiskLevel


CONFIRMATION_OPERATIONS = {
    ChangeOperation.UPDATE_POST,
    ChangeOperation.DELETE_POST,
    ChangeOperation.UPDATE_PAGE,
    ChangeOperation.DELETE_PAGE,
    ChangeOperation.UPDATE_SETTINGS,
    ChangeOperation.UPDATE_THEME,
    ChangeOperation.UPDATE_PLUGIN,
    ChangeOperation.SEO_OPTIMIZE,
    ChangeOperation.BULK_UPDATE_PRODUCTS,
    ChangeOperation.UNDO_LAST_CHANGE,
    ChangeOperation.WOOCOMMERCE_WRITE,
    ChangeOperation.STRIPE_REFUND,
    ChangeOperation.SEO_PLUGIN_BULK,
    ChangeOperation.PLUGIN_ACTION,
}

BACKUP_OPERATIONS = {
    ChangeOperation.UPDATE_POST,
    ChangeOperation.DELETE_POST,
    ChangeOperation.UPDATE_PAGE,
    ChangeOperation.DELETE_PAGE,
    ChangeOperation.UPDATE_SETTINGS,
    ChangeOperation.UPDATE_THEME,
    ChangeOperation.UPDATE_PLUGIN,
    ChangeOperation.SEO_OPTIMIZE,
    ChangeOperation.BULK_UPDATE_PRODUCTS,
    ChangeOperation.WOOCOMMERCE_WRITE,
    ChangeOperation.STRIPE_REFUND,
    ChangeOperation.SEO_PLUGIN_BULK,
    ChangeOperation.PLUGIN_ACTION,
}

PUBLISHABLE_OPERATIONS = {
    ChangeOperation.CREATE_POST,
    ChangeOperation.UPDATE_POST,
    ChangeOperation.CREATE_PAGE,
    ChangeOperation.UPDATE_PAGE,
}

CONTENT_ROLES = {"administrator", "editor", "author"}
PAGE_ROLES = {"administrator", "editor"}
MEDIA_ROLES = {"administrator", "editor", "author"}
ADMIN_ROLES = {"administrator"}
COMMERCE_ROLES = {"administrator", "shop_manager"}
READ_ROLES = {"administrator", "editor", "author", "contributor", "subscriber", "shop_manager"}

APP_ROLE_REQUIREMENTS: dict[ChangeOperation, str] = {
    ChangeOperation.CREATE_POST: "editor",
    ChangeOperation.UPDATE_POST: "editor",
    ChangeOperation.DELETE_POST: "editor",
    ChangeOperation.CREATE_PAGE: "editor",
    ChangeOperation.UPDATE_PAGE: "editor",
    ChangeOperation.DELETE_PAGE: "editor",
    ChangeOperation.UPLOAD_MEDIA: "editor",
    ChangeOperation.UPDATE_SETTINGS: "admin",
    ChangeOperation.UPDATE_THEME: "admin",
    ChangeOperation.UPDATE_PLUGIN: "admin",
    ChangeOperation.TROUBLESHOOT_FORM: "viewer",
    ChangeOperation.SEO_OPTIMIZE: "editor",
    ChangeOperation.BULK_UPDATE_PRODUCTS: "admin",
    ChangeOperation.PLUGIN_TROUBLESHOOT: "viewer",
    ChangeOperation.ANALYTICS_SUMMARY: "viewer",
    ChangeOperation.UNDO_LAST_CHANGE: "admin",
    ChangeOperation.READ_ONLY: "viewer",
    ChangeOperation.WOOCOMMERCE_READ: "viewer",
    ChangeOperation.WOOCOMMERCE_WRITE: "admin",
    ChangeOperation.STRIPE_READ: "viewer",
    ChangeOperation.STRIPE_REFUND: "admin",
    ChangeOperation.PLUGIN_INVENTORY: "viewer",
    ChangeOperation.SEO_PLUGIN_BULK: "editor",
    ChangeOperation.PLUGIN_READ: "viewer",
    ChangeOperation.PLUGIN_ACTION: "editor",
}

APP_ROLE_RANK = {"viewer": 0, "editor": 1, "admin": 2}

# Per-operation WordPress capability requirements. Capabilities are far more
# precise than role names (a custom role may have `edit_posts` without being an
# "editor"). When the connected user's capabilities are known, write/admin
# operations are checked against these; reads and plugin-framework actions fall
# back to role checks. Operations not listed here are not capability-gated.
CAPABILITY_REQUIREMENTS: dict[ChangeOperation, str] = {
    ChangeOperation.CREATE_POST: "edit_posts",
    ChangeOperation.UPDATE_POST: "edit_posts",
    ChangeOperation.DELETE_POST: "delete_posts",
    ChangeOperation.CREATE_PAGE: "edit_pages",
    ChangeOperation.UPDATE_PAGE: "edit_pages",
    ChangeOperation.DELETE_PAGE: "delete_pages",
    ChangeOperation.UPLOAD_MEDIA: "upload_files",
    ChangeOperation.UPDATE_SETTINGS: "manage_options",
    ChangeOperation.UPDATE_THEME: "switch_themes",
    ChangeOperation.UPDATE_PLUGIN: "activate_plugins",
    ChangeOperation.SEO_OPTIMIZE: "edit_posts",
    ChangeOperation.SEO_PLUGIN_BULK: "edit_posts",
    ChangeOperation.BULK_UPDATE_PRODUCTS: "manage_woocommerce",
    ChangeOperation.WOOCOMMERCE_WRITE: "manage_woocommerce",
    ChangeOperation.STRIPE_REFUND: "manage_woocommerce",
}

ROLE_REQUIREMENTS: dict[ChangeOperation, set[str]] = {
    ChangeOperation.CREATE_POST: CONTENT_ROLES,
    ChangeOperation.UPDATE_POST: CONTENT_ROLES,
    ChangeOperation.DELETE_POST: {"administrator", "editor"},
    ChangeOperation.CREATE_PAGE: PAGE_ROLES,
    ChangeOperation.UPDATE_PAGE: PAGE_ROLES,
    ChangeOperation.DELETE_PAGE: PAGE_ROLES,
    ChangeOperation.UPLOAD_MEDIA: MEDIA_ROLES,
    ChangeOperation.UPDATE_SETTINGS: ADMIN_ROLES,
    ChangeOperation.UPDATE_THEME: ADMIN_ROLES,
    ChangeOperation.UPDATE_PLUGIN: ADMIN_ROLES,
    ChangeOperation.TROUBLESHOOT_FORM: READ_ROLES,
    ChangeOperation.SEO_OPTIMIZE: CONTENT_ROLES,
    ChangeOperation.BULK_UPDATE_PRODUCTS: COMMERCE_ROLES,
    ChangeOperation.PLUGIN_TROUBLESHOOT: READ_ROLES,
    ChangeOperation.ANALYTICS_SUMMARY: READ_ROLES,
    ChangeOperation.UNDO_LAST_CHANGE: {"administrator", "editor"},
    ChangeOperation.READ_ONLY: READ_ROLES,
    ChangeOperation.WOOCOMMERCE_READ: READ_ROLES,
    ChangeOperation.WOOCOMMERCE_WRITE: COMMERCE_ROLES,
    ChangeOperation.STRIPE_READ: COMMERCE_ROLES,
    ChangeOperation.STRIPE_REFUND: COMMERCE_ROLES,
    ChangeOperation.PLUGIN_INVENTORY: READ_ROLES,
    ChangeOperation.SEO_PLUGIN_BULK: CONTENT_ROLES,
    ChangeOperation.PLUGIN_READ: READ_ROLES,
    ChangeOperation.PLUGIN_ACTION: {"administrator", "editor", "shop_manager"},
}


@dataclass(frozen=True)
class SafetyDecision:
    """Result of evaluating a proposed WordPress action plan."""

    requires_confirmation: bool
    requires_backup: bool
    highest_risk: RiskLevel
    reasons: list[str]
    allowed: bool = True
    blockers: list[str] = field(default_factory=list)


class SafetyLayer:
    """Classify planned actions before execution.

    The business rule is intentionally conservative: any publish, update, or
    delete action must be explicitly approved by the user before execution. When
    WordPress roles are available, actions are also checked against coarse REST
    API permission boundaries so the agent does not attempt obviously forbidden
    work.
    """

    def __init__(self, require_confirmation_for_major_changes: bool = True) -> None:
        self.require_confirmation_for_major_changes = (
            require_confirmation_for_major_changes
        )

    def evaluate(
        self,
        actions: Iterable[PlannedAction],
        *,
        user_roles: Iterable[str] | None = None,
        app_role: str | None = None,
        user_capabilities: Iterable[str] | None = None,
    ) -> SafetyDecision:
        """Return confirmation, backup, and permission requirements for actions."""

        roles = {role.lower() for role in user_roles or []}
        capabilities = {cap.lower() for cap in user_capabilities or []}
        reasons: list[str] = []
        blockers: list[str] = []
        requires_confirmation = False
        requires_backup = False
        highest_risk = RiskLevel.LOW

        for action in actions:
            if action.operation in BACKUP_OPERATIONS:
                requires_backup = True
                reasons.append(f"{action.title} should be backed up first.")

            if action.risk == RiskLevel.HIGH:
                highest_risk = RiskLevel.HIGH
            elif action.risk == RiskLevel.MEDIUM and highest_risk != RiskLevel.HIGH:
                highest_risk = RiskLevel.MEDIUM

            if action.operation in CONFIRMATION_OPERATIONS:
                action.requires_confirmation = True
                reasons.append(f"{action.title} changes existing WordPress content or settings.")

            if self._publishes_content(action):
                action.requires_confirmation = True
                reasons.append(f"{action.title} would publish content to the live site.")

            app_blocker = self._app_role_blocker(action, app_role)
            if app_blocker:
                blockers.append(app_blocker)

            blocker = self._permission_blocker(action, roles)
            if blocker:
                blockers.append(blocker)

            capability_blocker = self._capability_blocker(action, capabilities)
            if capability_blocker:
                blockers.append(capability_blocker)

            if action.requires_confirmation:
                requires_confirmation = True

        if self.require_confirmation_for_major_changes and highest_risk in {
            RiskLevel.MEDIUM,
            RiskLevel.HIGH,
        }:
            requires_confirmation = True

        return SafetyDecision(
            requires_confirmation=requires_confirmation,
            requires_backup=requires_backup,
            highest_risk=highest_risk,
            reasons=sorted(set(reasons)),
            allowed=not blockers,
            blockers=sorted(set(blockers)),
        )

    def _publishes_content(self, action: PlannedAction) -> bool:
        if action.operation not in PUBLISHABLE_OPERATIONS:
            return False
        return str(action.payload.get("status", "")).lower() == "publish"


    def _app_role_blocker(self, action: PlannedAction, app_role: str | None) -> str | None:
        if not app_role:
            return None
        required = APP_ROLE_REQUIREMENTS.get(action.operation, "viewer")
        if APP_ROLE_RANK.get(app_role, -1) >= APP_ROLE_RANK.get(required, 99):
            return None
        return f"{action.title} requires WordPressGenius role '{required}', but current user is '{app_role}'."

    def _capability_blocker(self, action: PlannedAction, capabilities: set[str]) -> str | None:
        """Block when the user's WordPress capabilities lack the required one.

        Only applies when capabilities are known; otherwise the role check is the
        permission boundary. A user with `manage_options` (administrator) is
        treated as able to perform any capability-gated action.
        """

        if not capabilities:
            return None
        required = CAPABILITY_REQUIREMENTS.get(action.operation)
        if not required:
            return None
        if required in capabilities or "manage_options" in capabilities:
            return None
        return (
            f"{action.title} needs the WordPress capability '{required}', which the "
            "connected account does not have."
        )

    def _permission_blocker(self, action: PlannedAction, roles: set[str]) -> str | None:
        if not roles:
            return None
        required_roles = ROLE_REQUIREMENTS.get(action.operation, READ_ROLES)
        if roles.intersection(required_roles):
            return None
        allowed = ", ".join(sorted(required_roles))
        actual = ", ".join(sorted(roles)) or "unknown"
        return f"{action.title} requires one of [{allowed}], but connected user role is [{actual}]."
