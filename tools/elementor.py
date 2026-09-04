"""Elementor handler: list templates, inspect, and duplicate (approval-gated)."""

from __future__ import annotations

from typing import Any

from tools.plugin_framework import PluginHandler, graceful_get
from tools.wordpress_client import WordPressClientError

_LIBRARY_CPT = "wp/v2/elementor_library"


class ElementorHandler(PluginHandler):
    key = "elementor"
    name = "Elementor"
    category = "page_builder"
    slug_fragments = ("elementor",)
    namespaces = ("elementor/v1",)
    read_actions = {
        "list_templates": "List Elementor library templates (sections, pages, popups).",
        "get_template": "Fetch one template by id (payload: id).",
        "summary": "Summarize Elementor usage and template counts.",
    }
    write_actions = {
        "duplicate_template": "Duplicate a template as a new draft (payload: id).",
    }

    def read(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        if action == "get_template":
            return self._get_template(payload)
        if action == "summary":
            return self._summary()
        return self._list_templates(payload)

    def write(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._duplicate_template(payload)

    def _list_templates(self, payload: dict[str, Any]) -> dict[str, Any]:
        per_page = min(int(payload.get("per_page", 50) or 50), 100)
        data, error = graceful_get(self.client, _LIBRARY_CPT, params={"per_page": per_page, "status": "any"})
        if error or not isinstance(data, list):
            return self._advisory(
                "Elementor's template library is not exposed via REST on this site. "
                "Open Templates > Saved Templates in wp-admin to manage them."
            )
        templates = [
            {
                "id": t.get("id"),
                "title": _title(t),
                "type": (t.get("meta", {}) or {}).get("_elementor_template_type"),
                "status": t.get("status"),
                "modified": t.get("modified"),
            }
            for t in data
        ]
        return {
            "summary": f"Found {len(templates)} Elementor template(s).",
            "count": len(templates),
            "templates": templates,
        }

    def _get_template(self, payload: dict[str, Any]) -> dict[str, Any]:
        template_id = _require_id(payload)
        data, error = graceful_get(self.client, f"{_LIBRARY_CPT}/{template_id}", params={"context": "edit"})
        if error or not isinstance(data, dict):
            return self._advisory(f"Could not read Elementor template #{template_id}. {error or ''}".strip())
        return {
            "summary": f"Elementor template #{template_id}: {_title(data)}.",
            "template": {"id": data.get("id"), "title": _title(data), "status": data.get("status")},
        }

    def _summary(self) -> dict[str, Any]:
        listing = self._list_templates({"per_page": 100})
        if not listing.get("templates"):
            return listing
        by_type: dict[str, int] = {}
        for template in listing["templates"]:
            by_type[template.get("type") or "unknown"] = by_type.get(template.get("type") or "unknown", 0) + 1
        return {
            "summary": f"Elementor has {listing['count']} template(s): " + ", ".join(
                f"{count} {kind}" for kind, count in by_type.items()
            ),
            "count": listing["count"],
            "by_type": by_type,
        }

    def _duplicate_template(self, payload: dict[str, Any]) -> dict[str, Any]:
        template_id = _require_id(payload)
        source, error = graceful_get(self.client, f"{_LIBRARY_CPT}/{template_id}", params={"context": "edit"})
        if error or not isinstance(source, dict):
            return self._advisory(
                f"Could not read template #{template_id} to duplicate it. Duplicate it from "
                "Templates > Saved Templates instead."
            )
        title = f"{_title(source)} (copy)"
        body: dict[str, Any] = {
            "title": title,
            "status": "draft",
            "content": _raw(source.get("content")),
        }
        meta = source.get("meta")
        if isinstance(meta, dict):
            keep = {
                key: value
                for key, value in meta.items()
                if key.startswith("_elementor")
            }
            if keep:
                body["meta"] = keep
        try:
            created = self.client.request_api(_LIBRARY_CPT, method="POST", json_body=body)
        except WordPressClientError as exc:
            return self._advisory(
                "WordPress did not allow creating an Elementor template via REST on this site. "
                f"Duplicate it inside the Elementor editor instead. ({exc})"
            )
        return {
            "summary": f"Duplicated template #{template_id} as draft '{title}'.",
            "template": {"id": created.get("id"), "title": title, "status": created.get("status", "draft")},
        }


def _title(item: dict[str, Any]) -> str:
    title = item.get("title")
    if isinstance(title, dict):
        return str(title.get("rendered") or title.get("raw") or "")
    return str(title or "")


def _raw(content: Any) -> str:
    if isinstance(content, dict):
        return str(content.get("raw") or content.get("rendered") or "")
    return str(content or "")


def _require_id(payload: dict[str, Any]) -> int:
    value = payload.get("id")
    if not value:
        raise WordPressClientError("I need the Elementor template id.")
    return int(value)
