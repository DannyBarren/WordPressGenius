"""Start the WordPressGenius web UI with only Python installed."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent


def main() -> int:
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))

    from environment_setup import ensure_streamlit_installed

    print("WordPressGenius")
    print(f"Python: {sys.executable}")

    if not ensure_streamlit_installed(print):
        print("Could not install Streamlit. Install Python 3.10+ and try again.")
        return 1

    print("Opening web UI at http://localhost:8501")
    print("Created by Danny Barren")
    print("Use the setup screen to install remaining dependencies on first run.")
    command = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(PROJECT_ROOT / "app.py"),
        "--server.headless",
        "true",
    ]
    return subprocess.call(command, cwd=PROJECT_ROOT)


if __name__ == "__main__":
    raise SystemExit(main())
