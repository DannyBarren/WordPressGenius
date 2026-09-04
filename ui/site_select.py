"""The "Select site" page and active-site sidebar controls."""

from __future__ import annotations

import streamlit as st
from pydantic import ValidationError

from core.config import AppConfig
from core.models import WordPressCredentials
from core.security import AuthenticatedUser, role_at_least
from core.sites_store import SavedSite, SiteVault
from ui.theme import render_hero


def site_vault(config: AppConfig) -> SiteVault:
    return SiteVault(config.sites_vault_path, config.credential_key)


def get_active_site(config: AppConfig, user: AuthenticatedUser) -> SavedSite | None:
    return site_vault(config).get_active(user)


def render_active_site_sidebar(config: AppConfig, user: AuthenticatedUser) -> None:
    """Show the active site and a button to open the Select site page."""

    st.sidebar.header("WordPress site")
    active = get_active_site(config, user)
    if active:
        st.sidebar.success(f"Active site: {active.label}")
        st.sidebar.caption(str(active.site_url))
    else:
        st.sidebar.info("No site selected yet.")
    if st.sidebar.button("Select / manage sites", use_container_width=True):
        st.session_state.manage_sites = True
        st.rerun()


def render_site_picker(config: AppConfig, user: AuthenticatedUser, orchestrator) -> None:
    """Render the full-page site selector and add-site form."""

    vault = site_vault(config)
    render_hero(
        title="Select a site",
        subtitle="Choose which WordPress site to manage",
        body="Pick a saved site below, or add a new one with its address and Application Password.",
        badge="Site library",
    )

    active_id = vault.get_active_id(user)
    if active_id and st.button("Back to chat", key="site_back"):
        st.session_state.manage_sites = False
        st.rerun()

    sites = vault.list_sites(user)
    if sites:
        st.subheader("Your saved sites")
        for site in sites:
            is_active = site.id == active_id
            with st.container(border=True):
                cols = st.columns([3, 1, 1])
                with cols[0]:
                    marker = " (active)" if is_active else ""
                    st.markdown(f"**{site.label}**{marker}")
                    st.caption(f"{site.site_url} · user: {site.username}")
                with cols[1]:
                    if st.button("Use", key=f"use_{site.id}", use_container_width=True):
                        vault.set_active(user, site.id)
                        st.session_state.manage_sites = False
                        st.session_state.connection_status = None
                        st.rerun()
                with cols[2]:
                    if st.button("Delete", key=f"del_{site.id}", use_container_width=True):
                        vault.delete_site(user, site.id)
                        st.rerun()
    else:
        st.info("No saved sites yet. Add your first site below.")

    _render_add_site_form(config, user, orchestrator)


def _render_add_site_form(config: AppConfig, user: AuthenticatedUser, orchestrator) -> None:
    st.subheader("Add a new site")

    label = st.text_input(
        "Site name",
        key="add_site_label",
        placeholder="e.g. My Shop (staging)",
        help="A friendly name so you can recognize this site.",
    )
    site_url = st.text_input(
        "Website address",
        key="add_site_url",
        placeholder="https://staging.yoursite.com",
    )
    username = st.text_input(
        "WordPress username",
        key="add_site_username",
        help="Use an administrator or editor account for the site.",
    )
    application_password = st.text_input(
        "Application Password",
        key="add_site_app_password",
        type="password",
        help="Generated in WordPress under Users -> Profile -> Application Passwords.",
    )

    credentials, error = _build_credentials(site_url, username, application_password)

    saved_site: SavedSite | None = None
    col1, col2 = st.columns(2)
    with col1:
        if st.button("Save site", type="primary", use_container_width=True, key="add_site_save"):
            if credentials is None:
                st.error(error)
            else:
                try:
                    saved_site = vault_add(config, user, label or site_url, credentials)
                except Exception as exc:  # noqa: BLE001 - surface storage failures
                    st.error(f"Could not save the site: {exc}")
    with col2:
        if st.button("Test connection", use_container_width=True, key="add_site_test"):
            if credentials is None:
                st.error(error)
            else:
                with st.spinner("Testing WordPress connection..."):
                    result = orchestrator.test_connection(credentials)
                if result.get("ok"):
                    st.success(
                        f"Connected to {result.get('site_name', 'site')} "
                        f"as {result.get('user_name', 'WordPress user')}."
                    )
                else:
                    st.error(result.get("message", "Connection failed."))

    st.caption("Site URLs and Application Passwords are encrypted on disk per user.")

    # Rerun after the widgets above have been processed so the save fully commits.
    if saved_site is not None:
        st.session_state.manage_sites = False
        st.session_state.connection_status = None
        st.success(f"Saved and selected '{saved_site.label}'. Loading...")
        st.rerun()


def _normalize_site_url(raw: str) -> str:
    value = raw.strip()
    if value and not value.lower().startswith(("http://", "https://")):
        value = f"https://{value}"
    return value


def _build_credentials(
    site_url: str, username: str, application_password: str
) -> tuple[WordPressCredentials | None, str]:
    """Return (credentials, error_message). credentials is None when invalid."""

    missing = []
    if not site_url.strip():
        missing.append("website address")
    if not username.strip():
        missing.append("WordPress username")
    if not application_password.strip():
        missing.append("Application Password")
    if missing:
        return None, "Please fill in: " + ", ".join(missing) + "."

    try:
        credentials = WordPressCredentials(
            site_url=_normalize_site_url(site_url),
            username=username.strip(),
            application_password=application_password.strip(),
        )
    except ValidationError as exc:
        detail = exc.errors()[0].get("msg", "invalid value") if exc.errors() else "invalid value"
        return None, f"That website address looks invalid ({detail}). Example: https://yoursite.com"
    return credentials, ""


def vault_add(
    config: AppConfig,
    user: AuthenticatedUser,
    label: str,
    credentials: WordPressCredentials,
) -> SavedSite:
    return site_vault(config).add_site(user, label=label, credentials=credentials, set_active=True)
