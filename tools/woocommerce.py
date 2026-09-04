"""WooCommerce REST tools (namespace ``wc/v3/``).

Deep, structured WooCommerce support built on the existing :class:`WordPressClient`.
Read operations are low-risk; write operations (create/update/delete/bulk) are
expected to be confirmation- and backup-gated by the safety layer before they run.

The tools intentionally keep the conservative philosophy:
- New products default to ``draft`` unless the caller explicitly publishes.
- Customer data is returned with PII (email/phone) masked.
- Bulk operations are capped and report updated/skipped counts.
"""

from __future__ import annotations

import logging
from typing import Any

from tools.wordpress_client import WordPressClient, WordPressClientError

LOGGER = logging.getLogger(__name__)

WC_NAMESPACE = "wc/v3"
_DEFAULT_PER_PAGE = 20
_MAX_PER_PAGE = 100
_MAX_BULK_ITEMS = 100

READ_ACTIONS = (
    "overview",
    "list_products",
    "get_product",
    "list_orders",
    "get_order",
    "list_categories",
    "list_tags",
    "list_variations",
    "list_customers",
    "low_stock",
)
WRITE_ACTIONS = (
    "create_product",
    "create_products",
    "update_product",
    "delete_product",
    "update_stock",
    "update_order_status",
    "bulk_price",
    "bulk_stock",
    "bulk_status",
)


class WooCommerceTools:
    """High-level WooCommerce operations over the REST API."""

    def __init__(self, client: WordPressClient) -> None:
        self.client = client

    # -- detection -------------------------------------------------------
    def is_active(self) -> bool:
        try:
            self.client.request_api(f"{WC_NAMESPACE}/system_status", params={"_fields": "environment"})
            return True
        except WordPressClientError:
            try:
                self.client.request_api(f"{WC_NAMESPACE}/products", params={"per_page": 1})
                return True
            except WordPressClientError:
                return False

    # -- dispatch --------------------------------------------------------
    def read(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        action = (action or "overview").lower()
        if action == "list_products":
            return self._list_products(payload)
        if action == "get_product":
            return self._get_product(payload)
        if action == "list_orders":
            return self._list_orders(payload)
        if action == "get_order":
            return self._get_order(payload)
        if action == "list_categories":
            return self._list_terms("categories", payload)
        if action == "list_tags":
            return self._list_terms("tags", payload)
        if action == "list_variations":
            return self._list_variations(payload)
        if action == "list_customers":
            return self._list_customers(payload)
        if action == "low_stock":
            return self._low_stock(payload)
        return self._overview(payload)

    def write(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        action = (action or "").lower()
        if action == "create_product":
            return self._create_product(payload)
        if action == "create_products":
            return self._create_products(payload)
        if action == "update_product":
            return self._update_product(payload)
        if action == "delete_product":
            return self._delete_product(payload)
        if action == "update_stock":
            return self._update_stock(payload)
        if action == "update_order_status":
            return self._update_order_status(payload)
        if action == "bulk_price":
            return self._bulk_products(payload, mode="price")
        if action == "bulk_stock":
            return self._bulk_products(payload, mode="stock")
        if action == "bulk_status":
            return self._bulk_products(payload, mode="status")
        raise WordPressClientError(
            f"Unsupported WooCommerce write action: '{action}'. Supported: {', '.join(WRITE_ACTIONS)}."
        )

    # -- reads -----------------------------------------------------------
    def _overview(self, payload: dict[str, Any]) -> dict[str, Any]:
        products = self.client.request_api(
            f"{WC_NAMESPACE}/products", params={"per_page": 1, "_fields": "id"}
        )
        recent_orders = self.client.request_api(
            f"{WC_NAMESPACE}/orders",
            params={"per_page": 5, "_fields": "id,number,status,total,currency,date_created"},
        )
        orders = recent_orders if isinstance(recent_orders, list) else []
        return {
            "summary": (
                f"WooCommerce is connected. Showing the {len(orders)} most recent order(s). "
                "Ask for products, orders, categories, inventory, or customers for more."
            ),
            "recent_orders": [self._order_brief(o) for o in orders],
            "count": len(orders),
        }

    def _list_products(self, payload: dict[str, Any]) -> dict[str, Any]:
        params = {
            "per_page": _clamp(payload.get("per_page", _DEFAULT_PER_PAGE), _MAX_PER_PAGE),
            "status": payload.get("status", "any"),
        }
        if payload.get("search"):
            params["search"] = payload["search"]
        if payload.get("category"):
            params["category"] = payload["category"]
        if payload.get("sku"):
            params["sku"] = payload["sku"]
        products = self.client.request_api(f"{WC_NAMESPACE}/products", params=params)
        products = products if isinstance(products, list) else []
        return {
            "summary": f"Found {len(products)} product(s).",
            "count": len(products),
            "products": [self._product_brief(p) for p in products],
        }

    def _get_product(self, payload: dict[str, Any]) -> dict[str, Any]:
        product_id = _require_id(payload, "product")
        product = self.client.request_api(f"{WC_NAMESPACE}/products/{product_id}")
        brief = self._product_brief(product)
        return {"summary": f"Product #{product_id}: {brief.get('name')}.", "product": brief}

    def _list_orders(self, payload: dict[str, Any]) -> dict[str, Any]:
        params = {"per_page": _clamp(payload.get("per_page", _DEFAULT_PER_PAGE), _MAX_PER_PAGE)}
        if payload.get("status"):
            params["status"] = payload["status"]
        orders = self.client.request_api(f"{WC_NAMESPACE}/orders", params=params)
        orders = orders if isinstance(orders, list) else []
        return {
            "summary": f"Found {len(orders)} order(s).",
            "count": len(orders),
            "orders": [self._order_brief(o) for o in orders],
        }

    def _get_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        order_id = _require_id(payload, "order")
        order = self.client.request_api(f"{WC_NAMESPACE}/orders/{order_id}")
        return {"summary": f"Order #{order_id}.", "order": self._order_brief(order, detailed=True)}

    def _list_terms(self, term: str, payload: dict[str, Any]) -> dict[str, Any]:
        params = {"per_page": _clamp(payload.get("per_page", 50), _MAX_PER_PAGE)}
        terms = self.client.request_api(f"{WC_NAMESPACE}/products/{term}", params=params)
        terms = terms if isinstance(terms, list) else []
        return {
            "summary": f"Found {len(terms)} product {term}.",
            "count": len(terms),
            term: [{"id": t.get("id"), "name": t.get("name"), "count": t.get("count")} for t in terms],
        }

    def _list_variations(self, payload: dict[str, Any]) -> dict[str, Any]:
        product_id = _require_id(payload, "product")
        variations = self.client.request_api(
            f"{WC_NAMESPACE}/products/{product_id}/variations",
            params={"per_page": _clamp(payload.get("per_page", 50), _MAX_PER_PAGE)},
        )
        variations = variations if isinstance(variations, list) else []
        return {
            "summary": f"Product #{product_id} has {len(variations)} variation(s).",
            "count": len(variations),
            "variations": [self._product_brief(v) for v in variations],
        }

    def _list_customers(self, payload: dict[str, Any]) -> dict[str, Any]:
        customers = self.client.request_api(
            f"{WC_NAMESPACE}/customers",
            params={"per_page": _clamp(payload.get("per_page", _DEFAULT_PER_PAGE), _MAX_PER_PAGE)},
        )
        customers = customers if isinstance(customers, list) else []
        return {
            "summary": (
                f"Found {len(customers)} customer(s). Personal contact details are masked; "
                "view full details in the WooCommerce dashboard."
            ),
            "count": len(customers),
            "customers": [_redact_customer(c) for c in customers],
        }

    def _low_stock(self, payload: dict[str, Any]) -> dict[str, Any]:
        threshold = _to_float(payload.get("threshold"), 5.0)
        products = self.client.paginated_request_api(
            f"{WC_NAMESPACE}/products",
            params={"status": "publish"},
            per_page=100,
            max_pages=int(payload.get("max_pages", 5)),
        )
        low: list[dict[str, Any]] = []
        for product in products if isinstance(products, list) else []:
            qty = product.get("stock_quantity")
            if product.get("manage_stock") and isinstance(qty, (int, float)) and qty <= threshold:
                low.append(self._product_brief(product))
        return {
            "summary": f"{len(low)} product(s) at or below stock threshold {int(threshold)}.",
            "count": len(low),
            "low_stock": low[:50],
        }

    # -- writes ----------------------------------------------------------
    def _create_product(self, payload: dict[str, Any]) -> dict[str, Any]:
        product = payload.get("product") or payload.get("fields") or {}
        body = _prepare_product_body(product)
        if not body.get("name"):
            raise WordPressClientError("A product needs a name before it can be created.")
        created = self.client.request_api(f"{WC_NAMESPACE}/products", method="POST", json_body=body)
        brief = self._product_brief(created)
        return {
            "summary": f"Created product '{brief.get('name')}' (status: {brief.get('status')}).",
            "product": brief,
        }

    def _create_products(self, payload: dict[str, Any]) -> dict[str, Any]:
        items = payload.get("products") or payload.get("items") or []
        if not isinstance(items, list) or not items:
            raise WordPressClientError("Provide a list of products to create.")
        if len(items) > _MAX_BULK_ITEMS:
            raise WordPressClientError(f"Too many products at once. The limit is {_MAX_BULK_ITEMS}.")
        bodies = [_prepare_product_body(item) for item in items]
        missing = [i for i, b in enumerate(bodies) if not b.get("name")]
        if missing:
            raise WordPressClientError(f"Every product needs a name. Missing name at index {missing[0]}.")
        result = self.client.request_api(
            f"{WC_NAMESPACE}/products/batch", method="POST", json_body={"create": bodies}
        )
        created = result.get("create", []) if isinstance(result, dict) else []
        briefs = [self._product_brief(p) for p in created]
        return {
            "summary": f"Created {len(briefs)} product(s) as drafts unless published explicitly.",
            "created_count": len(briefs),
            "products": briefs[:25],
        }

    def _update_product(self, payload: dict[str, Any]) -> dict[str, Any]:
        product_id = _require_id(payload, "product")
        fields = _prepare_product_body(payload.get("fields") or payload.get("product") or {}, for_update=True)
        if not fields:
            raise WordPressClientError("Tell me which product fields to update.")
        updated = self.client.request_api(
            f"{WC_NAMESPACE}/products/{product_id}", method="PUT", json_body=fields
        )
        return {
            "summary": f"Updated product #{product_id}.",
            "product": self._product_brief(updated),
        }

    def _delete_product(self, payload: dict[str, Any]) -> dict[str, Any]:
        product_id = _require_id(payload, "product")
        force = bool(payload.get("force", False))
        deleted = self.client.request_api(
            f"{WC_NAMESPACE}/products/{product_id}", method="DELETE", params={"force": force}
        )
        verb = "permanently deleted" if force else "moved to trash"
        return {"summary": f"Product #{product_id} {verb}.", "product": self._product_brief(deleted)}

    def _update_stock(self, payload: dict[str, Any]) -> dict[str, Any]:
        product_id = _require_id(payload, "product")
        body: dict[str, Any] = {"manage_stock": bool(payload.get("manage_stock", True))}
        if "stock_quantity" in payload:
            body["stock_quantity"] = int(payload["stock_quantity"])
        if payload.get("stock_status"):
            body["stock_status"] = payload["stock_status"]
        updated = self.client.request_api(
            f"{WC_NAMESPACE}/products/{product_id}", method="PUT", json_body=body
        )
        brief = self._product_brief(updated)
        return {
            "summary": f"Updated stock for product #{product_id} (now {brief.get('stock_quantity')}).",
            "product": brief,
        }

    def _update_order_status(self, payload: dict[str, Any]) -> dict[str, Any]:
        order_id = _require_id(payload, "order")
        status = payload.get("status")
        if not status:
            raise WordPressClientError("Tell me the new order status (e.g. processing, completed, refunded).")
        updated = self.client.request_api(
            f"{WC_NAMESPACE}/orders/{order_id}", method="PUT", json_body={"status": status}
        )
        return {
            "summary": f"Order #{order_id} status set to '{status}'.",
            "order": self._order_brief(updated),
        }

    def _bulk_products(self, payload: dict[str, Any], *, mode: str) -> dict[str, Any]:
        products = self.client.paginated_request_api(
            f"{WC_NAMESPACE}/products",
            params={"status": payload.get("status", "any")},
            per_page=int(payload.get("per_page", 100)),
            max_pages=int(payload.get("max_pages", 10)),
        )
        if not isinstance(products, list) or not products:
            raise WordPressClientError("No WooCommerce products were returned for the bulk update.")
        updated: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for product in products:
            product_id = product.get("id")
            body = self._bulk_body(product, payload, mode)
            if body is None:
                skipped.append({"id": product_id, "name": product.get("name")})
                continue
            result = self.client.request_api(
                f"{WC_NAMESPACE}/products/{product_id}", method="PUT", json_body=body
            )
            updated.append(self._product_brief(result))
        return {
            "summary": f"Bulk {mode} update applied to {len(updated)} product(s); {len(skipped)} skipped.",
            "mode": mode,
            "updated_count": len(updated),
            "skipped_count": len(skipped),
            "updated": updated[:25],
            "skipped": skipped[:25],
        }

    def _bulk_body(self, product: dict[str, Any], payload: dict[str, Any], mode: str) -> dict[str, Any] | None:
        if mode == "price":
            percent = _to_float(payload.get("percent"), 0.0)
            base = product.get("regular_price") or product.get("price")
            try:
                if percent == 0 or not base:
                    return None
                return {"regular_price": f"{float(base) * (1 + percent / 100):.2f}"}
            except (TypeError, ValueError):
                return None
        if mode == "stock":
            if "stock_quantity" not in payload:
                return None
            return {"manage_stock": True, "stock_quantity": int(payload["stock_quantity"])}
        if mode == "status":
            status = payload.get("status_value") or payload.get("new_status")
            return {"status": status} if status else None
        return None

    # -- briefs ----------------------------------------------------------
    def _product_brief(self, product: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(product, dict):
            return {}
        return {
            "id": product.get("id"),
            "name": product.get("name"),
            "sku": product.get("sku"),
            "type": product.get("type"),
            "status": product.get("status"),
            "price": product.get("price"),
            "regular_price": product.get("regular_price"),
            "sale_price": product.get("sale_price"),
            "stock_quantity": product.get("stock_quantity"),
            "stock_status": product.get("stock_status"),
            "permalink": product.get("permalink"),
        }

    def _order_brief(self, order: dict[str, Any], *, detailed: bool = False) -> dict[str, Any]:
        if not isinstance(order, dict):
            return {}
        brief = {
            "id": order.get("id"),
            "number": order.get("number"),
            "status": order.get("status"),
            "total": order.get("total"),
            "currency": order.get("currency"),
            "payment_method_title": order.get("payment_method_title"),
            "date_created": order.get("date_created"),
        }
        if detailed:
            items = order.get("line_items", []) or []
            brief["line_items"] = [
                {"name": i.get("name"), "quantity": i.get("quantity"), "total": i.get("total")}
                for i in items[:25]
            ]
            brief["customer_note"] = order.get("customer_note")
        return brief


def _prepare_product_body(product: dict[str, Any], *, for_update: bool = False) -> dict[str, Any]:
    if not isinstance(product, dict):
        return {}
    allowed = {
        "name",
        "type",
        "regular_price",
        "sale_price",
        "description",
        "short_description",
        "sku",
        "status",
        "categories",
        "tags",
        "images",
        "stock_quantity",
        "manage_stock",
        "stock_status",
        "weight",
        "dimensions",
        "attributes",
    }
    body = {key: value for key, value in product.items() if key in allowed}
    if "regular_price" in body and body["regular_price"] is not None:
        body["regular_price"] = str(body["regular_price"])
    if "sale_price" in body and body["sale_price"] is not None:
        body["sale_price"] = str(body["sale_price"])
    # Conservative default: new products are drafts unless explicitly published.
    if not for_update:
        body.setdefault("type", "simple")
        body.setdefault("status", "draft")
    return body


def _redact_customer(customer: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(customer, dict):
        return {}
    return {
        "id": customer.get("id"),
        "username": customer.get("username"),
        "email": _mask_email(customer.get("email")),
        "first_name": customer.get("first_name"),
        "orders_count": customer.get("orders_count"),
        "total_spent": customer.get("total_spent"),
        "date_created": customer.get("date_created"),
    }


def _mask_email(email: Any) -> str:
    text = str(email or "")
    if "@" not in text:
        return ""
    local, _, domain = text.partition("@")
    visible = local[:1]
    return f"{visible}***@{domain}"


def _require_id(payload: dict[str, Any], name: str) -> int:
    value = payload.get("id")
    if not value:
        raise WordPressClientError(f"I need the {name} ID before I can do that.")
    return int(value)


def _clamp(value: Any, maximum: int) -> int:
    try:
        return max(1, min(int(value), maximum))
    except (TypeError, ValueError):
        return min(_DEFAULT_PER_PAGE, maximum)


def _to_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
