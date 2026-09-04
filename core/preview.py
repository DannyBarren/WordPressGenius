"""Staged change previews for risky WordPress actions.

Before a confirmation-gated change runs, this module fetches the current state of
each affected resource (read-only) and computes a human-readable before -> after
diff. The preview is best-effort: any failure to read a resource simply omits its
diff rather than blocking the plan.
"""

from __future__ import annotations

import logging
from typing import Any

from core.models import ChangeOperation, PlannedAction
from tools.seo import suggest_seo

LOGGER = logging.getLogger(__name__)

# Operations worth previewing because they alter or remove existing resources.
_PREVIEWABLE = {
    ChangeOperation.UPDATE_POST,
    ChangeOperation.UPDATE_PAGE,
    ChangeOperation.DELETE_POST,
    ChangeOperation.DELETE_PAGE,
    ChangeOperation.SEO_OPTIMIZE,
    ChangeOperation.BULK_UPDATE_PRODUCTS,
    ChangeOperation.WOOCOMMERCE_WRITE,
    ChangeOperation.STRIPE_REFUND,
}


def build_change_previews(tools: Any, actions: list[PlannedAction]) -> list[dict[str, Any]]:
    """Return a best-effort list of preview dicts for confirmation-gated actions."""

    previews: list[dict[str, Any]] = []
    for action in actions:
        if action.operation not in _PREVIEWABLE:
            continue
        try:
            preview = _preview_action(tools, action)
        except Exception as exc:  # noqa: BLE001 - previews must never break the flow.
            LOGGER.info("Preview unavailable for %s: %s", action.title, exc)
            preview = None
        if preview:
            previews.append(preview)
    return previews


def render_previews(previews: list[dict[str, Any]]) -> str:
    """Render previews as a markdown block for the confirmation summary."""

    if not previews:
        return ""
    lines = ["Preview of changes (nothing applied yet):"]
    for preview in previews:
        header = preview.get("target") or preview.get("title", "Change")
        if preview.get("destructive"):
            lines.append(f"- [DELETE] {header}: {preview.get('note', 'will be removed.')}")
            continue
        note = preview.get("note")
        if note:
            lines.append(f"- {header}: {note}")
        for field, before, after in preview.get("changes", []):
            lines.append(f"    - {field}: '{_clip(before)}' -> '{_clip(after)}'")
    return "\n".join(lines)


def _preview_action(tools: Any, action: PlannedAction) -> dict[str, Any] | None:
    client = tools.client
    op = action.operation
    payload = action.payload

    if op in {ChangeOperation.UPDATE_POST, ChangeOperation.UPDATE_PAGE}:
        resource_id = payload.get("id")
        if not resource_id:
            return None
        current = client.get_page(int(resource_id)) if op == ChangeOperation.UPDATE_PAGE else client.get_post(int(resource_id))
        changes = _field_changes(current, payload, ["status", "slug", "excerpt"])
        proposed_title = payload.get("title")
        if proposed_title and _text(current.get("title")) != proposed_title:
            changes.insert(0, ("title", _text(current.get("title")), proposed_title))
        if "content" in payload:
            changes.append(("content", "current body", "new draft prepared by the writer"))
        return {"title": action.title, "target": f"{_kind(op)} #{resource_id}: {_text(current.get('title'))}", "changes": changes}

    if op in {ChangeOperation.DELETE_POST, ChangeOperation.DELETE_PAGE}:
        resource_id = payload.get("id")
        if not resource_id:
            return None
        current = client.get_page(int(resource_id)) if op == ChangeOperation.DELETE_PAGE else client.get_post(int(resource_id))
        force = bool(payload.get("force", False))
        verb = "permanently deleted" if force else "moved to trash"
        return {
            "title": action.title,
            "target": f"{_kind(op)} #{resource_id}: {_text(current.get('title'))}",
            "destructive": True,
            "note": f"current status '{current.get('status')}' will be {verb}.",
        }

    if op == ChangeOperation.SEO_OPTIMIZE:
        resource_id = payload.get("id")
        if not resource_id:
            return {"title": action.title, "note": "prepares SEO suggestions only (no page/post id given)."}
        getter = client.get_page if payload.get("type") == "page" else client.get_post
        current = getter(int(resource_id))
        suggestion = suggest_seo(payload.get("title") or _text(current.get("title")), "")
        changes = [
            ("slug", current.get("slug", ""), suggestion.slug),
            ("excerpt", _text(current.get("excerpt")), suggestion.meta_description),
        ]
        return {"title": action.title, "target": f"#{resource_id}: {_text(current.get('title'))}", "changes": changes}

    if op == ChangeOperation.BULK_UPDATE_PRODUCTS:
        percent = payload.get("percent", 0)
        return {"title": action.title, "note": f"every matching product's regular price changes by {percent}% (reversible only via the product's prior value)."}

    if op == ChangeOperation.WOOCOMMERCE_WRITE:
        return _preview_woocommerce(client, action)

    if op == ChangeOperation.STRIPE_REFUND:
        order_id = payload.get("order_id") or payload.get("id")
        if not order_id:
            return None
        order = client.request_api(f"wc/v3/orders/{int(order_id)}")
        amount = payload.get("amount") or order.get("total")
        return {
            "title": action.title,
            "target": f"Order #{order_id}",
            "destructive": True,
            "note": f"refund of {amount} {order.get('currency', '')} will be sent to the customer (irreversible).",
        }

    return None


def _preview_woocommerce(client: Any, action: PlannedAction) -> dict[str, Any] | None:
    payload = action.payload
    sub = str(payload.get("action", "")).lower()
    resource_id = payload.get("id")
    if sub == "create_product":
        product = payload.get("product") or payload.get("fields") or {}
        return {"title": action.title, "note": f"creates a new draft product '{product.get('name', 'unnamed')}'."}
    if sub == "create_products":
        items = payload.get("products") or payload.get("items") or []
        return {"title": action.title, "note": f"creates {len(items)} new draft product(s)."}
    if sub in {"update_product", "update_stock"} and resource_id:
        current = client.request_api(f"wc/v3/products/{int(resource_id)}")
        fields = payload.get("fields") or {k: v for k, v in payload.items() if k not in {"action", "id"}}
        changes = _field_changes(current, fields, ["regular_price", "sale_price", "stock_quantity", "status", "name"])
        return {"title": action.title, "target": f"Product #{resource_id}: {current.get('name')}", "changes": changes}
    if sub == "delete_product" and resource_id:
        current = client.request_api(f"wc/v3/products/{int(resource_id)}")
        verb = "permanently deleted" if payload.get("force") else "moved to trash"
        return {"title": action.title, "target": f"Product #{resource_id}: {current.get('name')}", "destructive": True, "note": f"will be {verb}."}
    if sub == "update_order_status" and resource_id:
        current = client.request_api(f"wc/v3/orders/{int(resource_id)}")
        return {"title": action.title, "target": f"Order #{resource_id}", "changes": [("status", current.get("status"), payload.get("status"))]}
    if sub.startswith("bulk_"):
        return {"title": action.title, "note": "applies a bulk change across many products (review carefully)."}
    return None


def _field_changes(current: dict[str, Any], proposed: dict[str, Any], fields: list[str]) -> list[tuple[str, str, str]]:
    changes: list[tuple[str, str, str]] = []
    for field in fields:
        if field not in proposed:
            continue
        before = _text(current.get(field))
        after = _text(proposed.get(field))
        if before != after:
            changes.append((field, before, after))
    return changes


def _kind(op: ChangeOperation) -> str:
    return "Page" if op in {ChangeOperation.UPDATE_PAGE, ChangeOperation.DELETE_PAGE} else "Post"


def _text(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("rendered") or value.get("raw") or "")
    return "" if value is None else str(value)


def _clip(value: Any, limit: int = 80) -> str:
    text = _text(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."
