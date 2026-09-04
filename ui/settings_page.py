"""Sidebar settings panel for selecting the LLM provider and API key."""

from __future__ import annotations

import streamlit as st

from core.config import AppConfig
from core.llm import (
    ANTHROPIC,
    DEFAULT_MODELS,
    GEMINI,
    GROQ,
    OPENAI,
    PROVIDER_LABELS,
    SUPPORTED_PROVIDERS,
    LLMClient,
    normalize_provider,
    preferred_model,
)
from core.security import AuthenticatedUser, role_at_least
from core.settings_store import LLMSettingsStore


def render_llm_settings(config: AppConfig, user: AuthenticatedUser) -> None:
    """Render the AI provider/model/API-key settings in the sidebar."""

    with st.sidebar.expander("AI model & provider", expanded=False):
        if not role_at_least(user.role, "admin"):
            st.caption("Only admins can change the AI provider and API keys.")
            return

        if not config.credential_key:
            st.warning(
                "Set CREDENTIAL_ENCRYPTION_KEY in .env so saved API keys persist "
                "securely across restarts."
            )

        store = LLMSettingsStore(config.llm_settings_path, config.credential_key)
        active = store.active_provider() or normalize_provider(config.llm_provider)

        provider = st.radio(
            "LLM provider",
            options=list(PROVIDER_LABELS.keys()),
            format_func=lambda p: PROVIDER_LABELS[p],
            index=list(PROVIDER_LABELS).index(active),
            key="llm_provider_choice",
            horizontal=True,
        )

        stored_cfg = store.provider_config(provider)
        env_key = getattr(config, f"{provider}_api_key", "")
        env_model = getattr(config, f"{provider}_model", "")
        current_model = stored_cfg.get("model") or env_model or DEFAULT_MODELS[provider]

        key_already_set = bool(stored_cfg.get("api_key")) or bool(env_key)
        if key_already_set:
            st.caption("An API key is already configured for this provider.")
        api_key_input = st.text_input(
            f"{PROVIDER_LABELS[provider]} API key",
            value="",
            type="password",
            key=f"llm_key_{provider}",
            placeholder="Leave blank to keep the saved key"
            if key_already_set
            else "Paste your API key",
        )

        effective_key = api_key_input or stored_cfg.get("api_key") or env_key

        # The model list comes from what the API key can actually access. We
        # auto-detect once per provider/key and cache it for the session.
        models_state_key = f"detected_models_{provider}"
        if effective_key and models_state_key not in st.session_state:
            with st.spinner(f"Detecting models available to your {PROVIDER_LABELS[provider]} key..."):
                st.session_state[models_state_key] = LLMClient(
                    provider=provider, api_key=effective_key, model=current_model
                ).list_models()

        detected_models = st.session_state.get(models_state_key, [])

        model = current_model
        if detected_models:
            default_model = preferred_model(provider, detected_models, current_model)
            model = st.selectbox(
                "Model (detected from your API key)",
                options=detected_models,
                index=detected_models.index(default_model),
                key=f"llm_model_select_{provider}",
                help="These are the models your API key can access. The best default is preselected.",
            )
        elif effective_key:
            st.warning(
                "Could not detect models for this key yet. Verify the key, then click "
                "'Refresh models'. The active model will stay as the current default."
            )
        else:
            st.info("Add an API key to automatically detect the models it can use.")

        if effective_key and st.button("Refresh models", use_container_width=True, key="llm_refresh"):
            with st.spinner("Refreshing available models..."):
                st.session_state[models_state_key] = LLMClient(
                    provider=provider, api_key=effective_key, model=model
                ).list_models()
            st.rerun()

        agentic = st.checkbox(
            "Use the model to plan and respond (agentic mode)",
            value=store.agentic_enabled(default=config.llm_agentic_enabled),
            key="llm_agentic_toggle",
            help=(
                "On: the selected model powers every agent (Planner, Researcher, "
                "Executor check, Reviewer, Communicator) and drafts content, so you see "
                "its intelligence end to end. Off: a fast rule-based planner is used and "
                "the model only drafts page copy (lowest cost)."
            ),
        )
        st.caption(
            "Cost-aware routing: planning, research, and reviewing use a fast/cheap "
            "model; content creation uses your selected (premium) model. Override with "
            "LLM_FAST_MODEL / LLM_PREMIUM_MODEL in .env."
        )

        col1, col2 = st.columns(2)
        with col1:
            if st.button("Save settings", type="primary", use_container_width=True, key="llm_save"):
                store.save_provider(
                    provider,
                    api_key=api_key_input if api_key_input else None,
                    model=model,
                    set_active=True,
                )
                store.set_agentic(agentic)
                # Drop cached detection so the next render re-detects with the saved key.
                st.session_state.pop(models_state_key, None)
                st.success(f"Saved. Active provider: {PROVIDER_LABELS[provider]}, model: {model}.")
                st.rerun()
        with col2:
            if st.button("Test connection", use_container_width=True, key="llm_test"):
                if not effective_key:
                    st.error("Enter an API key first.")
                else:
                    with st.spinner("Contacting provider..."):
                        ok, message = LLMClient(
                            provider=provider, api_key=effective_key, model=model
                        ).test()
                    if ok:
                        st.success(message)
                    else:
                        st.error(message)

        configured = [
            PROVIDER_LABELS[p]
            for p in SUPPORTED_PROVIDERS
            if store.has_key(p) or getattr(config, f"{p}_api_key", "")
        ]
        st.caption(
            f"Active provider: {PROVIDER_LABELS[active]} · "
            f"keys configured: {', '.join(configured) if configured else 'none'}"
        )
        if api_key_input:
            st.caption("Keys are encrypted on disk in data/llm_settings.enc.")
