from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from core.llm import (
    ANTHROPIC,
    OPENAI,
    LLMClient,
    _filter_openai_chat_models,
    preferred_model,
)
from core.security import generate_fernet_key
from core.settings_store import LLMSettingsStore, resolve_active_settings


@pytest.fixture
def key() -> str:
    return generate_fernet_key()


def _config(tmp_path, **overrides):
    base = dict(
        llm_settings_path=tmp_path / "llm_settings.enc",
        credential_key=generate_fernet_key(),
        llm_provider="openai",
        openai_api_key="",
        openai_model="gpt-4o-mini",
        anthropic_api_key="",
        anthropic_model="claude-3-5-sonnet-latest",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_store_roundtrip_persists_and_encrypts(tmp_path, key) -> None:
    path = tmp_path / "llm_settings.enc"
    store = LLMSettingsStore(path, key)

    store.save_provider(ANTHROPIC, api_key="sk-ant-123", model="claude-opus-4-latest")

    # Re-open with same key to confirm persistence.
    reopened = LLMSettingsStore(path, key)
    assert reopened.active_provider() == ANTHROPIC
    cfg = reopened.provider_config(ANTHROPIC)
    assert cfg["api_key"] == "sk-ant-123"
    assert cfg["model"] == "claude-opus-4-latest"

    # The file on disk must not contain the plaintext key.
    assert b"sk-ant-123" not in path.read_bytes()


def test_save_without_key_preserves_existing(tmp_path, key) -> None:
    store = LLMSettingsStore(tmp_path / "s.enc", key)
    store.save_provider(OPENAI, api_key="sk-openai", model="gpt-4o")

    store.save_provider(OPENAI, api_key=None, model="o3")

    cfg = store.provider_config(OPENAI)
    assert cfg["api_key"] == "sk-openai"
    assert cfg["model"] == "o3"


def test_resolve_prefers_stored_over_env(tmp_path) -> None:
    config = _config(tmp_path, openai_api_key="env-openai", llm_provider="openai")
    store = LLMSettingsStore(config.llm_settings_path, config.credential_key)
    store.save_provider(ANTHROPIC, api_key="stored-anthropic", model="claude-3-5-haiku-latest")

    resolved = resolve_active_settings(config)

    assert resolved.provider == ANTHROPIC
    assert resolved.api_key == "stored-anthropic"
    assert resolved.model == "claude-3-5-haiku-latest"


def test_resolve_falls_back_to_env(tmp_path) -> None:
    config = _config(tmp_path, anthropic_api_key="env-anthropic", llm_provider="anthropic")

    resolved = resolve_active_settings(config)

    assert resolved.provider == ANTHROPIC
    assert resolved.api_key == "env-anthropic"


def test_llm_client_disabled_without_key() -> None:
    client = LLMClient(provider=OPENAI, api_key="", model="gpt-4o-mini")
    assert client.enabled is False
    assert client.generate("sys", "user") is None


def test_llm_client_dispatches_to_anthropic() -> None:
    client = LLMClient(provider=ANTHROPIC, api_key="sk-ant", model="claude-3-5-sonnet-latest")
    fake_block = SimpleNamespace(type="text", text="hello world")
    fake_message = SimpleNamespace(content=[fake_block])
    fake_client = MagicMock()
    fake_client.messages.create.return_value = fake_message
    fake_module = SimpleNamespace(Anthropic=MagicMock(return_value=fake_client))

    with patch.dict("sys.modules", {"anthropic": fake_module}):
        result = client.generate("system prompt", "user prompt")

    assert result == "hello world"
    fake_client.messages.create.assert_called_once()
    _, kwargs = fake_client.messages.create.call_args
    assert kwargs["model"] == "claude-3-5-sonnet-latest"
    assert kwargs["system"] == "system prompt"


def test_llm_client_openai_reasoning_model_omits_temperature() -> None:
    client = LLMClient(provider=OPENAI, api_key="sk", model="o3")
    captured: dict = {}

    def _create(**kwargs):
        captured.update(kwargs)
        choice = SimpleNamespace(message=SimpleNamespace(content="ok"))
        return SimpleNamespace(choices=[choice])

    fake_client = MagicMock()
    fake_client.chat.completions.create.side_effect = _create
    fake_openai = SimpleNamespace(OpenAI=MagicMock(return_value=fake_client))

    with patch.dict("sys.modules", {"openai": fake_openai}):
        result = client.generate("sys", "user")

    assert result == "ok"
    assert "temperature" not in captured
    assert captured.get("max_completion_tokens")


def test_filter_openai_chat_models_excludes_non_chat() -> None:
    ids = [
        "gpt-4o",
        "gpt-4o-mini",
        "o3",
        "gpt-4o-audio-preview",
        "text-embedding-3-small",
        "whisper-1",
        "dall-e-3",
        "gpt-3.5-turbo-instruct",
    ]
    result = _filter_openai_chat_models(ids)
    assert "gpt-4o" in result and "o3" in result
    assert "gpt-4o-audio-preview" not in result
    assert "text-embedding-3-small" not in result
    assert "whisper-1" not in result
    assert "gpt-3.5-turbo-instruct" not in result


def test_preferred_model_prefers_known_default() -> None:
    available = ["gpt-4o", "gpt-4o-mini", "o3"]
    assert preferred_model(OPENAI, available) == "gpt-4o-mini"


def test_preferred_model_keeps_current_when_available() -> None:
    available = ["gpt-4o", "gpt-4o-mini", "o3"]
    assert preferred_model(OPENAI, available, current="o3") == "o3"


def test_preferred_model_matches_family_for_dated_ids() -> None:
    available = ["claude-3-5-sonnet-20241022", "claude-3-5-haiku-20241022"]
    assert preferred_model(ANTHROPIC, available) == "claude-3-5-sonnet-20241022"


def test_preferred_model_falls_back_to_first() -> None:
    available = ["some-custom-model"]
    assert preferred_model(OPENAI, available) == "some-custom-model"


def test_list_models_returns_empty_without_key() -> None:
    assert LLMClient(provider=OPENAI, api_key="").list_models() == []


def test_list_models_dispatches_to_anthropic() -> None:
    client = LLMClient(provider=ANTHROPIC, api_key="sk-ant", model="claude-3-5-sonnet-latest")
    models = SimpleNamespace(data=[SimpleNamespace(id="claude-3-5-sonnet-20241022")])
    fake_client = MagicMock()
    fake_client.models.list.return_value = models
    fake_module = SimpleNamespace(Anthropic=MagicMock(return_value=fake_client))

    with patch.dict("sys.modules", {"anthropic": fake_module}):
        result = client.list_models()

    assert result == ["claude-3-5-sonnet-20241022"]


def test_list_models_filters_openai() -> None:
    client = LLMClient(provider=OPENAI, api_key="sk", model="gpt-4o")
    models = SimpleNamespace(
        data=[SimpleNamespace(id="gpt-4o"), SimpleNamespace(id="whisper-1")]
    )
    fake_client = MagicMock()
    fake_client.models.list.return_value = models
    fake_openai = SimpleNamespace(OpenAI=MagicMock(return_value=fake_client))

    with patch.dict("sys.modules", {"openai": fake_openai}):
        result = client.list_models()

    assert result == ["gpt-4o"]


def test_llm_client_openai_standard_model_uses_temperature() -> None:
    client = LLMClient(provider=OPENAI, api_key="sk", model="gpt-4o")
    captured: dict = {}

    def _create(**kwargs):
        captured.update(kwargs)
        choice = SimpleNamespace(message=SimpleNamespace(content="ok"))
        return SimpleNamespace(choices=[choice])

    fake_client = MagicMock()
    fake_client.chat.completions.create.side_effect = _create
    fake_openai = SimpleNamespace(OpenAI=MagicMock(return_value=fake_client))

    with patch.dict("sys.modules", {"openai": fake_openai}):
        client.generate("sys", "user")

    assert captured.get("temperature") == 0.6
