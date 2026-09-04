from __future__ import annotations

from pathlib import Path

import pytest

from core.config import AppConfig
from core.memory import ActivityLog, SiteMemory
from core.safety import SafetyLayer
from core.security import generate_fernet_key
from tools.backups import BackupManager
from tools.wordpress_client import WordPressClient
from tools.wordpress_tools import WordPressTools


@pytest.fixture
def wp_base_url() -> str:
    return "https://example.test"


@pytest.fixture
def wp_client(wp_base_url: str) -> WordPressClient:
    return WordPressClient(
        site_url=wp_base_url,
        username="admin",
        application_password="app password",
        max_retries=0,
    )


@pytest.fixture
def backup_manager(tmp_path: Path) -> BackupManager:
    return BackupManager(tmp_path / "backups")


@pytest.fixture
def wp_tools(wp_client: WordPressClient, backup_manager: BackupManager) -> WordPressTools:
    return WordPressTools(wp_client, backup_manager)


@pytest.fixture
def activity_log(tmp_path: Path) -> ActivityLog:
    return ActivityLog(tmp_path / "activity.jsonl")


@pytest.fixture
def site_memory(tmp_path: Path) -> SiteMemory:
    return SiteMemory(tmp_path / "memory.json")


@pytest.fixture
def safety_layer() -> SafetyLayer:
    return SafetyLayer(require_confirmation_for_major_changes=True)


@pytest.fixture
def test_config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        wordpress_site_url="https://example.test",
        wordpress_username="admin",
        wordpress_application_password="app password",
        backup_dir=tmp_path / "backups",
        activity_log_path=tmp_path / "activity.jsonl",
        memory_path=tmp_path / "memory.json",
        upload_dir=tmp_path / "uploads",
        max_requests_per_minute=100,
        wordpress_max_retries=0,
        wordpress_retry_backoff_seconds=0,
        auth_enabled=False,
        credential_key=generate_fernet_key(),
        credential_vault_path=tmp_path / "credentials.enc",
        llm_settings_path=tmp_path / "llm_settings.enc",
        sites_vault_path=tmp_path / "sites.enc",
    )
