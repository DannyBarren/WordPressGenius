from __future__ import annotations

from core.models import ChangeOperation, PlannedAction, RiskLevel
from tools.backups import BackupManager
from tools.wordpress_tools import WordPressTools


class FakeClient:
    def __init__(self) -> None:
        self.updated_products: list[tuple[str, dict]] = []

    def paginated_request_api(self, endpoint: str, *, params=None, per_page=100, max_pages=10):
        return self.request_api(endpoint, params={**(params or {}), "per_page": per_page})

    def request_api(self, endpoint: str, *, method: str = "GET", params=None, json_body=None):
        if endpoint == "wc/v3/products":
            return [
                {"id": 1, "name": "Basic Plan", "regular_price": "10.00"},
                {"id": 2, "name": "No Price", "regular_price": ""},
            ]
        if endpoint == "wc/v3/products/1" and method == "PUT":
            self.updated_products.append((endpoint, json_body))
            return {"id": 1, "name": "Basic Plan", "regular_price": json_body["regular_price"]}
        raise AssertionError(f"Unexpected request_api call: {method} {endpoint}")

    def list_plugins(self):
        return [
            {
                "plugin": "contact-form-7/wp-contact-form-7.php",
                "name": "Contact Form 7",
                "status": "inactive",
                "version": "5.0",
                "new_version": "5.1",
            },
            {
                "plugin": "google-site-kit/google-site-kit.php",
                "name": "Google Site Kit",
                "status": "active",
                "version": "1.0",
            },
        ]


def test_bulk_update_products_updates_numeric_prices(tmp_path) -> None:
    client = FakeClient()
    tools = WordPressTools(client, BackupManager(tmp_path / "backups"))  # type: ignore[arg-type]
    action = PlannedAction(
        operation=ChangeOperation.BULK_UPDATE_PRODUCTS,
        title="Bulk price update",
        description="Increase prices.",
        payload={"percent": 10},
        risk=RiskLevel.HIGH,
    )

    result = tools.execute(action)

    assert result.success is True
    assert result.data["updated_count"] == 1
    assert result.data["skipped_count"] == 1
    assert client.updated_products == [("wc/v3/products/1", {"regular_price": "11.00"})]


def test_plugin_troubleshooting_surfaces_inactive_and_updates(tmp_path) -> None:
    tools = WordPressTools(FakeClient(), BackupManager(tmp_path / "backups"))  # type: ignore[arg-type]
    action = PlannedAction(
        operation=ChangeOperation.PLUGIN_TROUBLESHOOT,
        title="Troubleshoot plugin",
        description="Check plugin state.",
        payload={"plugin_slug": "contact-form"},
    )

    result = tools.execute(action)

    assert result.success is True
    assert result.message == "I reviewed plugin status and troubleshooting signals."


def test_missing_update_id_returns_safe_failure(wp_tools) -> None:
    action = PlannedAction(
        operation=ChangeOperation.UPDATE_POST,
        title="Update post",
        description="Missing ID.",
        payload={"content": "New"},
        risk=RiskLevel.MEDIUM,
    )

    result = wp_tools.execute(action)

    assert result.success is False
    assert "post ID" in result.message
