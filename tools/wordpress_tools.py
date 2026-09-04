"""High-level WordPress tools consumed by agents."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from core.models import ChangeOperation, ExecutionResult, PlannedAction
from tools.backups import BackupManager
from tools.plugin_framework import PluginFramework
from tools.plugin_manager import PluginManager
from tools.seo import suggest_seo
from tools.stripe_gateway import StripeTools
from tools.woocommerce import WooCommerceTools
from tools.wordpress_client import WordPressClient, WordPressClientError

LOGGER = logging.getLogger(__name__)


class WordPressTools:
    """Execute validated WordPress actions."""

    def __init__(self, client: WordPressClient, backup_manager: BackupManager) -> None:
        self.client = client
        self.backup_manager = backup_manager
        self.woocommerce = WooCommerceTools(client)
        self.stripe = StripeTools(client)
        self.plugins = PluginManager(client)
        self.framework = PluginFramework(client)

    def connection_summary(self) -> dict[str, Any]:
        return self.client.validate_connection()

    def create_backup(self, actions: list[PlannedAction]) -> Path | None:
        """Create a backup for the resources affected by *actions*."""

        return self.backup_manager.backup_for_actions(self.client, actions)

    def execute_many(self, actions: list[PlannedAction]) -> list[ExecutionResult]:
        """Execute actions sequentially and stop after the first failure.

        Stopping on failure avoids cascading partial changes when WordPress rejects
        an early operation in a multi-step plan.
        """

        results: list[ExecutionResult] = []
        for action in actions:
            result = self.execute(action)
            results.append(result)
            if not result.success:
                break
        return results

    def execute(self, action: PlannedAction) -> ExecutionResult:
        """Execute a single structured WordPress action."""
        LOGGER.info("Executing WordPress action: %s (%s)", action.title, action.operation.value)
        try:
            data = self._execute_action(action)
            LOGGER.info("Completed WordPress action: %s", action.title)
            return ExecutionResult(
                action_title=action.title,
                operation=action.operation,
                success=True,
                message=self._success_message(action, data),
                data=self._summarize_response(data),
            )
        except WordPressClientError as exc:
            LOGGER.warning("WordPress action failed: %s", exc)
            return ExecutionResult(
                action_title=action.title,
                operation=action.operation,
                success=False,
                message=str(exc),
            )
        except Exception as exc:  # noqa: BLE001 - surface friendly message to UI.
            LOGGER.exception("Unexpected WordPress action failure")
            return ExecutionResult(
                action_title=action.title,
                operation=action.operation,
                success=False,
                message=(
                    "Something unexpected happened while WordPressGenius was working. "
                    f"No additional actions were attempted for '{action.title}'. Details: {exc}"
                ),
            )

    def _execute_action(self, action: PlannedAction) -> dict[str, Any] | list[dict[str, Any]]:
        payload = action.payload
        if action.operation == ChangeOperation.CREATE_POST:
            seo = suggest_seo(payload.get("title", action.title), payload.get("content", ""))
            return self.client.create_post(
                payload.get("title", seo.title),
                payload.get("content", ""),
                status=payload.get("status", "draft"),
                excerpt=payload.get("excerpt") or seo.meta_description,
                categories=payload.get("categories"),
                tags=payload.get("tags"),
                featured_media=payload.get("featured_media"),
            )
        if action.operation == ChangeOperation.UPDATE_POST:
            post_id = self._require_id(payload, "post")
            fields = {key: value for key, value in payload.items() if key != "id"}
            return self.client.update_post(post_id, **fields)
        if action.operation == ChangeOperation.DELETE_POST:
            return self.client.delete_post(
                self._require_id(payload, "post"),
                force=bool(payload.get("force", False)),
            )
        if action.operation == ChangeOperation.CREATE_PAGE:
            return self.client.create_page(
                payload.get("title", action.title),
                payload.get("content", ""),
                status=payload.get("status", "draft"),
                parent=payload.get("parent"),
                featured_media=payload.get("featured_media"),
            )
        if action.operation == ChangeOperation.UPDATE_PAGE:
            page_id = self._require_id(payload, "page")
            fields = {key: value for key, value in payload.items() if key != "id"}
            return self.client.update_page(page_id, **fields)
        if action.operation == ChangeOperation.DELETE_PAGE:
            return self.client.delete_page(
                self._require_id(payload, "page"),
                force=bool(payload.get("force", False)),
            )
        if action.operation == ChangeOperation.UPLOAD_MEDIA:
            file_path = payload.get("file_path")
            if not file_path:
                raise WordPressClientError("I need an attached image file before uploading media.")
            return self.client.upload_media(
                Path(file_path),
                title=payload.get("title"),
                alt_text=payload.get("alt_text"),
                caption=payload.get("caption"),
            )
        if action.operation == ChangeOperation.UPDATE_SETTINGS:
            settings = payload.get("settings", {})
            if not settings:
                raise WordPressClientError(
                    "I need the exact WordPress settings to change before I can update them."
                )
            return self.client.update_settings(settings)
        if action.operation == ChangeOperation.UPDATE_PLUGIN:
            return self.client.update_plugin(payload["plugin_slug"], **payload.get("fields", {}))
        if action.operation == ChangeOperation.UPDATE_THEME:
            return self.client.update_theme(payload["stylesheet"], **payload.get("fields", {}))
        if action.operation == ChangeOperation.TROUBLESHOOT_FORM:
            return self._troubleshoot_form(payload.get("search", "contact form"))
        if action.operation == ChangeOperation.PLUGIN_TROUBLESHOOT:
            return self._plugin_troubleshoot(payload.get("plugin_slug"))
        if action.operation == ChangeOperation.ANALYTICS_SUMMARY:
            return self._analytics_summary()
        if action.operation == ChangeOperation.BULK_UPDATE_PRODUCTS:
            return self._bulk_update_products(payload)
        if action.operation == ChangeOperation.UNDO_LAST_CHANGE:
            return self.backup_manager.undo_latest(self.client)
        if action.operation == ChangeOperation.SEO_OPTIMIZE:
            return self._seo_optimize(payload)
        if action.operation == ChangeOperation.WOOCOMMERCE_READ:
            return self.woocommerce.read(payload.get("action", "overview"), payload)
        if action.operation == ChangeOperation.WOOCOMMERCE_WRITE:
            return self.woocommerce.write(payload.get("action", ""), payload)
        if action.operation == ChangeOperation.STRIPE_READ:
            return self.stripe.read(payload.get("action", "status"), payload)
        if action.operation == ChangeOperation.STRIPE_REFUND:
            return self.stripe.refund(payload)
        if action.operation == ChangeOperation.PLUGIN_INVENTORY:
            return self._plugin_inventory()
        if action.operation == ChangeOperation.SEO_PLUGIN_BULK:
            return self._seo_plugin_bulk(payload)
        if action.operation == ChangeOperation.PLUGIN_READ:
            return self.framework.read(payload.get("plugin", ""), payload.get("action", ""), payload)
        if action.operation == ChangeOperation.PLUGIN_ACTION:
            return self.framework.write(payload.get("plugin", ""), payload.get("action", ""), payload)
        if action.operation == ChangeOperation.READ_ONLY:
            return {
                "posts": self.client.get_posts(search=payload.get("search"), per_page=5),
                "pages": self.client.get_pages(search=payload.get("search"), per_page=5),
            }
        raise WordPressClientError(f"Unsupported operation: {action.operation}")

    def _troubleshoot_form(self, search: str) -> dict[str, Any]:
        pages = self.client.get_pages(search=search, per_page=10)
        plugins = self.client.list_plugins()
        form_plugins = [
            plugin
            for plugin in plugins
            if any(
                token in str(plugin).lower()
                for token in ["contact form", "gravity", "wpforms", "ninja forms", "formidable"]
            )
        ]
        return {
            "matching_pages": [self._summarize_response(page) for page in pages],
            "form_plugins": [self._summarize_response(plugin) for plugin in form_plugins],
            "recommended_next_steps": [
                "Confirm the form shortcode/block exists on the contact page.",
                "Verify the form plugin is active and up to date.",
                "Send a test submission after any change.",
            ],
        }


    def _plugin_troubleshoot(self, plugin_slug: str | None = None) -> dict[str, Any]:
        plugins = self.client.list_plugins()
        relevant = plugins
        if plugin_slug:
            relevant = [
                plugin
                for plugin in plugins
                if plugin_slug.lower() in str(plugin.get("plugin", plugin.get("slug", ""))).lower()
                or plugin_slug.lower() in str(plugin.get("name", "")).lower()
            ]
        inactive = [plugin for plugin in relevant if plugin.get("status") != "active"]
        likely_updates = [
            plugin
            for plugin in relevant
            if plugin.get("update") or plugin.get("version") != plugin.get("new_version", plugin.get("version"))
        ]
        return {
            "plugin_count": len(plugins),
            "matching_plugins": [self._summarize_response(plugin) for plugin in relevant[:10]],
            "inactive_matches": [self._summarize_response(plugin) for plugin in inactive[:10]],
            "possible_updates": [self._summarize_response(plugin) for plugin in likely_updates[:10]],
            "recommended_next_steps": [
                "Confirm the plugin is active if the feature is missing.",
                "Check plugin settings inside WordPress for required API keys or notification emails.",
                "Use a staging site before updating mission-critical plugins.",
            ],
        }

    def _analytics_summary(self) -> dict[str, Any]:
        plugins = self.client.list_plugins()
        analytics_plugins = [
            plugin
            for plugin in plugins
            if any(
                token in str(plugin).lower()
                for token in ["google site kit", "site kit", "analytics", "monsterinsights", "matomo"]
            )
        ]
        site_kit_status: dict[str, Any] | None = None
        try:
            site_kit_status = self.client.request_api("google-site-kit/v1/core/site/data/connection")
        except WordPressClientError as exc:
            site_kit_status = {"available": False, "message": str(exc)}
        return {
            "analytics_plugins": [self._summarize_response(plugin) for plugin in analytics_plugins],
            "site_kit_status": site_kit_status,
            "summary": (
                "Analytics plugin detected."
                if analytics_plugins
                else "No common analytics plugin was detected through the WordPress REST API."
            ),
            "recommended_next_steps": [
                "Install or connect Google Site Kit for richer in-app traffic summaries.",
                "Confirm the analytics plugin is active and connected to the correct property.",
                "Use the plugin dashboard for detailed visitor counts if its REST API is private.",
            ],
        }

    def _bulk_update_products(self, payload: dict[str, Any]) -> dict[str, Any]:
        percent = float(payload.get("percent", 0))
        if percent == 0:
            raise WordPressClientError("Tell me the percentage to change product prices by.")
        multiplier = 1 + (percent / 100)
        products = self.client.paginated_request_api(
            "wc/v3/products",
            params={"status": payload.get("status", "any")},
            per_page=int(payload.get("per_page", 100)),
            max_pages=int(payload.get("max_pages", 10)),
        )
        if not isinstance(products, list):
            raise WordPressClientError("WooCommerce products could not be read from the REST API.")
        if not products:
            raise WordPressClientError("No WooCommerce products were returned for the bulk update.")
        updated: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for product in products:
            product_id = product.get("id")
            regular_price = product.get("regular_price") or product.get("price")
            if not product_id or not regular_price:
                skipped.append({"id": product_id, "name": product.get("name"), "reason": "No regular price"})
                continue
            try:
                new_price = _format_price(float(regular_price) * multiplier)
            except (TypeError, ValueError):
                skipped.append({"id": product_id, "name": product.get("name"), "reason": "Price is not numeric"})
                continue
            result = self.client.request_api(
                f"wc/v3/products/{product_id}",
                method="PUT",
                json_body={"regular_price": new_price},
            )
            updated.append(self._summarize_response(result))
        return {
            "percent": percent,
            "updated_count": len(updated),
            "skipped_count": len(skipped),
            "updated": updated[:25],
            "skipped": skipped[:25],
        }

    def _seo_optimize(self, payload: dict[str, Any]) -> dict[str, Any]:
        suggestion = suggest_seo(payload.get("title", ""), payload.get("content", ""))
        target_type = payload.get("type", "post")
        target_id = payload.get("id")
        if not target_id:
            return {
                **suggestion.__dict__,
                "note": "No post or page ID was provided, so I only prepared SEO suggestions.",
            }
        fields = {
            "title": payload.get("title") or suggestion.title,
            "slug": suggestion.slug,
            "excerpt": suggestion.meta_description,
        }
        if target_type == "page":
            return self.client.update_page(int(target_id), **fields)
        return self.client.update_post(int(target_id), **fields)

    def _plugin_inventory(self) -> dict[str, Any]:
        inventory = self.plugins.inventory()
        try:
            catalog = self.framework.catalog()
        except WordPressClientError:
            catalog = []
        active = [item for item in catalog if item.get("active")]
        inventory["framework_capabilities"] = catalog
        if active:
            names = ", ".join(item["name"] for item in active)
            inventory["summary"] = f"{inventory.get('summary', '')} Framework-ready plugins active: {names}.".strip()
        return inventory

    def _seo_plugin_bulk(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Bulk-optimize SEO metadata for posts/pages/products.

        Always updates the core slug/excerpt (which works without any SEO plugin)
        and, when Yoast or Rank Math is active, also writes the plugin's meta keys.
        """

        targets = str(payload.get("targets", "posts")).lower()
        plugin = self.plugins.seo_plugin()
        items = self._collect_seo_targets(targets, payload)
        if not items:
            raise WordPressClientError(
                "I could not find any items to optimize. Provide ids or a search term."
            )
        updated: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for item in items[: int(payload.get("max", 25))]:
            item_id = item.get("id")
            title = _text(item.get("name") or _title_text(item.get("title")))
            content = _text(item.get("description") or _content_text(item.get("content")))
            suggestion = suggest_seo(title, content)
            try:
                if targets == "products":
                    self.client.request_api(
                        f"wc/v3/products/{item_id}",
                        method="PUT",
                        json_body=self._seo_product_body(suggestion, plugin),
                    )
                elif targets == "pages":
                    self.client.update_page(int(item_id), **self._seo_core_fields(suggestion, plugin))
                else:
                    self.client.update_post(int(item_id), **self._seo_core_fields(suggestion, plugin))
                updated.append({"id": item_id, "title": title, "slug": suggestion.slug})
            except WordPressClientError as exc:
                skipped.append({"id": item_id, "title": title, "reason": str(exc)})
        plugin_label = {"yoast": "Yoast SEO", "rankmath": "Rank Math"}.get(plugin or "", "core fields only")
        return {
            "summary": (
                f"Optimized SEO for {len(updated)} {targets} ({plugin_label}); {len(skipped)} skipped."
            ),
            "seo_plugin": plugin,
            "updated_count": len(updated),
            "skipped_count": len(skipped),
            "updated": updated[:25],
            "skipped": skipped[:10],
        }

    def _collect_seo_targets(self, targets: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
        ids = payload.get("ids") or []
        search = payload.get("search")
        if targets == "products":
            if ids:
                return [{"id": i} | self._safe_get_product(i) for i in ids]
            result = self.woocommerce.read("list_products", {"search": search, "per_page": payload.get("max", 25)})
            return result.get("products", [])
        if targets == "pages":
            if ids:
                return [self.client.get_page(int(i)) for i in ids]
            return self.client.get_pages(search=search, per_page=int(payload.get("max", 25)))
        if ids:
            return [self.client.get_post(int(i)) for i in ids]
        return self.client.get_posts(search=search, per_page=int(payload.get("max", 25)))

    def _safe_get_product(self, product_id: Any) -> dict[str, Any]:
        try:
            return self.client.request_api(f"wc/v3/products/{int(product_id)}")
        except WordPressClientError:
            return {}

    def _seo_core_fields(self, suggestion: Any, plugin: str | None) -> dict[str, Any]:
        fields: dict[str, Any] = {"slug": suggestion.slug, "excerpt": suggestion.meta_description}
        meta = self._seo_meta(suggestion, plugin)
        if meta:
            fields["meta"] = meta
        return fields

    def _seo_product_body(self, suggestion: Any, plugin: str | None) -> dict[str, Any]:
        body: dict[str, Any] = {"slug": suggestion.slug}
        meta = self._seo_meta(suggestion, plugin)
        if meta:
            body["meta_data"] = [{"key": key, "value": value} for key, value in meta.items()]
        return body

    def _seo_meta(self, suggestion: Any, plugin: str | None) -> dict[str, str]:
        keyword = suggestion.focus_keywords[0] if suggestion.focus_keywords else ""
        if plugin == "yoast":
            return {
                "_yoast_wpseo_title": suggestion.title,
                "_yoast_wpseo_metadesc": suggestion.meta_description,
                "_yoast_wpseo_focuskw": keyword,
            }
        if plugin == "rankmath":
            return {
                "rank_math_title": suggestion.title,
                "rank_math_description": suggestion.meta_description,
                "rank_math_focus_keyword": keyword,
            }
        return {}

    def _require_id(self, payload: dict[str, Any], resource_name: str) -> int:
        value = payload.get("id")
        if not value:
            raise WordPressClientError(
                f"I need the {resource_name} ID before I can safely change it."
            )
        return int(value)

    def _success_message(self, action: PlannedAction, data: Any) -> str:
        if action.operation == ChangeOperation.READ_ONLY:
            return "I checked WordPress and gathered context."
        if action.operation == ChangeOperation.ANALYTICS_SUMMARY:
            return "I checked available analytics plugin connections."
        if action.operation == ChangeOperation.PLUGIN_TROUBLESHOOT:
            return "I reviewed plugin status and troubleshooting signals."
        if action.operation == ChangeOperation.UNDO_LAST_CHANGE:
            return "I restored the latest supported backup snapshot."
        if action.operation == ChangeOperation.BULK_UPDATE_PRODUCTS:
            count = data.get("updated_count", 0) if isinstance(data, dict) else 0
            return f"Bulk product update completed for {count} product(s)."
        if action.operation in {
            ChangeOperation.WOOCOMMERCE_READ,
            ChangeOperation.WOOCOMMERCE_WRITE,
            ChangeOperation.STRIPE_READ,
            ChangeOperation.STRIPE_REFUND,
            ChangeOperation.PLUGIN_INVENTORY,
            ChangeOperation.SEO_PLUGIN_BULK,
            ChangeOperation.PLUGIN_READ,
            ChangeOperation.PLUGIN_ACTION,
        } and isinstance(data, dict) and data.get("summary"):
            return str(data["summary"])
        if action.operation == ChangeOperation.SEO_OPTIMIZE and not action.payload.get("id"):
            return "I prepared SEO recommendations but did not change WordPress."
        summary = self._summarize_response(data)
        status = summary.get("status")
        link = summary.get("link")
        details = []
        if status:
            details.append(f"status: {status}")
        if link:
            details.append(f"link: {link}")
        suffix = f" ({'; '.join(details)})" if details else ""
        return f"Completed {action.title}{suffix}"

    def _summarize_response(self, data: Any) -> dict[str, Any]:
        if isinstance(data, list):
            return {"items": [self._summarize_response(item) for item in data[:10]]}
        if not isinstance(data, dict):
            return {"value": data}
        return {
            key: data.get(key)
            for key in [
                "id",
                "date",
                "slug",
                "status",
                "link",
                "permalink",
                "title",
                "name",
                "sku",
                "price",
                "stock_quantity",
                "stock_status",
                "total",
                "currency",
                "number",
                "payment_method_title",
                "description",
                "plugin",
                "version",
                "count",
                "created_count",
                "updated_count",
                "skipped_count",
                "plugin_count",
                "summary",
                "active",
                "seo_plugin",
                "products",
                "orders",
                "transactions",
                "customers",
                "variations",
                "categories",
                "tags",
                "low_stock",
                "gateways",
                "refund",
                "templates",
                "forms",
                "entries",
                "field_groups",
                "schema_types",
                "top_sellers",
                "active_plugins",
                "available",
                "started",
                "cleared",
                "by_type",
                "detected_capabilities",
                "framework_capabilities",
                "available_namespaces",
                "recommended_next_steps",
                "backup_path",
                "restored",
                "unsupported",
            ]
            if key in data
        }


def _format_price(value: float) -> str:
    return f"{value:.2f}"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _title_text(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("rendered") or value.get("raw") or "")
    return str(value or "")


def _content_text(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("rendered") or value.get("raw") or "")
    return str(value or "")
