"""Application service for running WordPressGenius agent workflows."""

from __future__ import annotations

import logging
from uuid import uuid4
from dataclasses import dataclass

from agents.crew import AgentState, build_wordpress_crew
from core.config import AppConfig
from core.llm import build_llm_router
from core.settings_store import resolve_active_settings
from core.memory import ActivityLog, SiteMemory
from core.models import AgentStatus, WordPressCredentials
from core.observability import capture_exception
from core.rate_limit import SlidingWindowRateLimiter
from core.security import AuthenticatedUser, PromptGuard, SecurityAuditLog
from core.safety import SafetyLayer
from tools.backups import BackupManager
from tools.wordpress_client import WordPressClient, WordPressClientError
from tools.wordpress_tools import WordPressTools


LOGGER = logging.getLogger(__name__)


@dataclass
class OrchestrationResult:
    final_response: str
    statuses: list[AgentStatus]
    requires_confirmation: bool
    confirmation_summary: str
    raw_state: AgentState


class WordPressGeniusOrchestrator:
    """Coordinates configuration, WordPress tools, and the LangGraph crew."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.activity_log = ActivityLog(config.activity_log_path)
        self.site_memory = SiteMemory(config.memory_path)
        self.audit_log = SecurityAuditLog(config.activity_log_path.parent / "audit_log.jsonl")
        self.prompt_guard = PromptGuard(config.max_prompt_length)
        self.rate_limiter = SlidingWindowRateLimiter(config.max_requests_per_minute, window_seconds=60.0)
        self.safety_layer = SafetyLayer(config.require_confirmation_for_major_changes)
        settings = resolve_active_settings(config)
        self.router = build_llm_router(settings, config)
        self.agentic = settings.agentic

    def run(
        self,
        user_request: str,
        *,
        credentials: WordPressCredentials | None,
        approved: bool = False,
        user: AuthenticatedUser | None = None,
        client_ip: str | None = None,
    ) -> OrchestrationResult:
        rate_key = (user.username if user else None) or client_ip or "anonymous"
        rate = self.rate_limiter.check(rate_key)
        if not rate.allowed:
            wait = int(rate.retry_after_seconds) + 1
            self.audit_log.append(
                user=user,
                event_type="rate_limited",
                message="Blocked request that exceeded the per-minute rate limit.",
                details={"client_ip": client_ip, "retry_after_seconds": wait},
            )
            status = AgentStatus(
                agent="Planner",
                status="blocked",
                detail="Rate limit reached. Please slow down.",
            )
            return OrchestrationResult(
                final_response=(
                    "You're sending requests too quickly. "
                    f"Please wait about {wait} second(s) and try again. "
                    "Nothing was changed in WordPress."
                ),
                statuses=[status],
                requires_confirmation=False,
                confirmation_summary="",
                raw_state={"user_request": user_request, "statuses": [status]},
            )

        validation = self.prompt_guard.validate(user_request)
        if not validation.allowed:
            self.audit_log.append(
                user=user,
                event_type="prompt_blocked",
                message="Blocked unsafe or invalid prompt.",
                details={"warnings": validation.warnings, "client_ip": client_ip},
            )
            status = AgentStatus(
                agent="Planner",
                status="blocked",
                detail="The request was blocked by input safety checks.",
            )
            return OrchestrationResult(
                final_response="I cannot process that request because it appears unsafe or invalid.",
                statuses=[status],
                requires_confirmation=False,
                confirmation_summary="",
                raw_state={"user_request": user_request, "statuses": [status]},
            )

        user_request = validation.sanitized_text
        if validation.warnings:
            self.audit_log.append(
                user=user,
                event_type="prompt_warning",
                message="Prompt allowed with safety warnings.",
                details={"warnings": validation.warnings, "client_ip": client_ip},
            )
        run_id = uuid4().hex
        self.activity_log.append(
            "run_started",
            "Started WordPressGenius workflow.",
            {"run_id": run_id, "approved": approved, "username": user.username if user else "anonymous"},
        )
        self.audit_log.append(
            user=user,
            event_type="run_started",
            message="Started WordPressGenius workflow.",
            details={"run_id": run_id, "approved": approved, "client_ip": client_ip},
        )
        tools = self._build_tools(credentials) if credentials else None
        crew = build_wordpress_crew(
            tools=tools,
            safety_layer=self.safety_layer,
            activity_log=self.activity_log,
            site_memory=self.site_memory,
            router=self.router,
            agentic=self.agentic,
            web_search_enabled=self.config.web_search_enabled,
            web_search_max_results=self.config.web_search_max_results,
        )
        memory_context = self.site_memory.snapshot()
        memory_context["relevant"] = self.site_memory.search(user_request, limit=5)
        initial_state: AgentState = {
            "user_request": user_request,
            "approved": approved,
            "statuses": [],
            "memory_context": memory_context,
            "run_id": run_id,
            "app_user": user.username if user else "anonymous",
            "app_role": user.role if user else "admin",
            "client_ip": client_ip or "unknown",
        }
        try:
            state = crew.invoke(initial_state)
        except Exception as exc:  # noqa: BLE001 - convert workflow errors for the UI.
            LOGGER.exception("Agent workflow failed")
            capture_exception(exc, run_id=run_id, username=user.username if user else "anonymous")
            self.activity_log.append(
                "workflow_error",
                "The agent workflow failed before completing.",
                {"error": str(exc)},
            )
            status = AgentStatus(
                agent="Communicator",
                status="error",
                detail="I could not complete the workflow. Please review the error and try again.",
            )
            self.audit_log.append(
                user=user,
                event_type="run_failed",
                message="WordPressGenius workflow failed.",
                details={"run_id": run_id, "error": str(exc), "client_ip": client_ip},
            )
            return OrchestrationResult(
                final_response=(
                    "I ran into a problem before making changes. "
                    f"Nothing was changed in WordPress.\n\nDetails: {exc}"
                ),
                statuses=[status],
                requires_confirmation=False,
                confirmation_summary="",
                raw_state=initial_state,
            )
        self.activity_log.append(
            "run_completed",
            "Completed WordPressGenius workflow.",
            {
                "run_id": run_id,
                "requires_confirmation": bool(state.get("requires_confirmation")),
                "status_count": len(state.get("statuses", [])),
            },
        )
        self.audit_log.append(
            user=user,
            event_type="run_completed",
            message="Completed WordPressGenius workflow.",
            details={
                "run_id": run_id,
                "requires_confirmation": bool(state.get("requires_confirmation")),
                "client_ip": client_ip,
            },
        )
        return OrchestrationResult(
            final_response=state.get("final_response", ""),
            statuses=state.get("statuses", []),
            requires_confirmation=bool(state.get("requires_confirmation")),
            confirmation_summary=state.get("confirmation_summary", ""),
            raw_state=state,
        )

    def test_connection(self, credentials: WordPressCredentials) -> dict[str, object]:
        """Validate WordPress credentials for the sidebar connection checker."""

        try:
            summary = self._build_tools(credentials).connection_summary()
        except WordPressClientError as exc:
            LOGGER.warning("WordPress connection test failed: %s", exc)
            return {"ok": False, "message": str(exc)}
        except Exception as exc:  # noqa: BLE001 - show a safe generic failure.
            LOGGER.exception("Unexpected WordPress connection test failure")
            return {
                "ok": False,
                "message": (
                    "WordPressGenius could not test the connection. "
                    f"Details: {exc}"
                ),
            }
        user = summary.get("user", {})
        return {
            "ok": True,
            "site_name": summary.get("site_name", "WordPress site"),
            "site_url": summary.get("site_url", str(credentials.site_url)),
            "user_name": user.get("name") if isinstance(user, dict) else None,
        }

    def _build_tools(self, credentials: WordPressCredentials) -> WordPressTools:
        client = WordPressClient(
            site_url=str(credentials.site_url),
            username=credentials.username,
            application_password=credentials.application_password,
            max_retries=self.config.wordpress_max_retries,
            retry_backoff_seconds=self.config.wordpress_retry_backoff_seconds,
            cache_ttl_seconds=self.config.wp_cache_ttl_seconds,
        )
        backups = BackupManager(self.config.backup_dir, keep_last=self.config.backup_keep_last)
        return WordPressTools(client=client, backup_manager=backups)
