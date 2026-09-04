"""Advanced WooCommerce + Stripe handler for the plugin framework.

Composes the structured :class:`WooCommerceTools` and :class:`StripeTools` and
adds sales reporting. Exposes the full product/order/customer/variation surface
through the generic ``PLUGIN_READ`` / ``PLUGIN_ACTION`` operations in addition to
the dedicated WooCommerce operations.
"""

from __future__ import annotations

from typing import Any

from tools.plugin_framework import PluginHandler, graceful_get
from tools.stripe_gateway import StripeTools
from tools.woocommerce import READ_ACTIONS as WC_READ_ACTIONS
from tools.woocommerce import WooCommerceTools
from tools.wordpress_client import WordPressClient

_STRIPE_READ_MAP = {"stripe_status": "status", "stripe_transactions": "transactions", "stripe_settings": "settings"}


class WooCommerceAdvancedHandler(PluginHandler):
    key = "woocommerce"
    name = "WooCommerce"
    category = "ecommerce"
    slug_fragments = ("woocommerce",)
    namespaces = ("wc/v3",)
    read_actions = {
        "overview": "Store overview with the most recent orders.",
        "list_products": "List/search products (filters: search, status, category, sku).",
        "get_product": "Fetch a single product by id.",
        "list_orders": "List recent orders (filter: status).",
        "get_order": "Fetch a single order by id, including line items.",
        "list_categories": "List product categories.",
        "list_tags": "List product tags.",
        "list_variations": "List variations for a variable product (payload: id).",
        "list_customers": "List customers with contact details masked.",
        "low_stock": "Report products at/below a stock threshold.",
        "sales_report": "Summarize sales totals and top sellers.",
        "stripe_status": "Detect the Stripe gateway and its mode.",
        "stripe_transactions": "List recent Stripe transactions (read-only).",
        "stripe_settings": "Summarize Stripe settings with keys redacted.",
    }
    write_actions = {
        "create_product": "Create a product (defaults to draft).",
        "create_products": "Bulk-create products via the batch endpoint.",
        "update_product": "Update product fields by id.",
        "delete_product": "Delete/trash a product by id.",
        "update_stock": "Set stock quantity/status for a product.",
        "update_order_status": "Change an order's status.",
        "bulk_price": "Adjust regular prices by percent across products.",
        "bulk_stock": "Set stock quantity across products.",
        "bulk_status": "Set status across products.",
        "refund": "Refund an order through the Stripe gateway.",
    }

    def __init__(self, client: WordPressClient) -> None:
        super().__init__(client)
        self.wc = WooCommerceTools(client)
        self.stripe = StripeTools(client)

    def read(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        if action in WC_READ_ACTIONS:
            return self.wc.read(action, payload)
        if action == "sales_report":
            return self._sales_report(payload)
        if action in _STRIPE_READ_MAP:
            return self.stripe.read(_STRIPE_READ_MAP[action], payload)
        return self.wc.read("overview", payload)

    def write(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        if action == "refund":
            return self.stripe.refund(payload)
        return self.wc.write(action, payload)

    def _sales_report(self, payload: dict[str, Any]) -> dict[str, Any]:
        period = payload.get("period", "month")
        sales, sales_err = graceful_get(self.client, "wc/v3/reports/sales", params={"period": period})
        top, _ = graceful_get(self.client, "wc/v3/reports/top_sellers", params={"period": period})
        report = sales[0] if isinstance(sales, list) and sales else (sales if isinstance(sales, dict) else {})
        if not report and sales_err:
            return self._advisory(
                "Sales reports require WooCommerce analytics permissions. "
                f"Open WooCommerce > Reports for details. ({sales_err})"
            )
        return {
            "summary": (
                f"Sales for this {period}: {report.get('total_sales', 'n/a')} across "
                f"{report.get('total_orders', 'n/a')} order(s)."
            ),
            "period": period,
            "total_sales": report.get("total_sales"),
            "net_sales": report.get("net_sales"),
            "total_orders": report.get("total_orders"),
            "total_items": report.get("total_items"),
            "top_sellers": [
                {"name": item.get("name"), "quantity": item.get("quantity")}
                for item in (top if isinstance(top, list) else [])[:10]
            ],
        }
