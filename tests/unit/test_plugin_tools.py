from __future__ import annotations

import pytest

from core.models import ChangeOperation, PlannedAction, RiskLevel
from core.safety import SafetyLayer
from tools.backups import BackupManager
from tools.plugin_manager import PluginManager
from tools.stripe_gateway import StripeTools
from tools.woocommerce import WooCommerceTools
from tools.wordpress_client import WordPressClientError
from tools.wordpress_tools import WordPressTools


class FakeClient:
    """A configurable fake WordPress REST client for wc/v3 and wp/v2 calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict | None]] = []
        self.posts: dict[int, dict] = {
            7: {"id": 7, "title": {"rendered": "Protein Guide"}, "content": {"rendered": "Whey casein"}}
        }
        self.updated_posts: list[tuple[int, dict]] = []

    # -- generic REST ----------------------------------------------------
    def request_api(self, endpoint, *, method="GET", params=None, json_body=None):
        self.calls.append((method, endpoint, json_body))
        if endpoint == "":
            return {"namespaces": ["wp/v2", "wc/v3", "yoast/v1"]}
        if endpoint == "wc/v3/system_status":
            return {"environment": {"version": "8.0"}}
        if endpoint == "wc/v3/products" and method == "GET":
            return [
                {"id": 1, "name": "Whey Protein", "regular_price": "40.00", "price": "40.00",
                 "status": "publish", "stock_quantity": 3, "manage_stock": True, "sku": "WP-1"},
                {"id": 2, "name": "Shaker", "regular_price": "10.00", "price": "10.00",
                 "status": "publish", "stock_quantity": 50, "manage_stock": True, "sku": "SK-2"},
            ]
        if endpoint == "wc/v3/products" and method == "POST":
            return {"id": 99, **json_body}
        if endpoint == "wc/v3/products/batch" and method == "POST":
            return {"create": [{"id": 101, **item} for item in json_body.get("create", [])]}
        if endpoint == "wc/v3/products/1" and method == "GET":
            return {"id": 1, "name": "Whey Protein", "regular_price": "40.00", "stock_quantity": 3}
        if endpoint.startswith("wc/v3/products/") and method == "PUT":
            pid = int(endpoint.rsplit("/", 1)[1])
            return {"id": pid, "name": "Whey Protein", "status": json_body.get("status", "publish"),
                    "stock_quantity": json_body.get("stock_quantity", 3),
                    "regular_price": json_body.get("regular_price", "40.00")}
        if endpoint.startswith("wc/v3/products/") and method == "DELETE":
            pid = int(endpoint.rsplit("/", 1)[1])
            return {"id": pid, "name": "Whey Protein", "status": "trash"}
        if endpoint == "wc/v3/products/categories":
            return [{"id": 5, "name": "Supplements", "count": 12}]
        if endpoint == "wc/v3/products/1/variations":
            return [{"id": 11, "name": "1kg", "price": "40.00"}]
        if endpoint == "wc/v3/customers":
            return [{"id": 3, "username": "jane", "email": "jane@example.com", "orders_count": 4,
                     "total_spent": "120.00"}]
        if endpoint == "wc/v3/orders" and method == "GET":
            return [
                {"id": 100, "number": "100", "status": "processing", "total": "50.00",
                 "currency": "USD", "payment_method": "stripe", "payment_method_title": "Credit Card",
                 "transaction_id": "ch_123"},
                {"id": 101, "number": "101", "status": "completed", "total": "20.00",
                 "currency": "USD", "payment_method": "cod", "payment_method_title": "Cash"},
            ]
        if endpoint == "wc/v3/orders/100" and method == "GET":
            return {"id": 100, "number": "100", "status": "processing", "total": "50.00",
                    "line_items": [{"name": "Whey Protein", "quantity": 1, "total": "40.00"}]}
        if endpoint == "wc/v3/orders/100" and method == "PUT":
            return {"id": 100, "number": "100", "status": json_body["status"], "total": "50.00"}
        if endpoint == "wc/v3/orders/100/refunds" and method == "POST":
            return {"id": 500, "amount": json_body.get("amount", "50.00"), "reason": json_body.get("reason")}
        if endpoint == "wc/v3/payment_gateways":
            return [
                {"id": "stripe", "title": "Credit Card (Stripe)", "enabled": True,
                 "method_title": "Stripe",
                 "settings": {"testmode": {"value": "yes"},
                              "publishable_key": {"value": "pk_live_x"},
                              "secret_key": {"value": "sk_live_y"},
                              "statement_descriptor": {"value": "BULLFROG"}}},
                {"id": "cod", "title": "Cash on delivery", "enabled": False, "settings": {}},
            ]
        raise AssertionError(f"Unexpected request_api: {method} {endpoint}")

    def paginated_request_api(self, endpoint, *, params=None, per_page=100, max_pages=10):
        return self.request_api(endpoint, params={**(params or {}), "per_page": per_page})

    # -- wp/v2 helpers used by SEO bulk ---------------------------------
    def list_plugins(self):
        return [
            {"plugin": "woocommerce/woocommerce.php", "name": "WooCommerce", "status": "active", "version": "8.0"},
            {"plugin": "wordpress-seo/wp-seo.php", "name": "Yoast SEO", "status": "active", "version": "22.0"},
            {"plugin": "akismet/akismet.php", "name": "Akismet", "status": "inactive", "version": "5.0"},
        ]

    def get_posts(self, *, search=None, status="any", per_page=20):
        return list(self.posts.values())

    def get_post(self, post_id):
        return self.posts[int(post_id)]

    def update_post(self, post_id, **fields):
        self.updated_posts.append((int(post_id), fields))
        return {"id": int(post_id), **fields}


@pytest.fixture
def fake_tools(tmp_path):
    return WordPressTools(FakeClient(), BackupManager(tmp_path / "backups"))  # type: ignore[arg-type]


# -- WooCommerce reads ---------------------------------------------------
def test_woocommerce_list_products():
    wc = WooCommerceTools(FakeClient())  # type: ignore[arg-type]
    result = wc.read("list_products", {})
    assert result["count"] == 2
    assert result["products"][0]["name"] == "Whey Protein"
    assert result["products"][0]["sku"] == "WP-1"


def test_woocommerce_customers_are_redacted():
    wc = WooCommerceTools(FakeClient())  # type: ignore[arg-type]
    result = wc.read("list_customers", {})
    assert result["customers"][0]["email"] == "j***@example.com"


def test_woocommerce_low_stock_threshold():
    wc = WooCommerceTools(FakeClient())  # type: ignore[arg-type]
    result = wc.read("low_stock", {"threshold": 5})
    assert result["count"] == 1
    assert result["low_stock"][0]["id"] == 1


# -- WooCommerce writes --------------------------------------------------
def test_create_product_defaults_to_draft():
    client = FakeClient()
    wc = WooCommerceTools(client)  # type: ignore[arg-type]
    result = wc.write("create_product", {"product": {"name": "Creatine", "regular_price": 25}})
    assert result["product"]["status"] == "draft"
    method, endpoint, body = client.calls[-1]
    assert (method, endpoint) == ("POST", "wc/v3/products")
    assert body["status"] == "draft"
    assert body["regular_price"] == "25"


def test_create_product_requires_name():
    wc = WooCommerceTools(FakeClient())  # type: ignore[arg-type]
    with pytest.raises(WordPressClientError):
        wc.write("create_product", {"product": {"regular_price": 25}})


def test_bulk_create_products_uses_batch():
    client = FakeClient()
    wc = WooCommerceTools(client)  # type: ignore[arg-type]
    result = wc.write("create_products", {"products": [{"name": "A"}, {"name": "B"}]})
    assert result["created_count"] == 2
    assert ("POST", "wc/v3/products/batch", {"create": [
        {"name": "A", "type": "simple", "status": "draft"},
        {"name": "B", "type": "simple", "status": "draft"},
    ]}) == client.calls[-1]


def test_update_order_status():
    client = FakeClient()
    wc = WooCommerceTools(client)  # type: ignore[arg-type]
    result = wc.write("update_order_status", {"id": 100, "status": "completed"})
    assert "completed" in result["summary"]
    assert client.calls[-1] == ("PUT", "wc/v3/orders/100", {"status": "completed"})


def test_unknown_write_action_raises():
    wc = WooCommerceTools(FakeClient())  # type: ignore[arg-type]
    with pytest.raises(WordPressClientError):
        wc.write("nuke_everything", {})


# -- Stripe --------------------------------------------------------------
def test_stripe_status_detects_gateway():
    stripe = StripeTools(FakeClient())  # type: ignore[arg-type]
    result = stripe.status({})
    assert result["active"] is True
    assert result["gateways"][0]["test_mode"] is True


def test_stripe_settings_redacts_secrets():
    stripe = StripeTools(FakeClient())  # type: ignore[arg-type]
    settings = stripe.settings_summary({})["gateways"][0]["settings"]
    assert settings["secret_key"] == "***redacted***"
    assert settings["publishable_key"] == "***redacted***"
    assert settings["statement_descriptor"] == "BULLFROG"


def test_stripe_transactions_filters_stripe_orders():
    stripe = StripeTools(FakeClient())  # type: ignore[arg-type]
    result = stripe.recent_transactions({})
    assert result["count"] == 1
    assert result["transactions"][0]["order_id"] == 100


def test_stripe_refund_posts_refund():
    client = FakeClient()
    stripe = StripeTools(client)  # type: ignore[arg-type]
    result = stripe.refund({"order_id": 100, "amount": "10.00", "reason": "test"})
    assert result["refund"]["id"] == 500
    assert client.calls[-1][0:2] == ("POST", "wc/v3/orders/100/refunds")


# -- Plugin manager ------------------------------------------------------
def test_plugin_inventory_detects_known_plugins():
    manager = PluginManager(FakeClient())  # type: ignore[arg-type]
    inv = manager.inventory()
    keys = {d["key"] for d in inv["detected_capabilities"]}
    assert "woocommerce" in keys and "yoast" in keys
    assert "wc/v3" in inv["available_namespaces"]
    assert inv["plugin_count"] == 3


def test_seo_plugin_detection():
    manager = PluginManager(FakeClient())  # type: ignore[arg-type]
    assert manager.seo_plugin() == "yoast"


# -- WordPressTools dispatch + SEO bulk ---------------------------------
def test_tools_dispatch_woocommerce_read(fake_tools):
    action = PlannedAction(
        operation=ChangeOperation.WOOCOMMERCE_READ,
        title="List products",
        description="",
        payload={"action": "list_products"},
    )
    result = fake_tools.execute(action)
    assert result.success is True
    assert result.data["count"] == 2


def test_seo_plugin_bulk_writes_yoast_meta(fake_tools):
    action = PlannedAction(
        operation=ChangeOperation.SEO_PLUGIN_BULK,
        title="Bulk SEO",
        description="",
        payload={"targets": "posts", "ids": [7]},
        risk=RiskLevel.MEDIUM,
    )
    result = fake_tools.execute(action)
    assert result.success is True
    client = fake_tools.client
    assert client.updated_posts  # type: ignore[attr-defined]
    _, fields = client.updated_posts[-1]  # type: ignore[attr-defined]
    assert "_yoast_wpseo_metadesc" in fields["meta"]


# -- Safety --------------------------------------------------------------
def test_woocommerce_write_requires_confirmation_and_backup():
    safety = SafetyLayer()
    action = PlannedAction(
        operation=ChangeOperation.WOOCOMMERCE_WRITE,
        title="Create product",
        description="",
        payload={"action": "create_product"},
        risk=RiskLevel.HIGH,
    )
    decision = safety.evaluate([action], user_roles=["administrator"], app_role="admin")
    assert decision.requires_confirmation is True
    assert decision.requires_backup is True
    assert decision.allowed is True


def test_woocommerce_read_is_allowed_for_viewer():
    safety = SafetyLayer()
    action = PlannedAction(
        operation=ChangeOperation.WOOCOMMERCE_READ,
        title="List products",
        description="",
        payload={"action": "list_products"},
    )
    decision = safety.evaluate([action], user_roles=["shop_manager"], app_role="viewer")
    assert decision.requires_confirmation is False
    assert decision.allowed is True


def test_woocommerce_write_blocked_for_viewer_app_role():
    safety = SafetyLayer()
    action = PlannedAction(
        operation=ChangeOperation.WOOCOMMERCE_WRITE,
        title="Delete product",
        description="",
        payload={"action": "delete_product", "id": 1},
        risk=RiskLevel.HIGH,
    )
    decision = safety.evaluate([action], user_roles=["administrator"], app_role="viewer")
    assert decision.allowed is False
