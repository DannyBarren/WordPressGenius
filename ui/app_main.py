"""Main Streamlit chat interface for WordPressGenius."""

from __future__ import annotations

import io
import tempfile
import time
import zipfile
from pathlib import Path

import streamlit as st
from openai import OpenAI, OpenAIError

from core.config import get_config
from core.logging_config import configure_logging
from core.observability import UsageAnalytics, init_sentry
from core.models import AgentStatus, WordPressCredentials
from core.orchestrator import WordPressGeniusOrchestrator
from core.rate_limit import RateLimiter
from core.security import AuthManager, AuthenticatedUser, role_at_least
from ui.settings_page import render_llm_settings
from ui.site_select import (
    get_active_site,
    render_active_site_sidebar,
    render_site_picker,
)
from ui.theme import (
    COMPANY_NAME,
    PRODUCT_NAME,
    apply_theme,
    render_credit_footer,
    render_hero,
    render_sidebar_brand,
)


config = get_config()
configure_logging(
    config.log_level,
    log_file=config.log_file,
    json_logs=config.json_logs,
    enable_file_logging=config.enable_file_logging,
)
init_sentry(
    config.sentry_dsn,
    environment=config.app_env,
    traces_sample_rate=config.sentry_traces_sample_rate,
)


PROMPT_LIBRARY = {
    "Content": {
        "Product spotlight": "Create a draft blog post highlighting one of our products.",
        "New service page": "Add a draft page for a new service we offer.",
        "Store promo": "Update the homepage with our weekend sale after approval.",
    },
    "Operations": {
        "Check contact form": "Check the contact form and tell me what may be wrong.",
        "Undo last change": "Undo the last supported WordPress change.",
        "Bulk price update": "Increase all product prices by 5%.",
    },
    "Growth": {
        "SEO tune-up": "Optimize SEO for our main landing page.",
        "Analytics summary": "Summarize my connected analytics and site traffic options.",
    },
}


def _init_state() -> None:
    st.session_state.setdefault("messages", [])
    st.session_state.setdefault("pending_request", None)
    st.session_state.setdefault("pending_summary", "")
    st.session_state.setdefault("queued_prompt", None)
    st.session_state.setdefault("connection_status", None)
    st.session_state.setdefault("auth_user", None)
    st.session_state.setdefault(
        "rate_limiter",
        RateLimiter(max_requests=config.max_requests_per_minute),
    )
    st.session_state.setdefault("uploaded_media_paths", [])
    st.session_state.setdefault("usage_analytics_consent", False)
    st.session_state.setdefault("manage_sites", False)


def _auth_manager() -> AuthManager:
    return AuthManager(config.auth_users_path, enabled=config.auth_enabled)


def _usage_analytics() -> UsageAnalytics:
    return UsageAnalytics(config.usage_analytics_path, enabled=config.usage_analytics_enabled)


def _current_user() -> AuthenticatedUser | None:
    raw = st.session_state.get("auth_user")
    if not raw:
        return None
    return AuthenticatedUser(username=raw["username"], role=raw["role"])


def _render_login() -> AuthenticatedUser | None:
    user = _current_user()
    if user:
        with st.sidebar:
            st.success(f"Signed in as {user.username} ({user.role})")
            if st.button("Sign out", use_container_width=True):
                st.session_state.auth_user = None
                st.rerun()
        return user

    if not config.auth_enabled:
        user = AuthenticatedUser(username="local-admin", role="admin")
        st.session_state.auth_user = {"username": user.username, "role": user.role}
        return user

    st.title(PRODUCT_NAME)
    st.caption(COMPANY_NAME)
    st.info(f"Sign in to manage your site with {PRODUCT_NAME}.")
    with st.form("login_form"):
        username = st.text_input("Username")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Sign in", type="primary")
    if submitted:
        auth_user = _auth_manager().authenticate(username, password)
        if auth_user:
            st.session_state.auth_user = {"username": auth_user.username, "role": auth_user.role}
            st.rerun()
        st.error("Invalid username or password.")
    st.caption("For self-hosted deployments, configure users in AUTH_USERS_PATH.")
    return None


def _render_connection_status(
    orchestrator: WordPressGeniusOrchestrator,
    credentials: WordPressCredentials | None,
    user: AuthenticatedUser,
) -> None:
    if not credentials:
        st.sidebar.info(f"Not connected yet. {PRODUCT_NAME} can still draft a plan.")
        return

    st.sidebar.success("Credentials entered")
    if st.sidebar.button("Test WordPress connection", use_container_width=True):
        with st.sidebar.status("Testing WordPress...", expanded=True) as status:
            result = orchestrator.test_connection(credentials)
            if result["ok"]:
                status.write(f"Connected to {result['site_name']}")
                status.write(f"User: {result.get('user_name', 'WordPress user')}")
                status.update(label="Connection verified", state="complete")
                st.session_state.connection_status = result
            else:
                status.write(result["message"])
                status.update(label="Connection failed", state="error")
                st.session_state.connection_status = result

    cached = st.session_state.connection_status
    if cached and cached.get("ok"):
        st.sidebar.caption(f"Last verified: {cached.get('site_name')}")


def _render_quick_actions() -> None:
    st.sidebar.divider()
    st.sidebar.subheader("Example prompt library")
    for category, prompts in PROMPT_LIBRARY.items():
        with st.sidebar.expander(category, expanded=category == "Content"):
            for label, prompt in prompts.items():
                if st.button(label, use_container_width=True, key=f"quick_{category}_{label}"):
                    st.session_state.queued_prompt = prompt


def _run_agent(
    orchestrator: WordPressGeniusOrchestrator,
    request: str,
    credentials: WordPressCredentials | None,
    *,
    approved: bool,
    user: AuthenticatedUser,
    append_user: bool = True,
) -> None:
    if len(request) > config.max_prompt_length:
        st.error("That request is too long. Please shorten it and try again.")
        return
    rate_limit = st.session_state.rate_limiter.check(f"{user.username}:local")
    if not rate_limit.allowed:
        st.warning(rate_limit.message)
        return

    if append_user:
        st.session_state.messages.append({"role": "user", "content": request})

    with st.chat_message("assistant"):
        progress = st.progress(0, text="Planner is reading your request...")
        status_box = st.status(f"{PRODUCT_NAME} is working", expanded=True)
        status_box.write("Planner is turning the request into a safe plan.")
        time.sleep(0.05)
        progress.progress(25, text="Researcher is checking site context...")
        status_box.write("Researcher is checking WordPress access and related content.")
        time.sleep(0.05)
        progress.progress(50, text="Content Writer is preparing copy...")
        status_box.write("Content Writer is preparing customer-friendly copy.")
        time.sleep(0.05)
        progress.progress(75, text="Executor and Reviewer are validating the plan...")
        status_box.write("Executor is applying approved changes or waiting for confirmation.")

        result = orchestrator.run(
            request,
            credentials=credentials,
            approved=approved,
            user=user,
            client_ip="streamlit-local",
        )

        for status in result.statuses:
            status_box.write(f"{status.agent}: {status.status} - {status.detail}")
        progress.progress(100, text="Ready")
        if any(status.status == "error" for status in result.statuses):
            status_box.update(label=f"{PRODUCT_NAME} needs attention", state="error")
        elif result.requires_confirmation and not approved:
            status_box.update(label="Plan ready for your approval", state="complete")
        else:
            status_box.update(label=f"{PRODUCT_NAME} finished", state="complete")
        st.markdown(result.final_response)

    _usage_analytics().record(
        user=user,
        event_type="agent_run",
        properties={
            "approved": approved,
            "requires_confirmation": result.requires_confirmation,
            "status_count": len(result.statuses),
        },
        consent=st.session_state.get("usage_analytics_consent", False),
    )

    st.session_state.messages.append(
        {
            "role": "assistant",
            "content": result.final_response,
            "statuses": [status.model_dump(mode="json") for status in result.statuses],
        }
    )
    if result.requires_confirmation and not approved:
        st.session_state.pending_request = request
        st.session_state.pending_summary = result.confirmation_summary
    else:
        st.session_state.pending_request = None
        st.session_state.pending_summary = ""



def _transcribe_audio(audio_file) -> str | None:
    if not audio_file:
        return None
    if not config.openai_api_key:
        st.info("Add OPENAI_API_KEY to enable voice transcription.")
        return None
    client = OpenAI(api_key=config.openai_api_key)
    suffix = Path(audio_file.name or "voice.wav").suffix or ".wav"
    with tempfile.NamedTemporaryFile(suffix=suffix) as handle:
        handle.write(audio_file.getbuffer())
        handle.flush()
        try:
            with open(handle.name, "rb") as audio_handle:
                transcript = client.audio.transcriptions.create(
                    model="whisper-1",
                    file=audio_handle,
                )
        except OpenAIError as exc:
            st.warning(f"Voice transcription failed: {exc}")
            return None
    return transcript.text

def _save_uploaded_media() -> list[str]:
    uploaded_files = st.file_uploader(
        "Attach images for WordPress media",
        type=["png", "jpg", "jpeg", "gif", "webp"],
        accept_multiple_files=True,
        help=f"Uploaded files are saved locally first, then {PRODUCT_NAME} can upload them to WordPress Media.",
    )
    saved_paths: list[str] = []
    if not uploaded_files:
        return saved_paths
    upload_dir = config.upload_dir
    upload_dir.mkdir(parents=True, exist_ok=True)
    for uploaded_file in uploaded_files:
        safe_name = Path(uploaded_file.name).name.replace(" ", "_")
        destination = upload_dir / f"{int(time.time())}_{safe_name}"
        destination.write_bytes(uploaded_file.getbuffer())
        saved_paths.append(str(destination))
    if saved_paths:
        st.success(f"Attached {len(saved_paths)} image(s). Ask me to upload or use them in a page/post.")
        st.session_state.uploaded_media_paths = saved_paths
    return saved_paths


def _augment_prompt_with_uploads(prompt: str) -> str:
    paths = st.session_state.get("uploaded_media_paths", [])
    if not paths:
        return prompt
    attachments = "\n".join(f"Attached image file: {path}" for path in paths)
    st.session_state.uploaded_media_paths = []
    return f"{prompt}\n{attachments}"


def _render_memory(orchestrator: WordPressGeniusOrchestrator) -> None:
    with st.sidebar.expander("Long-term memory", expanded=False):
        memory = orchestrator.site_memory.snapshot()
        conversations = memory.get("conversations", [])[-5:]
        site_history = memory.get("site_history", [])[-5:]
        if not conversations and not site_history:
            st.caption("No remembered context yet.")
        for item in reversed(conversations):
            st.caption(f"Request: {item.get('request', '')}")
        if site_history:
            st.markdown("Recent site history")
            for item in reversed(site_history):
                st.caption(item.get("summary", ""))
        if st.button("Clear memory", use_container_width=True):
            orchestrator.site_memory.clear()
            st.success("Memory cleared.")


def _render_observability_controls() -> None:
    with st.sidebar.expander("Observability", expanded=False):
        if config.sentry_dsn:
            st.success("Sentry error tracking is enabled.")
        else:
            st.caption("Sentry is not configured.")
        consent = st.checkbox(
            "Share local usage analytics",
            value=st.session_state.get("usage_analytics_consent", False),
            help="Stores local, redacted event counts only when enabled in config and consented here.",
        )
        st.session_state.usage_analytics_consent = consent
        summary = _usage_analytics().summary()
        st.caption(f"Recorded events: {summary['total_events']}")


def _backup_zip_bytes() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(config.backup_dir.glob("wordpress_backup_*.json")):
            archive.write(path, arcname=path.name)
    return buffer.getvalue()


def _render_export_controls() -> None:
    with st.sidebar.expander("Export", expanded=False):
        if config.activity_log_path.exists():
            st.download_button(
                "Download activity log",
                data=config.activity_log_path.read_bytes(),
                file_name="wordpressgenius-activity-log.jsonl",
                mime="application/jsonl",
                use_container_width=True,
            )
        else:
            st.caption("No activity log yet.")
        backup_bytes = _backup_zip_bytes()
        if backup_bytes:
            st.download_button(
                "Download backups zip",
                data=backup_bytes,
                file_name="wordpressgenius-backups.zip",
                mime="application/zip",
                use_container_width=True,
            )
        else:
            st.caption("No backups available yet.")


def _render_activity_log(orchestrator: WordPressGeniusOrchestrator) -> None:
    with st.sidebar.expander("Recent activity", expanded=False):
        events = orchestrator.activity_log.recent(limit=8)
        if not events:
            st.caption("No activity yet.")
            return
        for event in reversed(events):
            st.caption(f"{event.created_at:%Y-%m-%d %H:%M} - {event.message}")


def _render_message(message: dict) -> None:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])
        raw_statuses = message.get("statuses", [])
        if raw_statuses:
            statuses = [AgentStatus.model_validate(status) for status in raw_statuses]
            with st.expander("Agent progress"):
                for status in statuses:
                    st.write(f"**{status.agent}** - {status.status}: {status.detail}")


def _render_pending_confirmation(
    orchestrator: WordPressGeniusOrchestrator,
    credentials: WordPressCredentials | None,
    user: AuthenticatedUser,
) -> None:
    if not st.session_state.pending_request:
        return

    st.markdown('<div class="bf-confirm">', unsafe_allow_html=True)
    st.subheader("Confirm before WordPress changes")
    st.markdown(
        f"{PRODUCT_NAME} will not publish, update, or delete anything until you "
        "explicitly approve this plan."
    )
    st.markdown(st.session_state.pending_summary)
    understood = st.checkbox(
        "I understand this will make the approved WordPress change.",
        key="confirm_understood",
    )
    col1, col2 = st.columns(2)
    with col1:
        if st.button(
            "Approve and run",
            type="primary",
            disabled=not understood,
            use_container_width=True,
        ):
            _run_agent(
                orchestrator,
                st.session_state.pending_request,
                credentials,
                approved=True,
                user=user,
                append_user=False,
            )
            st.rerun()
    with col2:
        if st.button("Cancel plan", use_container_width=True):
            st.session_state.pending_request = None
            st.session_state.pending_summary = ""
            st.session_state.messages.append(
                {
                    "role": "assistant",
                    "content": "Cancelled. I did not make any WordPress changes.",
                    "statuses": [],
                }
            )
            st.rerun()
    st.markdown("</div>", unsafe_allow_html=True)


def main() -> None:
    _init_state()
    apply_theme()
    user = _render_login()
    if not user:
        render_credit_footer()
        return
    render_sidebar_brand()
    orchestrator = WordPressGeniusOrchestrator(config)
    render_llm_settings(config, user)

    credentials: WordPressCredentials | None = None
    if role_at_least(user.role, "editor"):
        render_active_site_sidebar(config, user)
        active_site = get_active_site(config, user)
        if st.session_state.get("manage_sites") or active_site is None:
            render_site_picker(config, user, orchestrator)
            render_credit_footer()
            return
        credentials = active_site.credentials()
        _render_connection_status(orchestrator, credentials, user)
    else:
        st.sidebar.header("WordPress site")
        st.sidebar.info("Viewer role can draft plans but cannot manage WordPress sites.")

    _render_quick_actions()
    _render_memory(orchestrator)
    _render_observability_controls()
    _render_export_controls()

    render_hero(badge="Draft-first · approval-gated")

    col1, col2, col3 = st.columns(3)
    col1.info("Drafts are safe by default")
    col2.info("Major changes require approval")
    col3.info("Memory, backups, and undo are available")

    with st.expander("Media and voice", expanded=False):
        _save_uploaded_media()
        if hasattr(st, "audio_input"):
            audio_file = st.audio_input("Speak a request")
            if audio_file and st.button("Transcribe voice request"):
                transcript = _transcribe_audio(audio_file)
                if transcript:
                    st.session_state.queued_prompt = transcript
                    st.success("Voice request transcribed. It will run now.")
                    st.rerun()
        else:
            st.caption("Voice tip: use your browser or operating system dictation in the chat box.")

    if st.button("Undo last supported change", use_container_width=True):
        _run_agent(
            orchestrator,
            "Undo the last supported WordPress change.",
            credentials,
            approved=False,
            user=user,
        )
        st.rerun()

    for message in st.session_state.messages:
        _render_message(message)

    _render_pending_confirmation(orchestrator, credentials, user)

    prompt = st.chat_input("Example: Create a draft post about our new product launch")
    queued_prompt = st.session_state.queued_prompt
    if prompt or queued_prompt:
        st.session_state.queued_prompt = None
        request = _augment_prompt_with_uploads(prompt or queued_prompt)
        _run_agent(orchestrator, request, credentials, approved=False, user=user)
        st.rerun()

    _render_activity_log(orchestrator)
    render_credit_footer()


if __name__ == "__main__":
    main()
