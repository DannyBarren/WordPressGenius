"""Shared domain models for WordPressGenius."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, HttpUrl


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ChangeOperation(str, Enum):
    CREATE_POST = "create_post"
    UPDATE_POST = "update_post"
    DELETE_POST = "delete_post"
    CREATE_PAGE = "create_page"
    UPDATE_PAGE = "update_page"
    DELETE_PAGE = "delete_page"
    UPLOAD_MEDIA = "upload_media"
    UPDATE_SETTINGS = "update_settings"
    UPDATE_THEME = "update_theme"
    UPDATE_PLUGIN = "update_plugin"
    TROUBLESHOOT_FORM = "troubleshoot_form"
    SEO_OPTIMIZE = "seo_optimize"
    BULK_UPDATE_PRODUCTS = "bulk_update_products"
    PLUGIN_TROUBLESHOOT = "plugin_troubleshoot"
    ANALYTICS_SUMMARY = "analytics_summary"
    UNDO_LAST_CHANGE = "undo_last_change"
    READ_ONLY = "read_only"
    # WooCommerce, Stripe, and a scalable plugin framework. Read operations are
    # low-risk; write operations are confirmation- and backup-gated. Each carries
    # a payload["action"] sub-command so new capabilities need no new enum members.
    WOOCOMMERCE_READ = "woocommerce_read"
    WOOCOMMERCE_WRITE = "woocommerce_write"
    STRIPE_READ = "stripe_read"
    STRIPE_REFUND = "stripe_refund"
    PLUGIN_INVENTORY = "plugin_inventory"
    SEO_PLUGIN_BULK = "seo_plugin_bulk"
    # Generic, extensible plugin framework operations. payload carries
    # {plugin, action, ...}; the PluginFramework routes to the right handler.
    # PLUGIN_READ is always read-only; PLUGIN_ACTION is approval- and backup-gated.
    PLUGIN_READ = "plugin_read"
    PLUGIN_ACTION = "plugin_action"


class WordPressCredentials(BaseModel):
    """User-provided WordPress connection details."""

    site_url: HttpUrl
    username: str = Field(min_length=1)
    application_password: str = Field(min_length=1)


class PlannedAction(BaseModel):
    """A single action proposed by the Planner agent."""

    operation: ChangeOperation
    title: str
    description: str
    payload: dict[str, Any] = Field(default_factory=dict)
    risk: RiskLevel = RiskLevel.LOW
    requires_confirmation: bool = False


class ExecutionResult(BaseModel):
    """Result of a WordPress tool operation."""

    action_title: str
    operation: ChangeOperation
    success: bool
    message: str
    data: dict[str, Any] = Field(default_factory=dict)


class ActivityEvent(BaseModel):
    """Persisted activity log entry."""

    event_type: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class AgentStatus(BaseModel):
    """Real-time status update emitted by agents."""

    agent: Literal[
        "Planner",
        "Researcher",
        "Content Writer",
        "WordPress Executor",
        "Reviewer",
        "Communicator",
    ]
    status: str
    detail: str = ""
