"""Maintenance handler: UpdraftPlus backups and LiteSpeed/WP Rocket caching.

Backups and cache plugins generally operate over admin-ajax rather than the REST
API, so trigger actions degrade gracefully to clear guidance when no REST route
exists. Read actions detect plugins and summarize status; trigger/clear actions
are approval-gated writes.
"""

from __future__ import annotations

from typing import Any

from tools.plugin_framework import PluginHandler, find_plugin, graceful_get
from tools.wordpress_client import WordPressClientError

_BACKUP = {"updraftplus": ("updraftplus",), "backwpup": ("backwpup",), "duplicator": ("duplicator",)}
_CACHE = {"litespeed": ("litespeed-cache",), "wp_rocket": ("wp-rocket",), "w3_total_cache": ("w3-total-cache",), "wp_super_cache": ("wp-super-cache",)}


class MaintenanceHandler(PluginHandler):
    key = "maintenance"
    name = "Backups & Caching (UpdraftPlus/LiteSpeed/WP Rocket)"
    category = "maintenance"
    slug_fragments = ("updraftplus", "backwpup", "duplicator", "litespeed-cache", "wp-rocket", "w3-total-cache", "wp-super-cache")
    namespaces = ("litespeed/v1",)
    read_actions = {
        "backup_status": "Detect the backup plugin and summarize backup readiness.",
        "cache_status": "Detect the caching plugin and summarize optimization status.",
        "optimization_summary": "Summarize performance/caching and backup posture together.",
        "restore_summary": "Explain restore options for the detected backup plugin.",
    }
    write_actions = {
        "trigger_backup": "Start a backup if the plugin exposes a REST trigger (else advise).",
        "clear_cache": "Purge the site cache if the plugin exposes a REST trigger (else advise).",
    }

    def read(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        if action == "cache_status":
            return self._cache_status()
        if action == "optimization_summary":
            return self._optimization_summary()
        if action == "restore_summary":
            return self._restore_summary()
        return self._backup_status()

    def write(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        if action == "clear_cache":
            return self._clear_cache()
        return self._trigger_backup()

    # -- detection -------------------------------------------------------
    def _detect(self, registry: dict[str, tuple[str, ...]]) -> list[str]:
        try:
            plugins = self.client.list_plugins()
        except WordPressClientError:
            plugins = []
        plugins = plugins if isinstance(plugins, list) else []
        found = []
        for label, fragments in registry.items():
            match = find_plugin(plugins, fragments)
            if match and match.get("status") == "active":
                found.append(label)
        return found

    # -- reads -----------------------------------------------------------
    def _backup_status(self) -> dict[str, Any]:
        active = self._detect(_BACKUP)
        if not active:
            return {
                "summary": "No backup plugin detected. WordPressGenius backs up only the items it changes.",
                "active_plugins": [],
                "recommended_next_steps": ["Install UpdraftPlus for full-site, scheduled, off-site backups."],
            }
        return {
            "summary": f"Backup plugin active: {', '.join(active)}.",
            "active_plugins": active,
            "recommended_next_steps": [
                "Confirm a recent backup exists and is stored off-site (e.g. cloud storage).",
                "Run a fresh backup before major changes.",
            ],
        }

    def _cache_status(self) -> dict[str, Any]:
        active = self._detect(_CACHE)
        if not active:
            return {
                "summary": "No caching plugin detected. Consider one for faster page loads.",
                "active_plugins": [],
                "recommended_next_steps": ["Install LiteSpeed Cache (if on LiteSpeed) or WP Rocket for performance."],
            }
        return {
            "summary": f"Caching active: {', '.join(active)}.",
            "active_plugins": active,
            "recommended_next_steps": [
                "Clear the cache after publishing major changes.",
                "Enable page caching, minification, and lazy-loading in the plugin settings.",
            ],
        }

    def _optimization_summary(self) -> dict[str, Any]:
        cache = self._detect(_CACHE)
        backup = self._detect(_BACKUP)
        return {
            "summary": (
                f"Caching: {', '.join(cache) or 'none'}. Backups: {', '.join(backup) or 'none'}."
            ),
            "cache_plugins": cache,
            "backup_plugins": backup,
            "recommended_next_steps": [
                "Pair a caching plugin with image optimization for best speed.",
                "Schedule automatic off-site backups before relying on automation.",
            ],
        }

    def _restore_summary(self) -> dict[str, Any]:
        active = self._detect(_BACKUP)
        if not active:
            return self._advisory("No backup plugin is active, so there is nothing to restore from yet.")
        return {
            "summary": f"Restores are managed inside {', '.join(active)}; WordPressGenius does not auto-restore full sites.",
            "active_plugins": active,
            "recommended_next_steps": [
                "Open the plugin's Existing Backups tab and choose a restore point.",
                "Restore on a staging site first when possible.",
            ],
        }

    # -- writes (approval-gated) ----------------------------------------
    def _trigger_backup(self) -> dict[str, Any]:
        active = self._detect(_BACKUP)
        return self._advisory(
            "Backup plugins do not expose a stable public REST trigger. Start a backup from "
            "the plugin dashboard (e.g. UpdraftPlus > Backup Now), then I can summarize the result.",
            active_plugins=active,
        )

    def _clear_cache(self) -> dict[str, Any]:
        active = self._detect(_CACHE)
        if "litespeed" in active:
            data, error = graceful_get(self.client, "litespeed/v1/tool/purge_all")
            if error is None:
                return {"summary": "Requested a LiteSpeed full-cache purge.", "cleared": True, "active_plugins": active}
        return self._advisory(
            "This caching plugin does not expose a REST purge on this site. Use the toolbar "
            "'Purge' button or the plugin's dashboard to clear the cache.",
            active_plugins=active,
        )
