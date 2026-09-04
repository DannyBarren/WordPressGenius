"""Advanced SEO handler for Yoast and Rank Math: audits, bulk meta, and schema."""

from __future__ import annotations

from typing import Any

from tools.plugin_framework import PluginHandler, find_plugin, graceful_get
from tools.seo import suggest_seo
from tools.wordpress_client import WordPressClientError

_YOAST_FRAGMENTS = ("wordpress-seo", "yoast")
_RANKMATH_FRAGMENTS = ("seo-by-rank-math", "rank-math", "rankmath")


class SeoAdvancedHandler(PluginHandler):
    key = "seo"
    name = "Yoast / Rank Math SEO"
    category = "seo"
    slug_fragments = _YOAST_FRAGMENTS + _RANKMATH_FRAGMENTS
    namespaces = ("yoast/v1", "rankmath/v1")
    read_actions = {
        "audit": "Audit posts/products for missing SEO titles or meta descriptions.",
        "schema_summary": "Summarize the active SEO plugin and schema capabilities.",
    }
    write_actions = {
        "bulk_optimize": "Bulk-write SEO titles, meta descriptions, and schema type (payload: targets, ids/search, schema_type).",
    }

    def read(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        if action == "schema_summary":
            return self._schema_summary()
        return self._audit(payload)

    def write(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._bulk_optimize(payload)

    # -- detection -------------------------------------------------------
    def _seo_plugin(self) -> str | None:
        try:
            plugins = self.client.list_plugins()
        except WordPressClientError:
            plugins = []
        plugins = plugins if isinstance(plugins, list) else []
        if find_plugin(plugins, _YOAST_FRAGMENTS):
            return "yoast"
        if find_plugin(plugins, _RANKMATH_FRAGMENTS):
            return "rankmath"
        data, _ = graceful_get(self.client, "")
        namespaces = data.get("namespaces", []) if isinstance(data, dict) else []
        if "yoast/v1" in namespaces:
            return "yoast"
        if "rankmath/v1" in namespaces:
            return "rankmath"
        return None

    # -- reads -----------------------------------------------------------
    def _schema_summary(self) -> dict[str, Any]:
        plugin = self._seo_plugin()
        label = {"yoast": "Yoast SEO", "rankmath": "Rank Math"}.get(plugin or "")
        if not plugin:
            return self._advisory(
                "No Yoast or Rank Math installation was detected. SEO metadata can still be "
                "written to core slug/excerpt fields."
            )
        schema_types = (
            ["Article", "WebPage", "Product", "FAQPage", "LocalBusiness"]
            if plugin == "yoast"
            else ["article", "product", "recipe", "faq", "local"]
        )
        return {
            "summary": f"{label} is active. Supported schema types include: {', '.join(schema_types)}.",
            "seo_plugin": plugin,
            "schema_types": schema_types,
        }

    def _audit(self, payload: dict[str, Any]) -> dict[str, Any]:
        targets = str(payload.get("targets", "posts")).lower()
        items = self._collect(targets, payload)
        missing: list[dict[str, Any]] = []
        for item in items:
            title = _title(item)
            excerpt = _excerpt(item)
            if not excerpt:
                missing.append({"id": item.get("id"), "title": title, "issue": "missing meta description"})
        return {
            "summary": (
                f"Audited {len(items)} {targets}; {len(missing)} missing a meta description. "
                "Run a bulk optimize to fill gaps (requires approval)."
            ),
            "seo_plugin": self._seo_plugin(),
            "checked": len(items),
            "needs_attention": missing[:25],
        }

    # -- write -----------------------------------------------------------
    def _bulk_optimize(self, payload: dict[str, Any]) -> dict[str, Any]:
        targets = str(payload.get("targets", "posts")).lower()
        plugin = self._seo_plugin()
        schema_type = payload.get("schema_type")
        items = self._collect(targets, payload)
        if not items:
            raise WordPressClientError("No items found to optimize. Provide ids or a search term.")
        updated: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for item in items[: int(payload.get("max", 25))]:
            item_id = item.get("id")
            suggestion = suggest_seo(_title(item), _content(item))
            meta = _meta_payload(plugin, suggestion, schema_type)
            try:
                if targets == "products":
                    body: dict[str, Any] = {"slug": suggestion.slug}
                    if meta:
                        body["meta_data"] = [{"key": k, "value": v} for k, v in meta.items()]
                    self.client.request_api(f"wc/v3/products/{item_id}", method="PUT", json_body=body)
                else:
                    fields: dict[str, Any] = {"slug": suggestion.slug, "excerpt": suggestion.meta_description}
                    if meta:
                        fields["meta"] = meta
                    endpoint = "pages" if targets == "pages" else "posts"
                    self.client.request(method="POST", endpoint=f"{endpoint}/{int(item_id)}", json_body=fields)
                updated.append({"id": item_id, "slug": suggestion.slug})
            except WordPressClientError as exc:
                skipped.append({"id": item_id, "reason": str(exc)})
        label = {"yoast": "Yoast", "rankmath": "Rank Math"}.get(plugin or "", "core fields")
        return {
            "summary": (
                f"Optimized {len(updated)} {targets} via {label}"
                + (f" with schema '{schema_type}'" if schema_type else "")
                + f"; {len(skipped)} skipped."
            ),
            "seo_plugin": plugin,
            "updated_count": len(updated),
            "skipped_count": len(skipped),
            "updated": updated[:25],
        }

    # -- helpers ---------------------------------------------------------
    def _collect(self, targets: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
        ids = payload.get("ids") or []
        search = payload.get("search")
        per_page = int(payload.get("max", 25))
        if targets == "products":
            if ids:
                items = []
                for i in ids:
                    data, _ = graceful_get(self.client, f"wc/v3/products/{int(i)}")
                    if isinstance(data, dict):
                        items.append(data)
                return items
            data, _ = graceful_get(self.client, "wc/v3/products", params={"search": search, "per_page": per_page})
            return data if isinstance(data, list) else []
        endpoint = "wp/v2/pages" if targets == "pages" else "wp/v2/posts"
        params: dict[str, Any] = {"per_page": per_page, "context": "edit"}
        if ids:
            params["include"] = ",".join(str(int(i)) for i in ids)
        if search:
            params["search"] = search
        data, _ = graceful_get(self.client, endpoint, params=params)
        return data if isinstance(data, list) else []


def _meta_payload(plugin: str | None, suggestion: Any, schema_type: Any) -> dict[str, str]:
    keyword = suggestion.focus_keywords[0] if suggestion.focus_keywords else ""
    if plugin == "yoast":
        meta = {
            "_yoast_wpseo_title": suggestion.title,
            "_yoast_wpseo_metadesc": suggestion.meta_description,
            "_yoast_wpseo_focuskw": keyword,
        }
        if schema_type:
            meta["_yoast_wpseo_schema_page_type"] = str(schema_type)
        return meta
    if plugin == "rankmath":
        meta = {
            "rank_math_title": suggestion.title,
            "rank_math_description": suggestion.meta_description,
            "rank_math_focus_keyword": keyword,
        }
        if schema_type:
            meta["rank_math_rich_snippet"] = str(schema_type)
        return meta
    return {}


def _title(item: dict[str, Any]) -> str:
    value = item.get("name") or item.get("title")
    if isinstance(value, dict):
        return str(value.get("rendered") or value.get("raw") or "")
    return str(value or "")


def _content(item: dict[str, Any]) -> str:
    value = item.get("description") or item.get("content")
    if isinstance(value, dict):
        return str(value.get("rendered") or value.get("raw") or "")
    return str(value or "")


def _excerpt(item: dict[str, Any]) -> str:
    value = item.get("excerpt") or item.get("short_description")
    if isinstance(value, dict):
        return str(value.get("rendered") or value.get("raw") or "").strip()
    return str(value or "").strip()
