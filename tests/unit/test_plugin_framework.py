from __future__ import annotations

import pytest

from agents.crew import _detect_plugin_request, _plan_actions
from core.models import ChangeOperation, PlannedAction, RiskLevel
from core.safety import SafetyLayer
from tools.backups import BackupManager
from tools.plugin_framework import PluginFramework
from tools.wordpress_client import WordPressClientError
from tools.wordpress_tools import WordPressTools


class FrameworkFakeClient:
    """Fake REST client that serves known plugin endpoints and 404s others."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict | None]] = []

    def list_plugins(self):
        return [
            {"plugin": "woocommerce/woocommerce.php", "name": "WooCommerce", "status": "active", "version": "8.0"},
            {"plugin": "elementor/elementor.php", "name": "Elementor", "status": "active", "version": "3.2"},
            {"plugin": "wordpress-seo/wp-seo.php", "name": "Yoast SEO", "status": "active", "version": "22.0"},
            {"plugin": "wordfence/wordfence.php", "name": "Wordfence Security", "status": "active", "version": "7.0"},
            {"plugin": "gravityforms/gravityforms.php", "name": "Gravity Forms", "status": "active", "version": "2.7"},
            {"plugin": "litespeed-cache/litespeed-cache.php", "name": "LiteSpeed Cache", "status": "active", "version": "5.0"},
            {"plugin": "updraftplus/updraftplus.php", "name": "UpdraftPlus", "status": "active", "version": "1.2"},
            {"plugin": "advanced-custom-fields/acf.php", "name": "ACF", "status": "active", "version": "6.0"},
        ]

    def request_api(self, endpoint, *, method="GET", params=None, json_body=None):
        self.calls.append((method, endpoint, json_body))
        if endpoint == "":
            return {"namespaces": ["wp/v2", "wc/v3", "elementor/v1", "yoast/v1", "gf/v2", "litespeed/v1", "acf/v3"]}
        if endpoint == "wp/v2/elementor_library" and method == "GET":
            return [
                {"id": 10, "title": {"rendered": "Hero"}, "status": "publish", "meta": {"_elementor_template_type": "section"}},
                {"id": 11, "title": {"rendered": "Footer"}, "status": "publish", "meta": {"_elementor_template_type": "footer"}},
            ]
        if endpoint == "wp/v2/elementor_library/10" and method == "GET":
            return {"id": 10, "title": {"rendered": "Hero"}, "status": "publish",
                    "content": {"raw": "<div></div>"}, "meta": {"_elementor_data": "[]", "_elementor_edit_mode": "builder"}}
        if endpoint == "wp/v2/elementor_library" and method == "POST":
            return {"id": 99, "status": "draft", **json_body}
        if endpoint == "wc/v3/reports/sales":
            return [{"total_sales": "1200.00", "net_sales": "1100.00", "total_orders": 30, "total_items": 75}]
        if endpoint == "wc/v3/reports/top_sellers":
            return [{"name": "Whey", "quantity": 40}]
        if endpoint == "gf/v2/forms":
            return [{"id": 1, "title": "Contact"}, {"id": 2, "title": "Newsletter"}]
        if endpoint == "wp/v2/posts" and method == "GET":
            return [
                {"id": 7, "title": {"rendered": "Guide"}, "content": {"rendered": "body"}, "excerpt": {"rendered": ""}},
            ]
        if endpoint == "wp/v2/acf-field-group":
            return [{"id": 3, "title": {"rendered": "Product Specs"}, "status": "publish"}]
        if endpoint == "litespeed/v1/tool/purge_all":
            return {}
        if endpoint == "wordfence/v1/scan" and method == "POST":
            return {}
        raise WordPressClientError(f"404 for {endpoint}")


@pytest.fixture
def framework():
    return PluginFramework(FrameworkFakeClient())  # type: ignore[arg-type]


@pytest.fixture
def fw_tools(tmp_path):
    return WordPressTools(FrameworkFakeClient(), BackupManager(tmp_path / "backups"))  # type: ignore[arg-type]


# -- detection / catalog -------------------------------------------------
def test_catalog_detects_active_handlers(framework):
    catalog = {item["key"]: item for item in framework.catalog()}
    assert catalog["woocommerce"]["active"] is True
    assert catalog["elementor"]["active"] is True
    assert catalog["seo"]["active"] is True
    assert "list_templates" in catalog["elementor"]["read_actions"]


def test_supported_plugins(framework):
    assert set(framework.supported_plugins()) == {
        "woocommerce", "elementor", "seo", "security", "forms", "maintenance", "acf"
    }


# -- routing validation --------------------------------------------------
def test_unknown_plugin_raises(framework):
    with pytest.raises(WordPressClientError):
        framework.read("nonexistent", "list", {})


def test_unknown_action_raises(framework):
    with pytest.raises(WordPressClientError):
        framework.read("elementor", "delete_everything", {})


def test_write_action_rejected_as_read(framework):
    # duplicate_template is a write action; calling it via read must fail.
    with pytest.raises(WordPressClientError):
        framework.read("elementor", "duplicate_template", {"id": 10})


# -- handler reads -------------------------------------------------------
def test_elementor_list_templates(framework):
    result = framework.read("elementor", "list_templates", {})
    assert result["count"] == 2
    assert result["templates"][0]["title"] == "Hero"


def test_elementor_duplicate_creates_draft(framework):
    result = framework.write("elementor", "duplicate_template", {"id": 10})
    assert result["template"]["status"] == "draft"
    assert "(copy)" in result["template"]["title"]


def test_woocommerce_sales_report(framework):
    result = framework.read("woocommerce", "sales_report", {})
    assert result["total_orders"] == 30
    assert result["top_sellers"][0]["name"] == "Whey"


def test_seo_schema_summary_detects_yoast(framework):
    result = framework.read("seo", "schema_summary", {})
    assert result["seo_plugin"] == "yoast"
    assert "Article" in result["schema_types"]


def test_seo_audit_flags_missing_meta(framework):
    result = framework.read("seo", "audit", {"targets": "posts"})
    assert result["checked"] == 1
    assert result["needs_attention"][0]["id"] == 7


def test_security_summary_reports_wordfence(framework):
    result = framework.read("security", "security_summary", {})
    assert "wordfence" in result["active_plugins"]


def test_security_scan_trigger(framework):
    result = framework.write("security", "start_scan", {})
    assert result.get("started") is True


def test_forms_list(framework):
    result = framework.read("forms", "list_forms", {})
    assert result["count"] == 2
    assert result["forms"][0]["plugin"] == "gravity"


def test_maintenance_clear_cache_litespeed(framework):
    result = framework.write("maintenance", "clear_cache", {})
    assert result.get("cleared") is True


def test_maintenance_trigger_backup_degrades_gracefully(framework):
    result = framework.write("maintenance", "trigger_backup", {})
    assert result["available"] is False
    assert "updraftplus" in result["active_plugins"]


def test_acf_field_groups(framework):
    result = framework.read("acf", "field_groups", {})
    assert result["count"] == 1
    assert result["field_groups"][0]["title"] == "Product Specs"


# -- WordPressTools dispatch + inventory --------------------------------
def test_tools_dispatch_plugin_read(fw_tools):
    action = PlannedAction(
        operation=ChangeOperation.PLUGIN_READ,
        title="List templates",
        description="",
        payload={"plugin": "elementor", "action": "list_templates"},
    )
    result = fw_tools.execute(action)
    assert result.success is True
    assert result.data["count"] == 2


def test_tools_dispatch_plugin_action_requires_plugin(fw_tools):
    action = PlannedAction(
        operation=ChangeOperation.PLUGIN_ACTION,
        title="Bad",
        description="",
        payload={},
        risk=RiskLevel.HIGH,
    )
    result = fw_tools.execute(action)
    assert result.success is False
    assert "which plugin" in result.message.lower()


def test_plugin_inventory_includes_framework_capabilities(fw_tools):
    action = PlannedAction(
        operation=ChangeOperation.PLUGIN_INVENTORY,
        title="Inventory",
        description="",
        payload={},
    )
    result = fw_tools.execute(action)
    assert result.success is True
    assert result.data["framework_capabilities"]


# -- safety --------------------------------------------------------------
def test_plugin_action_requires_confirmation_and_backup():
    safety = SafetyLayer()
    action = PlannedAction(
        operation=ChangeOperation.PLUGIN_ACTION,
        title="Clear cache",
        description="",
        payload={"plugin": "maintenance", "action": "clear_cache"},
        risk=RiskLevel.HIGH,
    )
    decision = safety.evaluate([action], user_roles=["administrator"], app_role="editor")
    assert decision.requires_confirmation is True
    assert decision.requires_backup is True
    assert decision.allowed is True


def test_plugin_read_is_read_only_for_viewer():
    safety = SafetyLayer()
    action = PlannedAction(
        operation=ChangeOperation.PLUGIN_READ,
        title="Templates",
        description="",
        payload={"plugin": "elementor", "action": "list_templates"},
    )
    decision = safety.evaluate([action], user_roles=["subscriber"], app_role="viewer")
    assert decision.requires_confirmation is False
    assert decision.allowed is True


def test_plugin_action_blocked_for_viewer_app_role():
    safety = SafetyLayer()
    action = PlannedAction(
        operation=ChangeOperation.PLUGIN_ACTION,
        title="Start scan",
        description="",
        payload={"plugin": "security", "action": "start_scan"},
        risk=RiskLevel.HIGH,
    )
    decision = safety.evaluate([action], user_roles=["administrator"], app_role="viewer")
    assert decision.allowed is False


# -- planner detection ---------------------------------------------------
def _only(request: str) -> PlannedAction:
    actions = _plan_actions(request)
    assert len(actions) == 1
    return actions[0]


def test_planner_list_elementor_templates():
    action = _only("list elementor templates")
    assert action.operation == ChangeOperation.PLUGIN_READ
    assert action.payload == {"plugin": "elementor", "action": "list_templates"}


def test_planner_duplicate_elementor_template():
    action = _only("duplicate elementor template #5")
    assert action.operation == ChangeOperation.PLUGIN_ACTION
    assert action.payload == {"plugin": "elementor", "action": "duplicate_template", "id": 5}
    assert action.requires_confirmation is True


def test_planner_run_security_scan():
    action = _only("run a security scan")
    assert action.operation == ChangeOperation.PLUGIN_ACTION
    assert action.payload["plugin"] == "security"
    assert action.payload["action"] == "start_scan"


def test_planner_clear_cache():
    action = _only("clear the cache")
    assert action.operation == ChangeOperation.PLUGIN_ACTION
    assert action.payload == {"plugin": "maintenance", "action": "clear_cache"}


def test_planner_cache_status_is_read():
    action = _only("what is my cache status")
    assert action.operation == ChangeOperation.PLUGIN_READ
    assert action.payload["action"] == "cache_status"


def test_planner_trigger_backup():
    action = _only("trigger an updraftplus backup")
    assert action.operation == ChangeOperation.PLUGIN_ACTION
    assert action.payload == {"plugin": "maintenance", "action": "trigger_backup"}


def test_planner_list_acf_field_groups():
    action = _only("list my acf field groups")
    assert action.operation == ChangeOperation.PLUGIN_READ
    assert action.payload["plugin"] == "acf"


def test_planner_form_submissions():
    action = _only("show me recent form submissions")
    assert action.operation == ChangeOperation.PLUGIN_READ
    assert action.payload["plugin"] == "forms"
    assert action.payload["action"] == "list_entries"


def test_planner_sales_report():
    action = _only("give me a woocommerce sales report")
    assert action.operation == ChangeOperation.PLUGIN_READ
    assert action.payload == {"plugin": "woocommerce", "action": "sales_report"}


def test_planner_set_schema_is_write():
    action = _only("set schema to Article for all products")
    assert action.operation == ChangeOperation.PLUGIN_ACTION
    assert action.payload["plugin"] == "seo"
    assert action.payload["action"] == "bulk_optimize"
    assert action.payload["targets"] == "products"


def test_detect_plugin_request_returns_none_for_generic():
    assert _detect_plugin_request("write a blog post about coffee", "write a blog post about coffee") is None
