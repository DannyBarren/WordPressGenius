"""Scalable plugin detection and capability framework.

Detects installed plugins via ``/wp/v2/plugins``, discovers the site's available
REST namespaces, and maps well-known plugins to a capability descriptor so the
agents know what is possible and which endpoints to use. This makes adding
support for a new plugin a matter of extending ``KNOWN_PLUGINS`` rather than
changing the agent graph.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from tools.wordpress_client import WordPressClient, WordPressClientError

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class PluginCapability:
    """Describes a known plugin and what WordPressGenius can do with it."""

    key: str
    name: str
    category: str
    slug_fragments: tuple[str, ...]
    rest_namespaces: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    notes: str = ""


# Registry of well-known plugins. Extend this list to support more plugins;
# no changes to the agent graph are required.
KNOWN_PLUGINS: tuple[PluginCapability, ...] = (
    PluginCapability(
        key="woocommerce",
        name="WooCommerce",
        category="ecommerce",
        slug_fragments=("woocommerce",),
        rest_namespaces=("wc/v3", "wc/store/v1"),
        capabilities=(
            "Full product CRUD and bulk price/stock/status updates",
            "Order management (view, update status, refund via Stripe)",
            "Inventory, variations, categories, tags, and redacted customer reads",
        ),
        notes="Read products/orders freely; writes require approval and a backup.",
    ),
    PluginCapability(
        key="stripe",
        name="WooCommerce Stripe Gateway",
        category="payments",
        slug_fragments=("woocommerce-gateway-stripe", "stripe", "woocommerce-payments"),
        rest_namespaces=("wc/v3",),
        capabilities=(
            "Detect gateway status and test/live mode",
            "Summarize settings with keys redacted",
            "List recent Stripe transactions (read-only) and process approved refunds",
        ),
        notes="Works through WooCommerce; never needs your Stripe secret key.",
    ),
    PluginCapability(
        key="yoast",
        name="Yoast SEO",
        category="seo",
        slug_fragments=("wordpress-seo", "yoast"),
        rest_namespaces=("yoast/v1",),
        capabilities=("Bulk SEO metadata suggestions and updates for posts/products",),
        notes="Bulk meta updates are approval-gated.",
    ),
    PluginCapability(
        key="rankmath",
        name="Rank Math SEO",
        category="seo",
        slug_fragments=("seo-by-rank-math", "rank-math", "rankmath"),
        rest_namespaces=("rankmath/v1",),
        capabilities=("Bulk SEO metadata suggestions and updates for posts/products",),
        notes="Bulk meta updates are approval-gated.",
    ),
    PluginCapability(
        key="elementor",
        name="Elementor",
        category="page_builder",
        slug_fragments=("elementor",),
        rest_namespaces=("elementor/v1",),
        capabilities=("List templates/library items and summarize page-builder usage",),
        notes="Deep template edits should be done in the Elementor editor.",
    ),
    PluginCapability(
        key="contact_form_7",
        name="Contact Form 7",
        category="forms",
        slug_fragments=("contact-form-7",),
        rest_namespaces=("contact-form-7/v1",),
        capabilities=("List forms, troubleshoot delivery, and summarize settings",),
        notes="Submissions are not stored by CF7 unless a logging add-on is active.",
    ),
    PluginCapability(
        key="jetpack",
        name="Jetpack",
        category="platform",
        slug_fragments=("jetpack",),
        rest_namespaces=("jetpack/v4",),
        capabilities=("Summarize connection status and active modules",),
    ),
    PluginCapability(
        key="akismet",
        name="Akismet Anti-Spam",
        category="security",
        slug_fragments=("akismet",),
        capabilities=("Confirm spam protection is active and report key status",),
    ),
    PluginCapability(
        key="updraftplus",
        name="UpdraftPlus Backups",
        category="backup",
        slug_fragments=("updraftplus",),
        capabilities=("Confirm a backup plugin is active and advise on schedules",),
        notes="Use UpdraftPlus for full-site backups; WordPressGenius backs up only changed items.",
    ),
    PluginCapability(
        key="wpforms",
        name="WPForms",
        category="forms",
        slug_fragments=("wpforms",),
        capabilities=("Detect forms plugin and advise on troubleshooting",),
    ),
)


@dataclass
class PluginInventory:
    plugins: list[dict[str, Any]] = field(default_factory=list)
    detected: list[dict[str, Any]] = field(default_factory=list)
    namespaces: list[str] = field(default_factory=list)
    summary: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "plugin_count": len(self.plugins),
            "detected_capabilities": self.detected,
            "available_namespaces": self.namespaces,
            "plugins": self.plugins[:50],
        }


class PluginManager:
    """Detect plugins and map them to known capabilities and REST endpoints."""

    def __init__(self, client: WordPressClient) -> None:
        self.client = client

    def list_plugins(self) -> list[dict[str, Any]]:
        try:
            plugins = self.client.list_plugins()
        except WordPressClientError as exc:
            LOGGER.info("Plugin listing unavailable: %s", exc)
            return []
        return plugins if isinstance(plugins, list) else []

    def discover_namespaces(self) -> list[str]:
        try:
            index = self.client.request_api("")
        except WordPressClientError:
            return []
        if isinstance(index, dict):
            namespaces = index.get("namespaces", [])
            if isinstance(namespaces, list):
                return [str(ns) for ns in namespaces]
        return []

    def match_capabilities(self, plugins: list[dict[str, Any]]) -> list[dict[str, Any]]:
        detected: list[dict[str, Any]] = []
        for capability in KNOWN_PLUGINS:
            match = _find_plugin(plugins, capability.slug_fragments)
            if match is None:
                continue
            detected.append(
                {
                    "key": capability.key,
                    "name": capability.name,
                    "category": capability.category,
                    "status": match.get("status", "unknown"),
                    "version": match.get("version"),
                    "rest_namespaces": list(capability.rest_namespaces),
                    "capabilities": list(capability.capabilities),
                    "notes": capability.notes,
                }
            )
        return detected

    def inventory(self) -> dict[str, Any]:
        plugins = self.list_plugins()
        brief = [
            {
                "name": p.get("name"),
                "slug": p.get("plugin") or p.get("textdomain"),
                "status": p.get("status"),
                "version": p.get("version"),
            }
            for p in plugins
        ]
        detected = self.match_capabilities(plugins)
        namespaces = self.discover_namespaces()
        active_known = [d["name"] for d in detected if d.get("status") == "active"]
        if not plugins and not namespaces:
            summary = (
                "Could not list plugins via REST (the user may lack permission). "
                "Detected REST namespaces are unavailable too."
            )
        else:
            summary = (
                f"Found {len(plugins)} plugin(s); recognized {len(detected)} with built-in "
                f"capabilities. Active recognized plugins: {', '.join(active_known) or 'none'}."
            )
        inv = PluginInventory(plugins=brief, detected=detected, namespaces=namespaces, summary=summary)
        result = inv.as_dict()
        result["recommended_next_steps"] = _recommendations(detected)
        return result

    def seo_plugin(self) -> str | None:
        """Return 'yoast' or 'rankmath' if a known SEO plugin is active."""

        plugins = self.list_plugins()
        for key in ("yoast", "rankmath"):
            capability = next(c for c in KNOWN_PLUGINS if c.key == key)
            match = _find_plugin(plugins, capability.slug_fragments)
            if match is not None and match.get("status") == "active":
                return key
        # Fall back to namespace discovery when plugin listing is restricted.
        namespaces = self.discover_namespaces()
        if "yoast/v1" in namespaces:
            return "yoast"
        if "rankmath/v1" in namespaces:
            return "rankmath"
        return None


def _find_plugin(plugins: list[dict[str, Any]], fragments: tuple[str, ...]) -> dict[str, Any] | None:
    for plugin in plugins:
        haystack = " ".join(
            str(plugin.get(field, "")).lower()
            for field in ("plugin", "textdomain", "name", "slug")
        )
        if any(fragment in haystack for fragment in fragments):
            return plugin
    return None


def _recommendations(detected: list[dict[str, Any]]) -> list[str]:
    keys = {d["key"] for d in detected}
    tips: list[str] = []
    if "woocommerce" in keys:
        tips.append("Ask to list products/orders, adjust prices/stock, or create products (approval required).")
    if "stripe" in keys:
        tips.append("Ask for recent Stripe transactions or gateway status; refunds require approval.")
    if "yoast" in keys or "rankmath" in keys:
        tips.append("Ask to bulk-optimize SEO metadata for posts or products (approval required).")
    if "updraftplus" not in keys:
        tips.append("Consider a full-site backup plugin (e.g. UpdraftPlus) before large changes.")
    return tips
