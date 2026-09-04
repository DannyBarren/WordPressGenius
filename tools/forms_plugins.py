"""Forms handler for Gravity Forms, WPForms, and Contact Form 7.

Lists forms and (where the plugin exposes it) submissions/entries, plus a
troubleshooting summary. Read-only by design: deleting or editing entries is left
to the plugin dashboards to avoid destructive automation.
"""

from __future__ import annotations

from typing import Any

from tools.plugin_framework import PluginHandler, find_plugin, graceful_get
from tools.wordpress_client import WordPressClientError

_GRAVITY = ("gravityforms", "gravity-forms")
_WPFORMS = ("wpforms",)
_CF7 = ("contact-form-7",)


class FormsHandler(PluginHandler):
    key = "forms"
    name = "Forms (Gravity/WPForms/CF7)"
    category = "forms"
    slug_fragments = _GRAVITY + _WPFORMS + _CF7
    namespaces = ("gf/v2", "contact-form-7/v1")
    read_actions = {
        "list_forms": "List forms across Gravity Forms, WPForms, and Contact Form 7.",
        "list_entries": "List recent submissions/entries (Gravity Forms; payload: id optional).",
        "troubleshoot": "Summarize likely delivery/config issues and next steps.",
    }
    write_actions: dict[str, str] = {}

    def read(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        if action == "list_entries":
            return self._list_entries(payload)
        if action == "troubleshoot":
            return self._troubleshoot()
        return self._list_forms()

    def _active(self) -> dict[str, bool]:
        try:
            plugins = self.client.list_plugins()
        except WordPressClientError:
            plugins = []
        plugins = plugins if isinstance(plugins, list) else []
        return {
            "gravity": _is_active(plugins, _GRAVITY),
            "wpforms": _is_active(plugins, _WPFORMS),
            "cf7": _is_active(plugins, _CF7),
        }

    def _list_forms(self) -> dict[str, Any]:
        forms: list[dict[str, Any]] = []
        gravity, _ = graceful_get(self.client, "gf/v2/forms")
        if isinstance(gravity, (list, dict)):
            items = gravity.values() if isinstance(gravity, dict) else gravity
            for form in items:
                if isinstance(form, dict):
                    forms.append({"plugin": "gravity", "id": form.get("id"), "title": form.get("title")})
        cf7, _ = graceful_get(self.client, "contact-form-7/v1/contact-forms")
        if isinstance(cf7, list):
            for form in cf7:
                forms.append({"plugin": "cf7", "id": form.get("id"), "title": form.get("title")})
        wpforms, _ = graceful_get(self.client, "wp/v2/wpforms", params={"per_page": 50, "status": "any"})
        if isinstance(wpforms, list):
            for form in wpforms:
                forms.append({"plugin": "wpforms", "id": form.get("id"), "title": _title(form)})
        if not forms:
            return self._advisory(
                "No forms were readable via REST. Gravity Forms needs its REST API enabled; "
                "Contact Form 7 and WPForms manage forms in their own dashboards."
            )
        return {"summary": f"Found {len(forms)} form(s) across active form plugins.", "count": len(forms), "forms": forms}

    def _list_entries(self, payload: dict[str, Any]) -> dict[str, Any]:
        params: dict[str, Any] = {"paging[page_size]": min(int(payload.get("per_page", 20) or 20), 50)}
        form_id = payload.get("id")
        endpoint = f"gf/v2/forms/{int(form_id)}/entries" if form_id else "gf/v2/entries"
        data, error = graceful_get(self.client, endpoint, params=params)
        entries = (data or {}).get("entries", data) if isinstance(data, dict) else data
        if error or not isinstance(entries, list):
            return self._advisory(
                "Submissions are only readable when Gravity Forms' REST API is enabled. "
                "Contact Form 7 does not store submissions unless a logging add-on (e.g. Flamingo) is active."
            )
        return {
            "summary": f"Found {len(entries)} recent submission(s).",
            "count": len(entries),
            "entries": [
                {"id": e.get("id"), "form_id": e.get("form_id"), "date_created": e.get("date_created")}
                for e in entries[:25]
            ],
        }

    def _troubleshoot(self) -> dict[str, Any]:
        active = self._active()
        names = [name for name, on in active.items() if on]
        return {
            "summary": (
                f"Active form plugin(s): {', '.join(names) or 'none detected'}. "
                "Most delivery issues are email related."
            ),
            "active": active,
            "recommended_next_steps": [
                "Install an SMTP plugin (e.g. WP Mail SMTP) so confirmation emails actually send.",
                "Confirm the form's notification 'To' address and that it is not going to spam.",
                "Send a real test submission and check spam protection (reCAPTCHA/Akismet) settings.",
            ],
        }


def _is_active(plugins: list[dict[str, Any]], fragments: tuple[str, ...]) -> bool:
    match = find_plugin(plugins, fragments)
    return bool(match and match.get("status") == "active")


def _title(item: dict[str, Any]) -> str:
    title = item.get("title")
    if isinstance(title, dict):
        return str(title.get("rendered") or title.get("raw") or "")
    return str(title or "")
