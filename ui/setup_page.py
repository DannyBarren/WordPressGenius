"""First-run setup screen shown before dependencies are installed."""

from __future__ import annotations

import streamlit as st

from environment_setup import EnvironmentStatus, check_environment, run_setup
from ui.theme import COMPANY_NAME, PRODUCT_NAME, apply_theme, render_credit_footer, render_hero


def render_setup_page(status: EnvironmentStatus) -> None:
    apply_theme()
    render_hero(
        title="Environment setup",
        subtitle=f"Prepare {PRODUCT_NAME} for {COMPANY_NAME}",
        body=(
            f"Welcome to {PRODUCT_NAME}. Install the required tools and Python packages, "
            f"then this instance will verify everything is ready to run for {COMPANY_NAME}."
        ),
        badge="First launch · environment setup",
    )

    st.info(f"Python {status.python_version} detected.")

    if status.missing_packages:
        st.warning(
            "Missing packages: "
            + ", ".join(status.missing_packages)
        )
    elif status.message and not status.ready:
        st.warning(status.message)

    st.markdown(
        f"""
        The setup program will:

        1. Create `data/` and `logs/` folders (and a starter `.env` if needed)
        2. Upgrade `pip` and install everything in `requirements.txt`
        3. Verify {PRODUCT_NAME} modules load correctly
        4. Run a short automated readiness test
        """
    )

    log_box = st.empty()
    logs: list[str] = []

    def append_log(message: str) -> None:
        logs.append(message)
        log_box.code("\n".join(logs[-60:]), language="text")

    if st.button("Install and set up environment", type="primary", use_container_width=True):
        with st.status("Setting up environment...", expanded=True) as setup_status:
            result = run_setup(append_log)
            for line in result.logs:
                setup_status.write(line)
            if result.success:
                setup_status.update(label="Setup complete", state="complete")
                st.success(result.message)
                st.balloons()
                st.rerun()
            setup_status.update(label="Setup needs attention", state="error")
            st.error(result.message)

    st.caption(
        "Tip: from a terminal you can also run `python launch.py` to open this screen "
        "with only Python installed."
    )
    render_credit_footer()
