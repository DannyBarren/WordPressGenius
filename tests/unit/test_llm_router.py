from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from core.llm import (
    ANTHROPIC,
    GEMINI,
    GROQ,
    OPENAI,
    OPENAI_COMPATIBLE_BASE_URLS,
    PROVIDER_TIER_MODELS,
    AgentProfile,
    LLMClient,
    LLMRouter,
    build_llm_router,
    extract_json_object,
    is_openai_compatible,
    normalize_provider,
)


def test_normalize_provider_supports_all_four() -> None:
    assert normalize_provider("groq") == GROQ
    assert normalize_provider("GEMINI") == GEMINI
    assert normalize_provider("anthropic") == ANTHROPIC
    assert normalize_provider("unknown") == OPENAI


def test_is_openai_compatible() -> None:
    assert is_openai_compatible(OPENAI)
    assert is_openai_compatible(GROQ)
    assert is_openai_compatible(GEMINI)
    assert not is_openai_compatible(ANTHROPIC)


def test_extract_json_object_handles_fences_and_garbage() -> None:
    assert extract_json_object("```json\n{\"a\": 1}\n```") == {"a": 1}
    assert extract_json_object("noise {\"b\": 2} tail") == {"b": 2}
    assert extract_json_object("nope") is None
    assert extract_json_object(None) is None


def test_router_routes_roles_to_tiers_and_temperatures() -> None:
    router = LLMRouter(
        provider=OPENAI,
        api_key="sk",
        fast_model="gpt-4o-mini",
        premium_model="gpt-4o",
    )
    assert router.enabled
    # Planner/reviewer/executor/researcher -> fast model, low temperature.
    assert router.model_for("planner") == "gpt-4o-mini"
    assert router.model_for("reviewer") == "gpt-4o-mini"
    assert router.temperature_for("planner") == 0.1
    assert router.temperature_for("executor") == 0.0
    # Content writer -> premium model, higher temperature.
    assert router.model_for("content_writer") == "gpt-4o"
    assert router.temperature_for("content_writer") == 0.7


def test_router_disabled_without_key_returns_none() -> None:
    router = LLMRouter(provider=OPENAI, api_key="")
    assert router.enabled is False
    assert router.complete("planner", "sys", "user") is None
    assert router.complete_json("planner", "sys", "user") is None


def test_router_temperature_overrides_apply_by_role_group() -> None:
    router = LLMRouter(
        provider=GROQ,
        api_key="sk",
        precise_temperature=0.05,
        creative_temperature=0.9,
    )
    assert router.temperature_for("planner") == 0.05
    assert router.temperature_for("reviewer") == 0.05
    assert router.temperature_for("content_writer") == 0.9
    assert router.temperature_for("communicator") == 0.9


def test_build_llm_router_uses_selected_model_as_premium() -> None:
    settings = SimpleNamespace(provider="openai", api_key="sk", model="gpt-4o")
    config = SimpleNamespace(
        llm_fast_model="",
        llm_premium_model="",
        llm_temperature_precise=None,
        llm_temperature_creative=None,
    )
    router = build_llm_router(settings, config)
    assert router.premium_model == "gpt-4o"
    assert router.fast_model == PROVIDER_TIER_MODELS["openai"]["fast"]


def test_build_llm_router_respects_env_overrides() -> None:
    settings = SimpleNamespace(provider="anthropic", api_key="sk", model="claude-3-5-sonnet-latest")
    config = SimpleNamespace(
        llm_fast_model="claude-3-5-haiku-latest",
        llm_premium_model="claude-opus-4-latest",
        llm_temperature_precise=0.2,
        llm_temperature_creative=0.8,
    )
    router = build_llm_router(settings, config)
    assert router.fast_model == "claude-3-5-haiku-latest"
    assert router.premium_model == "claude-opus-4-latest"
    assert router.temperature_for("planner") == 0.2
    assert router.temperature_for("content_writer") == 0.8


@pytest.mark.parametrize("provider", [GROQ, GEMINI])
def test_compat_providers_use_openai_client_with_base_url(provider: str) -> None:
    client = LLMClient(provider=provider, api_key="sk", model="some-model", temperature=0.3)
    captured: dict = {}

    def _create(**kwargs):
        captured.update(kwargs)
        choice = SimpleNamespace(message=SimpleNamespace(content="ok"))
        return SimpleNamespace(choices=[choice])

    fake_client = MagicMock()
    fake_client.chat.completions.create.side_effect = _create
    fake_openai_ctor = MagicMock(return_value=fake_client)
    fake_openai = SimpleNamespace(OpenAI=fake_openai_ctor)

    with patch.dict("sys.modules", {"openai": fake_openai}):
        result = client.generate("sys", "user")

    assert result == "ok"
    # The OpenAI client must be constructed with the provider's compat base URL.
    _, kwargs = fake_openai_ctor.call_args
    assert kwargs["base_url"] == OPENAI_COMPATIBLE_BASE_URLS[provider]
    # Compat providers receive temperature (not treated as reasoning models).
    assert captured["temperature"] == 0.3


def test_generate_passes_custom_temperature_for_openai() -> None:
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
        client.generate("sys", "user", temperature=0.15)

    assert captured["temperature"] == 0.15


def test_profiles_cover_all_agents() -> None:
    from core.llm import AGENT_PROFILES

    for role in ("planner", "researcher", "content_writer", "executor", "reviewer", "communicator"):
        assert isinstance(AGENT_PROFILES[role], AgentProfile)
