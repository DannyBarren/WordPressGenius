"""Security plugin handler (Wordfence, Sucuri, iThemes/Solid Security).

Security plugins rarely expose public REST routes for scans, so reads provide a
detection-and-recommendation summary and the scan trigger degrades gracefully to
clear guidance when no REST endpoint is available. The scan trigger is a write
action, so it is always approval-gated.
"""

from __future__ import annotations

from typing import Any

from tools.plugin_framework import PluginHandler, find_plugin
from tools.wordpress_client import WordPressClientError

_KNOWN = {
    "wordfence": ("wordfence",),
    "sucuri": ("sucuri",),
    "solid security": ("better-wp-security", "ithemes-security", "solid-security"),
}


class SecurityHandler(PluginHandler):
    key = "security"
    name = "Security (Wordfence/Sucuri/Solid)"
    category = "security"
    slug_fragments = ("wordfence", "sucuri", "better-wp-security", "ithemes-security", "solid-security")
    namespaces = ("wordfence/v1",)
    read_actions = {
        "security_summary": "Detect active security plugins and summarize protection status.",
    }
    write_actions = {
        "start_scan": "Trigger a security scan if the plugin exposes a REST endpoint (else advise).",
    }

    def read(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._security_summary()

    def write(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._start_scan(payload)

    def _installed(self) -> list[str]:
        try:
            plugins = self.client.list_plugins()
        except WordPressClientError:
            plugins = []
        plugins = plugins if isinstance(plugins, list) else []
        found: list[str] = []
        for label, fragments in _KNOWN.items():
            match = find_plugin(plugins, fragments)
            if match and match.get("status") == "active":
                found.append(label)
        return found

    def _security_summary(self) -> dict[str, Any]:
        active = self._installed()
        if not active:
            return {
                "summary": "No common security plugin (Wordfence, Sucuri, Solid Security) appears active.",
                "active_plugins": [],
                "recommended_next_steps": [
                    "Install a firewall/malware scanner such as Wordfence or Solid Security.",
                    "Enable login protection and two-factor authentication.",
                    "Keep WordPress core, themes, and plugins updated.",
                ],
            }
        return {
            "summary": f"Active security protection: {', '.join(active)}.",
            "active_plugins": active,
            "recommended_next_steps": [
                "Confirm scheduled scans are enabled in the plugin dashboard.",
                "Review the latest scan results and firewall blocklist.",
                "Ensure two-factor authentication is enforced for admins.",
            ],
        }

    def _start_scan(self, payload: dict[str, Any]) -> dict[str, Any]:
        # Wordfence and peers do not ship a stable, documented public REST scan
        # trigger. Attempt only if a known namespace exists; otherwise advise.
        active = self._installed()
        if "wordfence" in active:
            try:
                self.client.request_api("wordfence/v1/scan", method="POST", json_body={})
                return {"summary": "Requested a Wordfence scan via REST.", "started": True}
            except WordPressClientError:
                pass
        return self._advisory(
            "This security plugin does not expose a REST scan trigger on this site. "
            "Start the scan from the plugin dashboard (e.g. Wordfence > Scan > Start New Scan), "
            "then ask me to summarize the results.",
            active_plugins=active,
        )
