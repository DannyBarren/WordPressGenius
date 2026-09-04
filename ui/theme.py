"""WordPressGenius theme for the Streamlit UI."""

from __future__ import annotations

import streamlit as st

PRODUCT_NAME = "WordPressGenius"
COMPANY_NAME = "WordPress & WooCommerce"
# Back-compat alias for imports that used BRAND_NAME / ASSISTANT_NAME
BRAND_NAME = COMPANY_NAME
ASSISTANT_NAME = PRODUCT_NAME
BRAND_TAGLINE = "WordPress command center"
CREATOR_NAME = "Danny Barren"
CREATOR_CREDIT = f"Created by {CREATOR_NAME}"
PAGE_TITLE = f"{PRODUCT_NAME} · {COMPANY_NAME}"
PAGE_ICON = "⚡"

THEME_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Orbitron:wght@500;700&family=Inter:wght@400;500;600;700&display=swap');

:root {
    --bf-pond-deep: #050a08;
    --bf-pond-dark: #0a1410;
    --bf-pond-mid: #0f1f18;
    --bf-card: #12261c;
    --bf-card-elevated: #1a3328;
    --bf-border: #2d5a45;
    --bf-border-glow: #39ff14;
    --bf-neon: #39ff14;
    --bf-neon-soft: #7cfc00;
    --bf-neon-dim: rgba(57, 255, 20, 0.15);
    --bf-green-dark: #1b4d3e;
    --bf-text: #e8f5e9;
    --bf-muted: #8fb39a;
    --bf-confirm-bg: #1a2e14;
    --bf-confirm-border: #39ff14;
    --bf-focus: #39ff14;
    --bf-font-display: 'Orbitron', 'Segoe UI', sans-serif;
    --bf-font-body: 'Inter', 'Segoe UI', sans-serif;
}

.stApp {
    background-color: var(--bf-pond-deep);
    background-image:
        radial-gradient(ellipse 80% 50% at 50% -20%, rgba(57, 255, 20, 0.12), transparent),
        linear-gradient(180deg, var(--bf-pond-dark) 0%, var(--bf-pond-deep) 100%);
    color: var(--bf-text);
    font-family: var(--bf-font-body);
}

.stApp header[data-testid="stHeader"] {
    background: rgba(10, 20, 16, 0.85);
    border-bottom: 1px solid var(--bf-border);
}

section[data-testid="stSidebar"] {
    background: linear-gradient(180deg, var(--bf-pond-mid) 0%, var(--bf-pond-dark) 100%);
    border-right: 1px solid var(--bf-border);
}

section[data-testid="stSidebar"] .stMarkdown,
section[data-testid="stSidebar"] label,
section[data-testid="stSidebar"] p {
    color: var(--bf-text);
}

.bf-hero {
    position: relative;
    border: 1px solid var(--bf-border);
    border-radius: 18px;
    padding: clamp(1.2rem, 2.5vw, 1.75rem);
    margin-bottom: 1rem;
    background:
        linear-gradient(135deg, rgba(27, 77, 62, 0.55) 0%, rgba(15, 31, 24, 0.95) 55%, var(--bf-pond-mid) 100%);
    box-shadow:
        0 0 40px var(--bf-neon-dim),
        inset 0 1px 0 rgba(57, 255, 20, 0.2);
    overflow: hidden;
}

.bf-hero::before {
    content: "";
    position: absolute;
    inset: 0;
    background-image:
        linear-gradient(rgba(57, 255, 20, 0.03) 1px, transparent 1px),
        linear-gradient(90deg, rgba(57, 255, 20, 0.03) 1px, transparent 1px);
    background-size: 24px 24px;
    pointer-events: none;
    opacity: 0.6;
}

.bf-hero-inner { position: relative; z-index: 1; }

.bf-hero h1 {
    font-family: var(--bf-font-display);
    font-size: clamp(1.35rem, 3vw, 1.85rem);
    font-weight: 700;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    color: var(--bf-neon);
    text-shadow: 0 0 24px rgba(57, 255, 20, 0.45);
    margin: 0 0 0.35rem 0;
}

.bf-hero .bf-subtitle {
    color: var(--bf-text);
    font-size: 1.05rem;
    font-weight: 500;
    margin: 0 0 0.5rem 0;
    max-width: 52rem;
}

.bf-hero .bf-muted {
    color: var(--bf-muted);
    font-size: 0.92rem;
    line-height: 1.5;
    margin: 0;
}

.bf-badge {
    display: inline-block;
    font-family: var(--bf-font-display);
    font-size: 0.65rem;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--bf-pond-deep);
    background: linear-gradient(90deg, var(--bf-neon), var(--bf-neon-soft));
    padding: 0.25rem 0.65rem;
    border-radius: 999px;
    margin-bottom: 0.75rem;
    font-weight: 700;
}

.bf-card {
    border: 1px solid var(--bf-border);
    border-radius: 14px;
    padding: 1rem;
    background: var(--bf-card);
    margin-bottom: 0.75rem;
}

.bf-muted { color: var(--bf-muted); font-size: 0.95rem; }

.bf-confirm {
    border: 1px solid var(--bf-confirm-border);
    border-radius: 14px;
    padding: 1rem;
    background: var(--bf-confirm-bg);
    box-shadow: 0 0 20px var(--bf-neon-dim);
}

.bf-footer {
    margin-top: 2.5rem;
    padding: 1rem 0 0.5rem;
    border-top: 1px solid var(--bf-border);
    text-align: center;
}

.bf-footer .bf-credit {
    font-family: var(--bf-font-display);
    font-size: 0.72rem;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    color: var(--bf-neon-soft);
    margin: 0 0 0.25rem 0;
}

.bf-footer .bf-powered {
    color: var(--bf-muted);
    font-size: 0.8rem;
    margin: 0;
}

.bf-sidebar-brand {
    font-family: var(--bf-font-display);
    font-size: 0.85rem;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--bf-neon);
    text-align: center;
    padding: 0.5rem 0 1rem;
    border-bottom: 1px solid var(--bf-border);
    margin-bottom: 0.75rem;
}

.stButton > button[kind="primary"],
.stButton > button[data-testid="baseButton-primary"] {
    background: linear-gradient(90deg, #2d6b4a 0%, var(--bf-green-dark) 50%, #1b4d3e 100%) !important;
    border: 1px solid var(--bf-neon) !important;
    color: var(--bf-neon) !important;
    font-weight: 600 !important;
    border-radius: 999px !important;
    box-shadow: 0 0 16px var(--bf-neon-dim) !important;
}

.stButton > button[kind="primary"]:hover,
.stButton > button[data-testid="baseButton-primary"]:hover {
    border-color: var(--bf-neon-soft) !important;
    color: var(--bf-neon-soft) !important;
    box-shadow: 0 0 24px rgba(57, 255, 20, 0.35) !important;
}

.stButton > button,
.stDownloadButton > button {
    border-radius: 999px !important;
    border-color: var(--bf-border) !important;
}

button:focus, input:focus, textarea:focus, select:focus {
    outline: 2px solid var(--bf-focus) !important;
    outline-offset: 2px !important;
    box-shadow: 0 0 12px var(--bf-neon-dim) !important;
}

div[data-testid="stMetric"], .stAlert {
    border-radius: 12px;
}

@media (max-width: 760px) {
    .block-container { padding-left: 0.75rem; padding-right: 0.75rem; }
    .bf-hero { border-radius: 12px; }
    [data-testid="column"] { width: 100% !important; flex: 1 1 100% !important; }
}
</style>
"""


def apply_theme() -> None:
    """Inject global WordPressGenius styles."""

    st.markdown(THEME_CSS, unsafe_allow_html=True)


def render_sidebar_brand() -> None:
    st.sidebar.markdown(
        f"""
        <div class="bf-sidebar-brand">
            {PRODUCT_NAME}<br>
            <span style="font-size:0.55rem;color:var(--bf-muted);letter-spacing:0.06em;">
                {COMPANY_NAME}
            </span>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_hero(
    *,
    title: str | None = None,
    subtitle: str | None = None,
    body: str | None = None,
    badge: str = "Performance systems online",
) -> None:
    heading = title or PRODUCT_NAME
    sub = subtitle or COMPANY_NAME
    detail = body or (
        f"{PRODUCT_NAME} is your site assistant for {COMPANY_NAME}. "
        "Tell it what you want changed—it plans carefully, asks before risky actions, "
        "and reports back in plain English."
    )
    st.markdown(
        f"""
        <div class="bf-hero">
            <div class="bf-hero-inner">
                <span class="bf-badge">{badge}</span>
                <h1>{heading}</h1>
                <p class="bf-subtitle">{sub}</p>
                <p class="bf-muted">{detail}</p>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_credit_footer() -> None:
    st.markdown(
        f"""
        <div class="bf-footer">
            <p class="bf-credit">{CREATOR_CREDIT}</p>
            <p class="bf-powered">{PRODUCT_NAME} for {COMPANY_NAME}</p>
        </div>
        """,
        unsafe_allow_html=True,
    )
