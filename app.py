"""Bootstrap Streamlit entrypoint for WordPressGenius.

Launches with only Streamlit installed (via ``python launch.py``). When full
dependencies are missing, shows a setup screen with an install button.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _import_streamlit():
    try:
        import streamlit as st

        return st
    except ImportError:
        print(
            "Streamlit is not installed.\n"
            "Run: python launch.py\n"
            "Or: python -m pip install streamlit && streamlit run app.py"
        )
        raise SystemExit(1) from None


def _running_under_streamlit() -> bool:
    """Return True only when launched via ``streamlit run`` (has a script context)."""

    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx
    except ImportError:
        try:
            from streamlit.scriptrunner import get_script_run_ctx  # type: ignore
        except ImportError:
            return False
    try:
        return get_script_run_ctx() is not None
    except Exception:  # noqa: BLE001 - any failure means no active context
        return False


def _relaunch_through_streamlit() -> int:
    """Re-exec the app via the launcher so ``python app.py`` works for users."""

    import subprocess

    print("Starting WordPressGenius web UI via Streamlit...")
    print("(Tip: launch directly with `python launch.py`.)")
    return subprocess.call([sys.executable, str(ROOT / "launch.py")])


def main() -> None:
    st = _import_streamlit()
    from ui.theme import PAGE_ICON, PAGE_TITLE

    st.set_page_config(
        page_title=PAGE_TITLE,
        page_icon=PAGE_ICON,
        layout="wide",
        initial_sidebar_state="expanded",
    )

    from environment_setup import check_environment
    from ui.setup_page import render_setup_page

    status = check_environment()
    if not status.ready:
        render_setup_page(status)
        return

    from ui.app_main import main as run_application

    run_application()


if __name__ == "__main__":
    if _running_under_streamlit():
        main()
    else:
        raise SystemExit(_relaunch_through_streamlit())
