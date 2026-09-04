from __future__ import annotations

from agents.crew import _plan_actions
from core.models import ChangeOperation


def _only(request: str):
    actions = _plan_actions(request)
    assert len(actions) == 1
    return actions[0]


def test_show_recent_orders_routes_to_woocommerce_read():
    action = _only("show me the recent orders")
    assert action.operation == ChangeOperation.WOOCOMMERCE_READ
    assert action.payload["action"] == "list_orders"
    assert action.requires_confirmation is False


def test_view_order_with_id():
    action = _only("view order #100")
    assert action.operation == ChangeOperation.WOOCOMMERCE_READ
    assert action.payload == {"action": "get_order", "id": 100}


def test_list_customers_routes_to_woocommerce_read():
    action = _only("list my woocommerce customers")
    assert action.operation == ChangeOperation.WOOCOMMERCE_READ
    assert action.payload["action"] == "list_customers"


def test_create_product_routes_to_woocommerce_write():
    action = _only("create a new product called Creatine Monohydrate")
    assert action.operation == ChangeOperation.WOOCOMMERCE_WRITE
    assert action.payload["action"] == "create_product"
    assert action.requires_confirmation is True


def test_update_stock_routes_to_woocommerce_write():
    action = _only("update stock for product #1 to 25")
    assert action.operation == ChangeOperation.WOOCOMMERCE_WRITE
    assert action.payload["action"] == "update_stock"
    assert action.payload["id"] == 1
    assert action.payload["stock_quantity"] == 1  # first integer in text


def test_mark_order_completed_routes_to_woocommerce_write():
    action = _only("mark order #100 as completed")
    assert action.operation == ChangeOperation.WOOCOMMERCE_WRITE
    assert action.payload["action"] == "update_order_status"
    assert action.payload["status"] == "completed"


def test_refund_routes_to_stripe_refund():
    action = _only("refund order #100")
    assert action.operation == ChangeOperation.STRIPE_REFUND
    assert action.payload == {"order_id": 100}
    assert action.requires_confirmation is True


def test_stripe_transactions_routes_to_stripe_read():
    action = _only("show recent stripe transactions")
    assert action.operation == ChangeOperation.STRIPE_READ
    assert action.payload["action"] == "transactions"


def test_plugin_inventory_branch():
    action = _only("what plugins are installed")
    assert action.operation == ChangeOperation.PLUGIN_INVENTORY


def test_bulk_seo_routes_to_seo_plugin_bulk():
    action = _only("optimize SEO for all products")
    assert action.operation == ChangeOperation.SEO_PLUGIN_BULK
    assert action.payload["targets"] == "products"
    assert action.requires_confirmation is True


def test_plain_price_bulk_still_uses_legacy_operation():
    action = _only("increase all product prices by 10%")
    assert action.operation == ChangeOperation.BULK_UPDATE_PRODUCTS


def test_delete_page_not_hijacked_by_commerce_branches():
    action = _only("delete page #4")
    assert action.operation == ChangeOperation.DELETE_PAGE
