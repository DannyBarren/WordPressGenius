"""Optional error tracking and consent-based usage analytics."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import sentry_sdk

from core.logging_config import redact
from core.security import AuthenticatedUser

LOGGER = logging.getLogger(__name__)


def init_sentry(
    dsn: str,
    *,
    environment: str,
    traces_sample_rate: float = 0.0,
) -> None:
    """Initialize Sentry when a DSN is configured."""

    if not dsn:
        return
    sentry_sdk.init(
        dsn=dsn,
        environment=environment,
        traces_sample_rate=traces_sample_rate,
        send_default_pii=False,
    )
    LOGGER.info("Sentry error tracking initialized for %s", environment)


def capture_exception(exc: BaseException, **context: Any) -> None:
    """Capture an exception in Sentry if configured, otherwise log it."""

    with sentry_sdk.push_scope() as scope:
        for key, value in context.items():
            scope.set_extra(key, redact(str(value)))
        sentry_sdk.capture_exception(exc)


class UsageAnalytics:
    """Consent-based local usage analytics stored as JSONL."""

    def __init__(self, path: Path, *, enabled: bool) -> None:
        self.path = path
        self.enabled = enabled
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(
        self,
        *,
        user: AuthenticatedUser | None,
        event_type: str,
        properties: dict[str, Any] | None = None,
        consent: bool = False,
    ) -> None:
        """Record an analytics event only when enabled and consent is present."""

        if not self.enabled or not consent:
            return
        event = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "event_type": event_type,
            "username": user.username if user else "anonymous",
            "role": user.role if user else "anonymous",
            "properties": _sanitize(properties or {}),
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True) + "\n")

    def summary(self, limit: int = 500) -> dict[str, Any]:
        """Return a small aggregate summary for the sidebar."""

        if not self.path.exists():
            return {"total_events": 0, "by_type": {}}
        rows = self.path.read_text(encoding="utf-8").splitlines()[-limit:]
        by_type: dict[str, int] = {}
        for row in rows:
            try:
                event = json.loads(row)
            except json.JSONDecodeError:
                continue
            event_type = str(event.get("event_type", "unknown"))
            by_type[event_type] = by_type.get(event_type, 0) + 1
        return {"total_events": sum(by_type.values()), "by_type": by_type}


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for key, item in value.items():
            lowered = key.lower()
            if any(secret in lowered for secret in ["password", "authorization", "token", "secret", "api_key"]):
                clean[key] = "[REDACTED]"
            else:
                clean[key] = _sanitize(item)
        return clean
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    if isinstance(value, str):
        return redact(value)
    return value
