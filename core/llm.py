"""Centralized multi-provider LLM factory for the whole agent crew.

A single API key (per active provider) powers every agent. Supports OpenAI,
Groq, Anthropic, and Google Gemini. Groq and Gemini are reached through their
OpenAI-compatible endpoints, so no extra SDKs are required. An ``LLMRouter``
routes each agent role to a model tier (fast vs premium) and a temperature,
falling back to deterministic templates when no key is configured.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

LOGGER = logging.getLogger(__name__)

OPENAI = "openai"
ANTHROPIC = "anthropic"
GROQ = "groq"
GEMINI = "gemini"
SUPPORTED_PROVIDERS = (OPENAI, ANTHROPIC, GROQ, GEMINI)

# Providers that speak the OpenAI chat-completions protocol via a custom base URL.
# Reusing the ``openai`` SDK keeps dependencies minimal and behavior consistent.
OPENAI_COMPATIBLE_BASE_URLS: dict[str, str] = {
    GROQ: "https://api.groq.com/openai/v1",
    GEMINI: "https://generativelanguage.googleapis.com/v1beta/openai/",
}

# Default models offered as suggestions in the UI. Users may pick any detected id.
PROVIDER_MODEL_SUGGESTIONS: dict[str, list[str]] = {
    OPENAI: ["gpt-4o-mini", "gpt-4o", "o4-mini", "o3", "o1"],
    ANTHROPIC: [
        "claude-3-5-sonnet-latest",
        "claude-3-5-haiku-latest",
        "claude-3-7-sonnet-latest",
        "claude-opus-4-latest",
    ],
    GROQ: [
        "llama-3.3-70b-versatile",
        "llama-3.1-8b-instant",
        "openai/gpt-oss-20b",
    ],
    GEMINI: [
        "gemini-1.5-flash",
        "gemini-1.5-pro",
        "gemini-2.0-flash",
    ],
}

DEFAULT_MODELS: dict[str, str] = {
    OPENAI: "gpt-4o-mini",
    ANTHROPIC: "claude-3-5-sonnet-latest",
    GROQ: "llama-3.3-70b-versatile",
    GEMINI: "gemini-1.5-flash",
}

# Per-provider model tiers used for cost-aware routing: a cheaper/faster model
# for planning, research, execution checks, and reviewing; a stronger model for
# content creation.
PROVIDER_TIER_MODELS: dict[str, dict[str, str]] = {
    OPENAI: {"fast": "gpt-4o-mini", "premium": "gpt-4o"},
    ANTHROPIC: {"fast": "claude-3-5-haiku-latest", "premium": "claude-3-5-sonnet-latest"},
    GROQ: {"fast": "llama-3.1-8b-instant", "premium": "llama-3.3-70b-versatile"},
    GEMINI: {"fast": "gemini-1.5-flash", "premium": "gemini-1.5-pro"},
}

# Human-friendly provider names used across the UI and status messages.
PROVIDER_LABELS: dict[str, str] = {
    OPENAI: "OpenAI",
    ANTHROPIC: "Anthropic",
    GROQ: "Groq",
    GEMINI: "Google Gemini",
}

# Model name fragments that indicate an OpenAI reasoning model. These models
# reject `temperature` and use `max_completion_tokens` instead of `max_tokens`.
_OPENAI_REASONING_PREFIXES = ("o1", "o3", "o4", "o5")

# Used to filter the OpenAI model list down to chat/reasoning models.
_OPENAI_CHAT_PREFIXES = ("gpt-", "o1", "o3", "o4", "o5", "chatgpt")
_OPENAI_NON_CHAT_FRAGMENTS = (
    "audio",
    "realtime",
    "embedding",
    "tts",
    "whisper",
    "image",
    "dall-e",
    "moderation",
    "transcribe",
    "-instruct",
    "search",
)

_MAX_OUTPUT_TOKENS = 2000
_DEFAULT_TEMPERATURE = 0.6


def normalize_provider(provider: str | None) -> str:
    value = (provider or "").strip().lower()
    return value if value in SUPPORTED_PROVIDERS else OPENAI


def is_openai_compatible(provider: str) -> bool:
    """True for providers reached via the OpenAI chat-completions protocol."""

    return normalize_provider(provider) in (OPENAI, GROQ, GEMINI)


def preferred_model(provider: str, available: list[str], current: str = "") -> str:
    """Pick a sensible default model from the list a key actually has access to."""

    provider = normalize_provider(provider)
    if not available:
        return current or DEFAULT_MODELS[provider]
    if current and current in available:
        return current
    suggestions = PROVIDER_MODEL_SUGGESTIONS.get(provider, [])
    for pref in suggestions:
        if pref in available:
            return pref
    # Fall back to matching by family (e.g. "claude-3-5-sonnet" matches a dated id).
    for pref in suggestions:
        base = pref.replace("-latest", "")
        for model_id in available:
            if model_id.startswith(base):
                return model_id
    return available[0]


def _filter_openai_chat_models(model_ids: list[str]) -> list[str]:
    chat: list[str] = []
    for model_id in model_ids:
        lowered = model_id.lower()
        if not lowered.startswith(_OPENAI_CHAT_PREFIXES):
            continue
        if any(fragment in lowered for fragment in _OPENAI_NON_CHAT_FRAGMENTS):
            continue
        chat.append(model_id)
    result = sorted(set(chat))
    return result or sorted(set(model_ids))


class LLMClient:
    """Provider-agnostic chat wrapper for OpenAI, Groq, Gemini, and Anthropic."""

    def __init__(
        self,
        provider: str = OPENAI,
        api_key: str = "",
        model: str = "",
        *,
        temperature: float = _DEFAULT_TEMPERATURE,
    ) -> None:
        self.provider = normalize_provider(provider)
        self.api_key = api_key or ""
        self.model = model or DEFAULT_MODELS[self.provider]
        self.temperature = temperature

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        max_output_tokens: int | None = None,
        temperature: float | None = None,
    ) -> str | None:
        """Return model output, or None when disabled or on any provider error."""

        if not self.enabled:
            return None
        tokens = max_output_tokens or _MAX_OUTPUT_TOKENS
        temp = self.temperature if temperature is None else temperature
        try:
            if self.provider == ANTHROPIC:
                return self._generate_anthropic(system_prompt, user_prompt, tokens, temp)
            return self._generate_openai(system_prompt, user_prompt, tokens, temp)
        except Exception:  # noqa: BLE001 - never let model errors break the app flow
            LOGGER.exception("LLM generation failed for provider %s", self.provider)
            return None

    def generate_json(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        max_output_tokens: int | None = None,
        temperature: float | None = None,
    ) -> dict | None:
        """Return parsed structured output, or None on disable/error/parse failure."""

        raw = self.generate(
            system_prompt + "\n\nRespond with a single valid JSON object and nothing else.",
            user_prompt,
            max_output_tokens=max_output_tokens,
            temperature=temperature,
        )
        return extract_json_object(raw) if raw else None

    def list_models(self) -> list[str]:
        """Return model ids the configured API key can access (empty on error)."""

        if not self.api_key:
            return []
        try:
            if self.provider == ANTHROPIC:
                return self._list_anthropic_models()
            return self._list_openai_models()
        except Exception:  # noqa: BLE001 - listing is best-effort
            LOGGER.exception("Listing models failed for provider %s", self.provider)
            return []

    def _openai_client(self):
        from openai import OpenAI

        base_url = OPENAI_COMPATIBLE_BASE_URLS.get(self.provider)
        if base_url:
            return OpenAI(api_key=self.api_key, base_url=base_url)
        return OpenAI(api_key=self.api_key)

    def _list_openai_models(self) -> list[str]:
        client = self._openai_client()
        ids = [model.id for model in client.models.list().data]
        if self.provider == OPENAI:
            return _filter_openai_chat_models(ids)
        # Groq/Gemini already expose only chat-capable models on this endpoint.
        return sorted(set(ids)) or ids

    def _list_anthropic_models(self) -> list[str]:
        import anthropic

        client = anthropic.Anthropic(api_key=self.api_key)
        return [model.id for model in client.models.list().data]

    def test(self) -> tuple[bool, str]:
        """Make a tiny call to validate the provider, key, and model."""

        if not self.api_key:
            return False, "No API key configured."
        try:
            if self.provider == ANTHROPIC:
                self._generate_anthropic("You are a connectivity check.", "Reply with: ok", 16, 0.0)
            else:
                self._generate_openai("You are a connectivity check.", "Reply with: ok", 16, 0.0)
        except Exception as exc:  # noqa: BLE001 - surface a friendly reason
            return False, _friendly_provider_error(self.provider, exc)
        return True, f"{PROVIDER_LABELS.get(self.provider, self.provider.title())} model '{self.model}' is reachable."

    def _is_openai_reasoning_model(self) -> bool:
        # Reasoning-model parameter rules only apply to native OpenAI.
        if self.provider != OPENAI:
            return False
        name = self.model.lower()
        return name.startswith(_OPENAI_REASONING_PREFIXES)

    def _generate_openai(
        self,
        system_prompt: str,
        user_prompt: str,
        max_output_tokens: int,
        temperature: float,
    ) -> str | None:
        client = self._openai_client()
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        kwargs: dict[str, object] = {"model": self.model, "messages": messages}
        if self._is_openai_reasoning_model():
            kwargs["max_completion_tokens"] = max_output_tokens
        else:
            kwargs["temperature"] = temperature
            kwargs["max_tokens"] = max_output_tokens
        response = client.chat.completions.create(**kwargs)
        return response.choices[0].message.content

    def _generate_anthropic(
        self,
        system_prompt: str,
        user_prompt: str,
        max_output_tokens: int,
        temperature: float,
    ) -> str | None:
        import anthropic

        client = anthropic.Anthropic(api_key=self.api_key)
        message = client.messages.create(
            model=self.model,
            max_tokens=max_output_tokens,
            temperature=max(0.0, min(1.0, temperature)),
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        parts = [block.text for block in message.content if getattr(block, "type", "") == "text"]
        return "".join(parts) if parts else None


def extract_json_object(text: str | None) -> dict | None:
    """Best-effort parse of a JSON object from possibly fenced/wrapped model text."""

    if not text:
        return None
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\n?", "", cleaned)
        cleaned = re.sub(r"\n?```$", "", cleaned).strip()
    try:
        parsed = json.loads(cleaned)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group(0))
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            return None
    return None


# Agent roles share one API key but differ in model tier, temperature, and
# verbosity. Planner/Researcher/Executor/Reviewer favor precision and cost;
# the Content Writer favors quality; the Communicator is warm but concise.
@dataclass(frozen=True)
class AgentProfile:
    """Routing profile for a single agent role."""

    tier: str  # "fast" | "premium"
    temperature: float
    max_output_tokens: int


AGENT_PROFILES: dict[str, AgentProfile] = {
    "planner": AgentProfile("fast", 0.1, 700),
    "researcher": AgentProfile("fast", 0.2, 400),
    "content_writer": AgentProfile("premium", 0.7, 2000),
    "executor": AgentProfile("fast", 0.0, 300),
    "reviewer": AgentProfile("fast", 0.1, 450),
    "communicator": AgentProfile("fast", 0.4, 700),
}

# Roles grouped for the two coarse temperature knobs exposed in config.
PRECISE_ROLES = frozenset({"planner", "researcher", "executor", "reviewer"})
CREATIVE_ROLES = frozenset({"content_writer", "communicator"})


class LLMRouter:
    """One key, every agent.

    Holds the active provider/key once and hands each agent role a correctly
    configured :class:`LLMClient` (model tier + temperature). This is the single
    object injected into every LangGraph node so the whole crew shares one key.
    """

    def __init__(
        self,
        *,
        provider: str,
        api_key: str,
        fast_model: str = "",
        premium_model: str = "",
        profiles: dict[str, AgentProfile] | None = None,
        precise_temperature: float | None = None,
        creative_temperature: float | None = None,
    ) -> None:
        self.provider = normalize_provider(provider)
        self.api_key = api_key or ""
        tier_defaults = PROVIDER_TIER_MODELS[self.provider]
        self.fast_model = fast_model or tier_defaults["fast"]
        self.premium_model = premium_model or tier_defaults["premium"]
        self.profiles = profiles or AGENT_PROFILES
        self.precise_temperature = precise_temperature
        self.creative_temperature = creative_temperature

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def model_for(self, role: str) -> str:
        profile = self.profiles.get(role, AGENT_PROFILES["communicator"])
        return self.premium_model if profile.tier == "premium" else self.fast_model

    def temperature_for(self, role: str) -> float:
        profile = self.profiles.get(role, AGENT_PROFILES["communicator"])
        if role in PRECISE_ROLES and self.precise_temperature is not None:
            return self.precise_temperature
        if role in CREATIVE_ROLES and self.creative_temperature is not None:
            return self.creative_temperature
        return profile.temperature

    def client_for(self, role: str) -> LLMClient:
        return LLMClient(
            provider=self.provider,
            api_key=self.api_key,
            model=self.model_for(role),
            temperature=self.temperature_for(role),
        )

    def complete(
        self, role: str, system_prompt: str, user_prompt: str, *, max_output_tokens: int | None = None
    ) -> str | None:
        if not self.enabled:
            return None
        profile = self.profiles.get(role, AGENT_PROFILES["communicator"])
        return self.client_for(role).generate(
            system_prompt,
            user_prompt,
            max_output_tokens=max_output_tokens or profile.max_output_tokens,
        )

    def complete_json(
        self, role: str, system_prompt: str, user_prompt: str, *, max_output_tokens: int | None = None
    ) -> dict | None:
        if not self.enabled:
            return None
        profile = self.profiles.get(role, AGENT_PROFILES["communicator"])
        return self.client_for(role).generate_json(
            system_prompt,
            user_prompt,
            max_output_tokens=max_output_tokens or profile.max_output_tokens,
        )


def build_llm_router(settings, config=None) -> LLMRouter:
    """Build the shared router from resolved settings and optional app config.

    The UI-selected model becomes the premium tier (used for content), while the
    fast tier defaults to a cheaper per-provider model unless overridden in env.
    """

    provider = normalize_provider(getattr(settings, "provider", OPENAI))
    api_key = getattr(settings, "api_key", "") or ""
    selected_model = getattr(settings, "model", "") or ""

    fast_override = getattr(config, "llm_fast_model", "") if config else ""
    premium_override = getattr(config, "llm_premium_model", "") if config else ""
    precise = getattr(config, "llm_temperature_precise", None) if config else None
    creative = getattr(config, "llm_temperature_creative", None) if config else None

    tier_defaults = PROVIDER_TIER_MODELS[provider]
    fast_model = fast_override or tier_defaults["fast"]
    premium_model = premium_override or selected_model or tier_defaults["premium"]

    return LLMRouter(
        provider=provider,
        api_key=api_key,
        fast_model=fast_model,
        premium_model=premium_model,
        precise_temperature=precise,
        creative_temperature=creative,
    )


def _friendly_provider_error(provider: str, exc: Exception) -> str:
    text = str(exc)
    lowered = text.lower()
    if "module named" in lowered or isinstance(exc, ImportError):
        package = "anthropic" if provider == ANTHROPIC else "openai"
        return f"The '{package}' package is not installed. Run the environment setup again."
    if "auth" in lowered or "api key" in lowered or "401" in lowered or "invalid" in lowered:
        return "The API key was rejected. Double-check the key for this provider."
    if "model" in lowered and ("not found" in lowered or "does not exist" in lowered or "404" in lowered):
        return "That model id was not found for this provider/account."
    return f"Could not reach the provider: {text}"


def fallback_page_copy(request: str) -> str:
    """Create clean starter content when no LLM provider is configured."""

    return f"""
<!-- wp:heading -->
<h2>{_headline_from_request(request)}</h2>
<!-- /wp:heading -->

<!-- wp:paragraph -->
<p>We created this draft from your request: "{request}". Review the details,
photos, pricing, and service area before publishing.</p>
<!-- /wp:paragraph -->

<!-- wp:list -->
<ul>
  <li>Clear description of the service or announcement.</li>
  <li>Benefits for customers and next steps.</li>
  <li>Suggested call to action: call, book online, or request a quote.</li>
</ul>
<!-- /wp:list -->

<!-- wp:paragraph -->
<p><strong>Suggested image:</strong> Use an authentic business photo that shows
the team, finished work, or a before-and-after transformation.</p>
<!-- /wp:paragraph -->
""".strip()


def fallback_blog_copy(request: str) -> str:
    return f"""
<!-- wp:heading -->
<h2>{_headline_from_request(request)}</h2>
<!-- /wp:heading -->

<!-- wp:paragraph -->
<p>This draft introduces the topic requested by the business owner and gives
customers useful, plain-English guidance.</p>
<!-- /wp:paragraph -->

<!-- wp:heading {{"level":3}} -->
<h3>Why it matters</h3>
<!-- /wp:heading -->

<!-- wp:paragraph -->
<p>Customers want fast answers, trustworthy expertise, and a clear reason to
contact the business today.</p>
<!-- /wp:paragraph -->

<!-- wp:heading {{"level":3}} -->
<h3>What to do next</h3>
<!-- /wp:heading -->

<!-- wp:paragraph -->
<p>Contact our team to learn more, schedule service, or ask about current
specials.</p>
<!-- /wp:paragraph -->
""".strip()


def _headline_from_request(request: str) -> str:
    cleaned = request.strip().strip(".")
    if len(cleaned) <= 70:
        return cleaned[:1].upper() + cleaned[1:]
    return cleaned[:67].rstrip() + "..."
