"""Backup and undo helpers for WordPress resources before risky changes."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.models import ChangeOperation, PlannedAction
from tools.wordpress_client import WordPressClient, WordPressClientError


class BackupManager:
    """Create JSON backups, rotate old files, and restore supported snapshots."""

    def __init__(self, backup_dir: Path, *, keep_last: int = 25) -> None:
        self.backup_dir = backup_dir
        self.keep_last = max(1, keep_last)
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self.latest_path = self.backup_dir / "latest_backup.txt"

    def backup_for_actions(
        self, client: WordPressClient, actions: list[PlannedAction]
    ) -> Path | None:
        resources: list[dict[str, Any]] = []
        for action in actions:
            payload = action.payload
            try:
                resource = self._read_resource(client, action)
                if resource is not None:
                    resources.append(
                        {
                            "operation": action.operation.value,
                            "action_title": action.title,
                            "payload": _safe_payload(payload),
                            "resource": resource,
                        }
                    )
            except Exception as exc:  # noqa: BLE001 - backup errors should be recorded.
                resources.append(
                    {
                        "operation": action.operation.value,
                        "action_title": action.title,
                        "payload": _safe_payload(payload),
                        "backup_error": str(exc),
                    }
                )

        if not resources:
            return None

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = self.backup_dir / f"wordpress_backup_{stamp}.json"
        payload = {
            "schema_version": 2,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "resources": resources,
        }
        path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        self.latest_path.write_text(str(path), encoding="utf-8")
        self.rotate_backups()
        return path


    def rotate_backups(self) -> list[Path]:
        """Delete old backup files, preserving the latest `keep_last` snapshots."""

        backups = sorted(self.backup_dir.glob("wordpress_backup_*.json"))
        if len(backups) <= self.keep_last:
            return []
        latest = self.latest_backup()
        removable = backups[: max(0, len(backups) - self.keep_last)]
        deleted: list[Path] = []
        for path in removable:
            if latest and path == latest:
                continue
            try:
                path.unlink()
                deleted.append(path)
            except OSError:
                continue
        return deleted

    def latest_backup(self) -> Path | None:
        if self.latest_path.exists():
            candidate = Path(self.latest_path.read_text(encoding="utf-8").strip())
            if candidate.exists():
                return candidate
        backups = sorted(self.backup_dir.glob("wordpress_backup_*.json"))
        return backups[-1] if backups else None

    def undo_latest(self, client: WordPressClient) -> dict[str, Any]:
        backup = self.latest_backup()
        if not backup:
            raise WordPressClientError("There is no backup available to undo.")
        data = json.loads(backup.read_text(encoding="utf-8"))
        restored: list[dict[str, Any]] = []
        unsupported: list[str] = []
        for entry in reversed(data.get("resources", [])):
            operation = entry.get("operation")
            resource = entry.get("resource")
            try:
                restored_entry = self._restore_entry(client, operation, resource, entry)
            except Exception as exc:  # noqa: BLE001 - record restore failures, keep going.
                unsupported.append(f"{operation or 'unknown'} ({exc})")
                continue
            if restored_entry is None:
                unsupported.append(operation or "unknown")
            elif isinstance(restored_entry, list):
                restored.extend(restored_entry)
            else:
                restored.append(restored_entry)
        return {
            "backup_path": str(backup),
            "restored": restored,
            "unsupported": unsupported,
        }

    def _restore_entry(
        self,
        client: WordPressClient,
        operation: str | None,
        resource: Any,
        entry: dict[str, Any],
    ) -> dict[str, Any] | list[dict[str, Any]] | None:
        """Restore a single backed-up resource based on its operation type."""

        if operation in {
            ChangeOperation.UPDATE_POST.value,
            ChangeOperation.DELETE_POST.value,
            ChangeOperation.SEO_OPTIMIZE.value,
        } and isinstance(resource, dict):
            return self._restore_post(client, resource)
        if operation in {
            ChangeOperation.UPDATE_PAGE.value,
            ChangeOperation.DELETE_PAGE.value,
        } and isinstance(resource, dict):
            return self._restore_page(client, resource)
        if operation == ChangeOperation.UPDATE_SETTINGS.value and isinstance(resource, dict):
            return client.update_settings(_settings_fields(resource))
        if operation in {
            ChangeOperation.WOOCOMMERCE_WRITE.value,
            ChangeOperation.BULK_UPDATE_PRODUCTS.value,
        }:
            return self._restore_woocommerce(client, resource, entry.get("payload", {}))
        if operation == ChangeOperation.UPDATE_PLUGIN.value:
            return self._restore_plugin(client, resource, entry.get("payload", {}))
        return None

    def _restore_woocommerce(
        self, client: WordPressClient, resource: Any, payload: dict[str, Any]
    ) -> dict[str, Any] | list[dict[str, Any]] | None:
        sub_action = str(payload.get("action", "")).lower()
        if sub_action == "update_order_status" and isinstance(resource, dict) and resource.get("id"):
            return client.request_api(
                f"wc/v3/orders/{int(resource['id'])}",
                method="PUT",
                json_body={"status": resource.get("status")},
            )
        if isinstance(resource, dict) and resource.get("id"):
            return client.request_api(
                f"wc/v3/products/{int(resource['id'])}",
                method="PUT",
                json_body=_product_fields(resource),
            )
        if isinstance(resource, list):
            restored: list[dict[str, Any]] = []
            for product in resource[:50]:
                if isinstance(product, dict) and product.get("id"):
                    restored.append(
                        client.request_api(
                            f"wc/v3/products/{int(product['id'])}",
                            method="PUT",
                            json_body=_product_fields(product),
                        )
                    )
            return restored or None
        return None

    def _restore_plugin(
        self, client: WordPressClient, resource: Any, payload: dict[str, Any]
    ) -> dict[str, Any] | None:
        slug = payload.get("plugin_slug")
        plugins = resource if isinstance(resource, list) else [resource]
        for plugin in plugins:
            if not isinstance(plugin, dict):
                continue
            identifier = str(plugin.get("plugin", ""))
            if slug and slug.lower() not in identifier.lower():
                continue
            return client.update_plugin(identifier, status=plugin.get("status", "inactive"))
        return None

    def _read_resource(
        self, client: WordPressClient, action: PlannedAction
    ) -> dict[str, Any] | list[dict[str, Any]] | None:
        payload = action.payload
        if action.operation in {ChangeOperation.UPDATE_POST, ChangeOperation.DELETE_POST} and payload.get("id"):
            return client.get_post(int(payload["id"]))
        if action.operation in {ChangeOperation.UPDATE_PAGE, ChangeOperation.DELETE_PAGE} and payload.get("id"):
            return client.get_page(int(payload["id"]))
        if action.operation == ChangeOperation.SEO_OPTIMIZE and payload.get("id"):
            return (
                client.get_page(int(payload["id"]))
                if payload.get("type") == "page"
                else client.get_post(int(payload["id"]))
            )
        if action.operation == ChangeOperation.UPDATE_SETTINGS:
            return client.get_settings()
        if action.operation == ChangeOperation.UPDATE_PLUGIN:
            return client.list_plugins()
        if action.operation == ChangeOperation.UPDATE_THEME:
            return client.list_themes()
        if action.operation == ChangeOperation.BULK_UPDATE_PRODUCTS:
            return client.paginated_request_api("wc/v3/products", per_page=100, max_pages=10)
        if action.operation == ChangeOperation.WOOCOMMERCE_WRITE:
            return self._read_woocommerce_resource(client, payload)
        if action.operation == ChangeOperation.STRIPE_REFUND:
            order_id = payload.get("order_id") or payload.get("id")
            return client.request_api(f"wc/v3/orders/{int(order_id)}") if order_id else None
        if action.operation == ChangeOperation.SEO_PLUGIN_BULK:
            return self._read_seo_targets(client, payload)
        return None

    def _read_woocommerce_resource(
        self, client: WordPressClient, payload: dict[str, Any]
    ) -> dict[str, Any] | list[dict[str, Any]] | None:
        sub_action = str(payload.get("action", "")).lower()
        resource_id = payload.get("id")
        if sub_action in {"update_product", "delete_product", "update_stock"} and resource_id:
            return client.request_api(f"wc/v3/products/{int(resource_id)}")
        if sub_action == "update_order_status" and resource_id:
            return client.request_api(f"wc/v3/orders/{int(resource_id)}")
        if sub_action.startswith("bulk_"):
            return client.paginated_request_api("wc/v3/products", per_page=100, max_pages=10)
        return None

    def _read_seo_targets(
        self, client: WordPressClient, payload: dict[str, Any]
    ) -> list[dict[str, Any]] | None:
        ids = payload.get("ids") or []
        target = "pages" if payload.get("targets") == "pages" else "posts"
        wc = payload.get("targets") == "products"
        snapshots: list[dict[str, Any]] = []
        for resource_id in list(ids)[:25]:
            try:
                if wc:
                    snapshots.append(client.request_api(f"wc/v3/products/{int(resource_id)}"))
                elif target == "pages":
                    snapshots.append(client.get_page(int(resource_id)))
                else:
                    snapshots.append(client.get_post(int(resource_id)))
            except Exception:  # noqa: BLE001 - snapshot is best-effort.
                continue
        return snapshots or None

    def _restore_post(self, client: WordPressClient, resource: dict[str, Any]) -> dict[str, Any]:
        post_id = int(resource["id"])
        return client.update_post(post_id, **_content_fields(resource))

    def _restore_page(self, client: WordPressClient, resource: dict[str, Any]) -> dict[str, Any]:
        page_id = int(resource["id"])
        return client.update_page(page_id, **_content_fields(resource))


def _safe_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in payload.items()
        if "password" not in key.lower() and "token" not in key.lower() and "secret" not in key.lower()
    }


def _content_fields(resource: dict[str, Any]) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for key in ["title", "content", "excerpt", "slug", "status"]:
        if key in resource:
            fields[key] = resource[key]
    if "featured_media" in resource:
        fields["featured_media"] = resource["featured_media"]
    return fields


def _settings_fields(resource: dict[str, Any]) -> dict[str, Any]:
    readonly = {"_links"}
    return {key: value for key, value in resource.items() if key not in readonly}


def _product_fields(resource: dict[str, Any]) -> dict[str, Any]:
    """Fields needed to roll a WooCommerce product back to a prior snapshot."""

    fields: dict[str, Any] = {}
    for key in ["name", "regular_price", "sale_price", "status", "stock_status", "description"]:
        if resource.get(key) is not None:
            fields[key] = resource[key]
    if "manage_stock" in resource:
        fields["manage_stock"] = resource["manage_stock"]
    if resource.get("stock_quantity") is not None:
        fields["stock_quantity"] = resource["stock_quantity"]
    return fields
