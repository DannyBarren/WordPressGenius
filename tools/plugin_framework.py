"""Extensible plugin interaction framework.

A small router that maps ``(plugin, action)`` requests to a registered
:class:`PluginHandler`. Each handler declares its read-only and write actions,
detects whether the plugin is active (via ``/wp/v2/plugins`` and REST namespace
discovery), and implements the actual REST calls with graceful degradation.

Adding support for a new plugin means writing one handler module and registering
it in :func:`build_default_handlers` - no changes to the agent graph, the
operation enum, or the safety layer are required.

Safety model:
- Read actions never modify the site and are exposed through ``PLUGIN_READ``.
- Write/trigger actions are exposed through ``PLUGIN_ACTION`` and are therefore
  confirmation-, backup-, and role-gated by ``core.safety``.
"""

from __future__ import annotations

import logging
from typing import Any

from tools.wordpress_client import WordPressClient, WordPressClientError

LOGGER = logging.getLogger(__name__)


def find_plugin(plugins: list[dict[str, Any]], fragments: tuple[str, ...]) -> dict[str, Any] | None:
    """Return the first installed plugin whose metadata matches *fragments*."""

    for plugin in plugins:
        haystack = " ".join(
            str(plugin.get(key, "")).lower() for key in ("plugin", "textdomain", "name", "slug")
        )
        if any(fragment in haystack for fragment in fragments):
            return plugin
    return None


def graceful_get(
    client: WordPressClient, endpoint: str, *, params: dict[str, Any] | None = None
) -> tuple[Any, str | None]:
    """GET an endpoint, returning ``(data, None)`` or ``(None, error_message)``.

    This lets handlers degrade gracefully when a plugin does not expose a public
    REST route (common for security, cache, and backup plugins).
    """

    try:
        return client.request_api(endpoint, params=params), None
    except WordPressClientError as exc:
        LOGGER.info("Plugin endpoint unavailable (%s): %s", endpoint, exc)
        return None, str(exc)


class PluginHandler:
    """Base class for a single plugin's capabilities."""

    key: str = ""
    name: str = ""
    category: str = ""
    slug_fragments: tuple[str, ...] = ()
    namespaces: tuple[str, ...] = ()
    read_actions: dict[str, str] = {}
    write_actions: dict[str, str] = {}

    def __init__(self, client: WordPressClient) -> None:
        self.client = client

    def detect(self, plugins: list[dict[str, Any]], namespaces: list[str]) -> dict[str, Any]:
        match = find_plugin(plugins, self.slug_fragments)
        namespace_active = any(ns in namespaces for ns in self.namespaces)
        active = bool((match and match.get("status") == "active") or namespace_active)
        return {
            "key": self.key,
            "name": self.name,
            "category": self.category,
            "active": active,
            "installed": match is not None or namespace_active,
            "version": (match or {}).get("version"),
            "read_actions": sorted(self.read_actions),
            "write_actions": sorted(self.write_actions),
        }

    def read(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        raise WordPressClientError(f"{self.name} has no read action '{action}'.")

    def write(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        raise WordPressClientError(f"{self.name} does not support changes through WordPressGenius.")

    # Helpers shared by concrete handlers -------------------------------
    def _advisory(self, summary: str, **extra: Any) -> dict[str, Any]:
        result = {"summary": summary, "available": False}
        result.update(extra)
        return result


def build_default_handlers(client: WordPressClient) -> list[PluginHandler]:
    """Instantiate every bundled plugin handler.

    Imports are deferred to avoid an import cycle (handler modules import the
    :class:`PluginHandler` base from this module).
    """

    from tools.acf import AcfHandler
    from tools.elementor import ElementorHandler
    from tools.forms_plugins import FormsHandler
    from tools.maintenance_plugins import MaintenanceHandler
    from tools.security_plugins import SecurityHandler
    from tools.seo_advanced import SeoAdvancedHandler
    from tools.woocommerce_advanced import WooCommerceAdvancedHandler

    handler_classes = [
        WooCommerceAdvancedHandler,
        ElementorHandler,
        SeoAdvancedHandler,
        SecurityHandler,
        FormsHandler,
        MaintenanceHandler,
        AcfHandler,
    ]
    return [handler_cls(client) for handler_cls in handler_classes]


class PluginFramework:
    """Detect plugins and route capability requests to their handlers."""

    def __init__(self, client: WordPressClient, handlers: list[PluginHandler] | None = None) -> None:
        self.client = client
        handler_list = handlers if handlers is not None else build_default_handlers(client)
        self.handlers: dict[str, PluginHandler] = {handler.key: handler for handler in handler_list}
        self._plugins: list[dict[str, Any]] | None = None
        self._namespaces: list[str] | None = None

    # -- detection -------------------------------------------------------
    def installed_plugins(self) -> list[dict[str, Any]]:
        if self._plugins is None:
            try:
                plugins = self.client.list_plugins()
            except WordPressClientError as exc:
                LOGGER.info("Plugin listing unavailable: %s", exc)
                plugins = []
            self._plugins = plugins if isinstance(plugins, list) else []
        return self._plugins

    def namespaces(self) -> list[str]:
        if self._namespaces is None:
            data, _ = graceful_get(self.client, "")
            namespaces = data.get("namespaces", []) if isinstance(data, dict) else []
            self._namespaces = [str(ns) for ns in namespaces] if isinstance(namespaces, list) else []
        return self._namespaces

    def detect_all(self) -> list[dict[str, Any]]:
        plugins = self.installed_plugins()
        namespaces = self.namespaces()
        return [handler.detect(plugins, namespaces) for handler in self.handlers.values()]

    def catalog(self, *, active_only: bool = False) -> list[dict[str, Any]]:
        detected = self.detect_all()
        if active_only:
            detected = [item for item in detected if item["active"]]
        return detected

    def supported_plugins(self) -> list[str]:
        return sorted(self.handlers)

    def describe_for_prompt(self) -> str:
        lines: list[str] = []
        for handler in self.handlers.values():
            reads = ", ".join(sorted(handler.read_actions)) or "-"
            writes = ", ".join(sorted(handler.write_actions)) or "-"
            lines.append(f"{handler.key} ({handler.name}): read[{reads}] write[{writes}]")
        return "\n".join(lines)

    # -- routing ---------------------------------------------------------
    def _handler(self, plugin: str) -> PluginHandler:
        if not plugin:
            raise WordPressClientError(
                f"Tell me which plugin to use. Supported: {', '.join(self.supported_plugins())}."
            )
        handler = self.handlers.get(plugin.lower())
        if handler is None:
            raise WordPressClientError(
                f"Plugin '{plugin}' is not supported yet. Supported: {', '.join(self.supported_plugins())}."
            )
        return handler

    def read(self, plugin: str, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        handler = self._handler(plugin)
        action = (action or "").lower()
        if action not in handler.read_actions:
            valid = ", ".join(sorted(handler.read_actions)) or "none"
            raise WordPressClientError(
                f"{handler.name} has no read action '{action}'. Available reads: {valid}."
            )
        return handler.read(action, payload)

    def write(self, plugin: str, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        handler = self._handler(plugin)
        action = (action or "").lower()
        if action not in handler.write_actions:
            valid = ", ".join(sorted(handler.write_actions)) or "none"
            raise WordPressClientError(
                f"{handler.name} has no change action '{action}'. Available changes: {valid}."
            )
        return handler.write(action, payload)
