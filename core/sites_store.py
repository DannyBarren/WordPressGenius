"""Encrypted, per-user storage of saved WordPress sites.

Each app user can save multiple WordPress sites (site URL + username +
Application Password) under friendly labels and pick an active one from the
"Select site" page. Secrets are encrypted at rest with the same Fernet key used
for the credential vault (``CREDENTIAL_ENCRYPTION_KEY``).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from cryptography.fernet import InvalidToken
from pydantic import BaseModel, Field, HttpUrl

from core.models import WordPressCredentials
from core.security import AuthenticatedUser, make_fernet


class SavedSite(BaseModel):
    """A single stored WordPress site for a user."""

    id: str = Field(default_factory=lambda: uuid4().hex)
    label: str = Field(min_length=1)
    site_url: HttpUrl
    username: str = Field(min_length=1)
    application_password: str = Field(min_length=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def credentials(self) -> WordPressCredentials:
        return WordPressCredentials(
            site_url=self.site_url,
            username=self.username,
            application_password=self.application_password,
        )


class SiteVault:
    """Encrypted per-user collection of saved WordPress sites."""

    def __init__(self, path: Path, key: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fernet = make_fernet(key)

    def list_sites(self, user: AuthenticatedUser) -> list[SavedSite]:
        record = self._user_record(self.load(), user.username)
        sites = [SavedSite.model_validate(site) for site in record.get("sites", {}).values()]
        return sorted(sites, key=lambda site: (site.label.lower(), site.created_at))

    def get_site(self, user: AuthenticatedUser, site_id: str) -> SavedSite | None:
        record = self._user_record(self.load(), user.username)
        raw = record.get("sites", {}).get(site_id)
        return SavedSite.model_validate(raw) if raw else None

    def add_site(
        self,
        user: AuthenticatedUser,
        *,
        label: str,
        credentials: WordPressCredentials,
        set_active: bool = True,
    ) -> SavedSite:
        site = SavedSite(
            label=label.strip() or str(credentials.site_url),
            site_url=credentials.site_url,
            username=credentials.username,
            application_password=credentials.application_password,
        )
        data = self.load()
        record = self._user_record(data, user.username)
        record.setdefault("sites", {})[site.id] = site.model_dump(mode="json")
        if set_active or not record.get("active"):
            record["active"] = site.id
        data[user.username] = record
        self._write(data)
        return site

    def delete_site(self, user: AuthenticatedUser, site_id: str) -> None:
        data = self.load()
        record = self._user_record(data, user.username)
        sites = record.get("sites", {})
        if site_id in sites:
            sites.pop(site_id, None)
            if record.get("active") == site_id:
                record["active"] = next(iter(sites), None)
            data[user.username] = record
            self._write(data)

    def set_active(self, user: AuthenticatedUser, site_id: str) -> None:
        data = self.load()
        record = self._user_record(data, user.username)
        if site_id in record.get("sites", {}):
            record["active"] = site_id
            data[user.username] = record
            self._write(data)

    def get_active_id(self, user: AuthenticatedUser) -> str | None:
        record = self._user_record(self.load(), user.username)
        active = record.get("active")
        if active and active in record.get("sites", {}):
            return active
        return None

    def get_active(self, user: AuthenticatedUser) -> SavedSite | None:
        active_id = self.get_active_id(user)
        return self.get_site(user, active_id) if active_id else None

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            decrypted = self.fernet.decrypt(self.path.read_bytes())
        except InvalidToken:
            return {}
        try:
            data = json.loads(decrypted.decode("utf-8"))
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _user_record(data: dict[str, Any], username: str) -> dict[str, Any]:
        record = data.get(username)
        if not isinstance(record, dict):
            return {"sites": {}, "active": None}
        record.setdefault("sites", {})
        record.setdefault("active", None)
        return record

    def _write(self, data: dict[str, Any]) -> None:
        encoded = json.dumps(data, sort_keys=True, default=str).encode("utf-8")
        self.path.write_bytes(self.fernet.encrypt(encoded))
        try:
            self.path.chmod(0o600)
        except OSError:
            pass
