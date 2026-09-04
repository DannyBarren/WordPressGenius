from __future__ import annotations

import json
import logging

from core.config import AppConfig
from core.logging_config import configure_logging, redact


def test_config_reads_docker_secret_file(monkeypatch, tmp_path) -> None:
    secret = tmp_path / "wp_password"
    secret.write_text("secret app password", encoding="utf-8")
    monkeypatch.setenv("WORDPRESS_APPLICATION_PASSWORD_FILE", str(secret))

    config = AppConfig()

    assert config.wordpress_application_password == "secret app password"


def test_redact_removes_secret_like_values() -> None:
    message = "application_password=abc123 token=def456 safe=value"

    redacted = redact(message)

    assert "abc123" not in redacted
    assert "def456" not in redacted
    assert "safe=value" in redacted


def test_json_file_logging_redacts_values(tmp_path) -> None:
    log_file = tmp_path / "app.log"
    configure_logging("INFO", log_file=log_file, json_logs=True, enable_file_logging=True)

    logging.getLogger("test").info("api_key=secret-value")

    row = json.loads(log_file.read_text(encoding="utf-8").splitlines()[0])
    assert row["level"] == "INFO"
    assert "secret-value" not in row["message"]
    assert "[REDACTED]" in row["message"]
