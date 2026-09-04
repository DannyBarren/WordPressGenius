"""Companion FastAPI health endpoint for production deployments."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from core.config import get_config
from core.logging_config import configure_logging
from core.observability import init_sentry


config = get_config()
configure_logging(
    config.log_level,
    log_file=config.log_file,
    json_logs=config.json_logs,
    enable_file_logging=config.enable_file_logging,
)
init_sentry(
    config.sentry_dsn,
    environment=config.app_env,
    traces_sample_rate=config.sentry_traces_sample_rate,
)

app = FastAPI(title="WordPressGenius Health", version="0.1.0")


@app.get("/health")
def health() -> dict[str, Any]:
    """Return liveness and basic persistence-path checks."""

    checks = {_label(path): _path_check(path) for path in config.data_paths}
    healthy = all(check["ok"] for check in checks.values())
    return {
        "status": "ok" if healthy else "degraded",
        "app": config.app_name,
        "environment": config.app_env,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "checks": checks,
    }


@app.get("/ready")
def ready() -> dict[str, Any]:
    """Return readiness for reverse proxies and container health checks."""

    result = health()
    result["ready"] = result["status"] == "ok"
    return result


def _label(path: Path) -> str:
    return str(path).replace("/", "_").strip("_") or "root"


def _path_check(path: Path) -> dict[str, Any]:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".healthcheck"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return {"ok": True, "path": str(path)}
    except OSError as exc:
        return {"ok": False, "path": str(path), "error": str(exc)}
