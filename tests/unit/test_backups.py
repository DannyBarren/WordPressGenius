from __future__ import annotations

import json

import responses

from core.models import ChangeOperation, PlannedAction, RiskLevel
from tests.helpers import post_payload


@responses.activate
def test_backup_for_update_post_records_resource(
    wp_base_url: str, wp_client, backup_manager
) -> None:
    responses.add(
        responses.GET,
        f"{wp_base_url}/wp-json/wp/v2/posts/123",
        json=post_payload(123),
        status=200,
    )
    action = PlannedAction(
        operation=ChangeOperation.UPDATE_POST,
        title="Update post",
        description="Update content.",
        payload={"id": 123, "content": "New"},
        risk=RiskLevel.MEDIUM,
    )

    path = backup_manager.backup_for_actions(wp_client, [action])

    assert path is not None
    data = json.loads(path.read_text())
    assert data["schema_version"] == 2
    assert data["resources"][0]["resource"]["id"] == 123
    assert backup_manager.latest_backup() == path


@responses.activate
def test_undo_latest_restores_supported_post_backup(
    wp_base_url: str, wp_client, backup_manager
) -> None:
    responses.add(
        responses.GET,
        f"{wp_base_url}/wp-json/wp/v2/posts/123",
        json=post_payload(123, status="publish"),
        status=200,
    )
    action = PlannedAction(
        operation=ChangeOperation.UPDATE_POST,
        title="Update post",
        description="Update content.",
        payload={"id": 123, "content": "New"},
        risk=RiskLevel.MEDIUM,
    )
    backup_manager.backup_for_actions(wp_client, [action])
    responses.add(
        responses.POST,
        f"{wp_base_url}/wp-json/wp/v2/posts/123",
        json=post_payload(123, status="publish"),
        status=200,
    )

    result = backup_manager.undo_latest(wp_client)

    assert result["restored"][0]["id"] == 123
    assert result["unsupported"] == []


def test_undo_without_backup_raises_friendly_error(wp_client, backup_manager) -> None:
    from tools.wordpress_client import WordPressClientError

    try:
        backup_manager.undo_latest(wp_client)
    except WordPressClientError as exc:
        assert "no backup available" in str(exc).lower()
    else:
        raise AssertionError("Expected WordPressClientError")


def test_backup_rotation_keeps_latest_snapshots(tmp_path) -> None:
    from tools.backups import BackupManager

    manager = BackupManager(tmp_path / "backups", keep_last=2)
    for index in range(4):
        path = manager.backup_dir / f"wordpress_backup_20260528T15000{index}Z.json"
        path.write_text("{}", encoding="utf-8")
    latest = manager.backup_dir / "wordpress_backup_20260528T150003Z.json"
    manager.latest_path.write_text(str(latest), encoding="utf-8")

    deleted = manager.rotate_backups()

    remaining = sorted(path.name for path in manager.backup_dir.glob("wordpress_backup_*.json"))
    assert len(deleted) == 2
    assert remaining == [
        "wordpress_backup_20260528T150002Z.json",
        "wordpress_backup_20260528T150003Z.json",
    ]
