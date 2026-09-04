"""Advanced Custom Fields handler: read-only field group summaries."""

from __future__ import annotations

from typing import Any

from tools.plugin_framework import PluginHandler, graceful_get

_FIELD_GROUP_CPT = "wp/v2/acf-field-group"


class AcfHandler(PluginHandler):
    key = "acf"
    name = "Advanced Custom Fields"
    category = "custom_fields"
    slug_fragments = ("advanced-custom-fields", "acf")
    namespaces = ("acf/v3",)
    read_actions = {
        "field_groups": "List ACF field groups and where they apply.",
        "summary": "Summarize ACF usage across the site.",
    }
    write_actions: dict[str, str] = {}

    def read(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        if action == "summary":
            return self._summary()
        return self._field_groups()

    def _field_groups(self) -> dict[str, Any]:
        data, error = graceful_get(
            self.client, _FIELD_GROUP_CPT, params={"per_page": 100, "status": "any"}
        )
        if error or not isinstance(data, list):
            return self._advisory(
                "ACF field groups are not exposed via REST on this site. Manage them under "
                "Custom Fields in wp-admin. (Field group registration can be made REST-visible.)"
            )
        groups = [
            {"id": g.get("id"), "title": _title(g), "status": g.get("status")} for g in data
        ]
        return {
            "summary": f"Found {len(groups)} ACF field group(s).",
            "count": len(groups),
            "field_groups": groups,
        }

    def _summary(self) -> dict[str, Any]:
        listing = self._field_groups()
        if not listing.get("field_groups"):
            return listing
        active = [g for g in listing["field_groups"] if g.get("status") == "publish"]
        return {
            "summary": f"ACF has {listing['count']} field group(s), {len(active)} active.",
            "count": listing["count"],
            "active_count": len(active),
        }


def _title(item: dict[str, Any]) -> str:
    title = item.get("title")
    if isinstance(title, dict):
        return str(title.get("rendered") or title.get("raw") or "")
    return str(title or "")
