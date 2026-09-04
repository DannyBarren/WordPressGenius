"""Encrypted, file-backed storage for user-selected LLM provider settings.

The settings page lets a user choose OpenAI or Anthropic, enter an API key, and
pick a model. Keys are encrypted at rest with the same Fernet key used for the
WordPress credential vault (``CREDENTIAL_ENCRYPTION_KEY``).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.fernet import InvalidToken

from core.llm import DEFAULT_MODELS, SUPPORTED_PROVIDERS, normalize_provider
from core.security import make_fernet


@dataclass(frozen=True)
class LLMSettings:
    """Resolved active LLM configuration."""

    provider: str
    model: str
    api_key: str
    agentic: bool = True

    @property
    def configured(self) -> bool:
        return bool(self.api_key)


class LLMSettingsStore:
    """Encrypted store holding the active provider and per-provider key/model."""

    def __init__(self, path: Path, key: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fernet = make_fernet(key)

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

    def active_provider(self) -> str | None:
        data = self.load()
        provider = data.get("provider")
        if provider in SUPPORTED_PROVIDERS:
            return provider
        return None

    def provider_config(self, provider: str) -> dict[str, str]:
        provider = normalize_provider(provider)
        providers = self.load().get("providers", {})
        record = providers.get(provider, {}) if isinstance(providers, dict) else {}
        return {
            "api_key": str(record.get("api_key", "")),
            "model": str(record.get("model", "")),
        }

    def has_key(self, provider: str) -> bool:
        return bool(self.provider_config(provider).get("api_key"))

    def agentic_enabled(self, default: bool = True) -> bool:
        data = self.load()
        value = data.get("agentic")
        return bool(value) if isinstance(value, bool) else default

    def set_agentic(self, enabled: bool) -> None:
        data = self.load()
        data["agentic"] = bool(enabled)
        self._write(data)

    def save_provider(
        self,
        provider: str,
        *,
        api_key: str | None = None,
        model: str | None = None,
        set_active: bool = True,
    ) -> None:
        """Persist provider settings.

        When *api_key* is None the existing stored key is preserved (so users can
        update the model without retyping the key). An empty string clears it.
        """

        provider = normalize_provider(provider)
        data = self.load()
        providers = data.get("providers")
        if not isinstance(providers, dict):
            providers = {}
        record = providers.get(provider, {})
        if not isinstance(record, dict):
            record = {}

        if api_key is not None:
            record["api_key"] = api_key
        if model is not None:
            record["model"] = model or DEFAULT_MODELS[provider]
        record.setdefault("model", DEFAULT_MODELS[provider])
        record.setdefault("api_key", "")

        providers[provider] = record
        data["providers"] = providers
        if set_active:
            data["provider"] = provider
        self._write(data)

    def clear_provider(self, provider: str) -> None:
        provider = normalize_provider(provider)
        data = self.load()
        providers = data.get("providers", {})
        if isinstance(providers, dict) and provider in providers:
            providers.pop(provider, None)
            data["providers"] = providers
            self._write(data)

    def _write(self, data: dict[str, Any]) -> None:
        encoded = json.dumps(data, sort_keys=True).encode("utf-8")
        self.path.write_bytes(self.fernet.encrypt(encoded))
        try:
            self.path.chmod(0o600)
        except OSError:
            pass


def resolve_active_settings(config: Any) -> LLMSettings:
    """Merge stored UI settings over environment defaults to pick the active LLM.

    Precedence: settings saved in the UI win; otherwise fall back to env config.
    """

    store = LLMSettingsStore(config.llm_settings_path, config.credential_key)
    env_defaults = {
        "openai": {"api_key": config.openai_api_key, "model": config.openai_model},
        "anthropic": {"api_key": config.anthropic_api_key, "model": config.anthropic_model},
        "groq": {
            "api_key": getattr(config, "groq_api_key", ""),
            "model": getattr(config, "groq_model", ""),
        },
        "gemini": {
            "api_key": getattr(config, "gemini_api_key", ""),
            "model": getattr(config, "gemini_model", ""),
        },
    }

    provider = store.active_provider() or normalize_provider(config.llm_provider)

    stored = store.provider_config(provider)
    defaults = env_defaults.get(provider, {})
    api_key = stored.get("api_key") or defaults.get("api_key", "")
    model = stored.get("model") or defaults.get("model", "") or DEFAULT_MODELS[provider]
    agentic = store.agentic_enabled(default=getattr(config, "llm_agentic_enabled", True))

    return LLMSettings(provider=provider, model=model, api_key=api_key, agentic=agentic)
