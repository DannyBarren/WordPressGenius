"""Install dependencies, prepare runtime folders, and verify WordPressGenius is ready."""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
REQUIREMENTS_FILE = PROJECT_ROOT / "requirements.txt"
MINIMAL_REQUIREMENTS_FILE = PROJECT_ROOT / "requirements-minimal.txt"
ENV_MARKER_FILE = PROJECT_ROOT / "data" / ".environment_ready.json"
MIN_PYTHON = (3, 10)

# import_name -> pip package name (for error messages)
RUNTIME_IMPORTS: tuple[tuple[str, str], ...] = (
    ("streamlit", "streamlit"),
    ("requests", "requests"),
    ("dotenv", "python-dotenv"),
    ("pydantic", "pydantic"),
    ("langgraph", "langgraph"),
    ("openai", "openai"),
    ("anthropic", "anthropic"),
    ("bs4", "beautifulsoup4"),
    ("slugify", "python-slugify"),
    ("fastapi", "fastapi"),
    ("uvicorn", "uvicorn"),
    ("redis", "redis"),
    ("cryptography", "cryptography"),
    ("yaml", "PyYAML"),
    ("sentry_sdk", "sentry-sdk"),
)

READINESS_TEST_TARGETS = (
    "tests/unit/test_config_logging.py",
    "tests/unit/test_safety.py",
)


@dataclass(frozen=True)
class EnvironmentStatus:
    ready: bool
    python_ok: bool
    python_version: str
    missing_packages: tuple[str, ...] = ()
    message: str = ""


@dataclass
class SetupResult:
    success: bool
    logs: list[str] = field(default_factory=list)
    message: str = ""


def python_version_ok() -> bool:
    return sys.version_info[:2] >= MIN_PYTHON


def check_environment() -> EnvironmentStatus:
    """Return whether required runtime packages and app modules are importable."""

    version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    if not python_version_ok():
        return EnvironmentStatus(
            ready=False,
            python_ok=False,
            python_version=version,
            message=f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ is required.",
        )

    missing = list(_missing_runtime_imports())
    if missing:
        return EnvironmentStatus(
            ready=False,
            python_ok=True,
            python_version=version,
            missing_packages=tuple(missing),
            message="Install dependencies to continue.",
        )

    app_error = _check_application_imports()
    if app_error:
        return EnvironmentStatus(
            ready=False,
            python_ok=True,
            python_version=version,
            message=app_error,
        )

    return EnvironmentStatus(
        ready=True,
        python_ok=True,
        python_version=version,
        message="Environment is ready.",
    )


BOOTSTRAP_IMPORTS: tuple[tuple[str, str], ...] = (
    ("streamlit", "streamlit"),
    # Streamlit parses .streamlit/config.toml on startup and needs a TOML parser.
    ("toml", "toml"),
)


def ensure_streamlit_installed(log: Callable[[str], None] | None = None) -> bool:
    """Install the minimal packages (Streamlit + TOML parser) so the UI can open."""

    missing_imports: list[str] = []
    missing_packages: list[str] = []
    for import_name, package_name in BOOTSTRAP_IMPORTS:
        try:
            importlib.import_module(import_name)
        except ImportError:
            missing_imports.append(import_name)
            missing_packages.append(package_name)

    if not missing_imports:
        return True

    if MINIMAL_REQUIREMENTS_FILE.exists():
        return _pip_install(["-r", str(MINIMAL_REQUIREMENTS_FILE)], log) == 0

    return _pip_install(missing_packages, log) == 0


def run_setup(log: Callable[[str], None] | None = None) -> SetupResult:
    """Install requirements, prepare folders, verify imports, and run readiness tests."""

    logs: list[str] = []

    def emit(message: str) -> None:
        logs.append(message)
        if log:
            log(message)

    if not python_version_ok():
        message = f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ is required."
        emit(message)
        return SetupResult(success=False, logs=logs, message=message)

    emit(f"Using Python {sys.executable} ({sys.version.split()[0]})")
    emit("Preparing data and log folders...")
    _prepare_runtime_directories(emit)

    emit("Upgrading pip...")
    if _pip_install(["--upgrade", "pip"], emit) != 0:
        message = "Could not upgrade pip."
        return SetupResult(success=False, logs=logs, message=message)

    if not REQUIREMENTS_FILE.exists():
        message = f"Missing requirements file: {REQUIREMENTS_FILE}"
        emit(message)
        return SetupResult(success=False, logs=logs, message=message)

    emit(f"Installing packages from {REQUIREMENTS_FILE.name}...")
    if _pip_install(["-r", str(REQUIREMENTS_FILE)], emit) != 0:
        message = "Package installation failed. Review the log above."
        return SetupResult(success=False, logs=logs, message=message)

    missing = _missing_runtime_imports()
    if missing:
        message = f"Packages still missing after install: {', '.join(missing)}"
        emit(message)
        return SetupResult(success=False, logs=logs, message=message)

    emit("Verifying WordPressGenius application modules...")
    app_error = _check_application_imports()
    if app_error:
        emit(app_error)
        return SetupResult(success=False, logs=logs, message=app_error)

    emit("Running readiness tests...")
    tests_ok, test_output = run_readiness_tests()
    for line in test_output.splitlines():
        if line.strip():
            emit(line)
    if not tests_ok:
        message = "Readiness tests failed. Fix the errors above and try again."
        return SetupResult(success=False, logs=logs, message=message)

    _write_environment_marker()
    emit("Environment is ready. Opening WordPressGenius...")
    return SetupResult(success=True, logs=logs, message="Environment is ready.")


def run_readiness_tests() -> tuple[bool, str]:
    """Run a small pytest subset to confirm the install works."""

    existing = [target for target in READINESS_TEST_TARGETS if (PROJECT_ROOT / target).exists()]
    if not existing:
        return _smoke_import_check()

    command = [sys.executable, "-m", "pytest", *existing, "-q", "--tb=short"]
    completed = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    output = "\n".join(part for part in (completed.stdout, completed.stderr) if part).strip()
    if completed.returncode == 0:
        return True, output or "Readiness tests passed."
    return False, output or "Readiness tests failed."


def _missing_runtime_imports() -> list[str]:
    missing: list[str] = []
    for import_name, package_name in RUNTIME_IMPORTS:
        try:
            importlib.import_module(import_name)
        except ImportError:
            missing.append(package_name)
    return missing


def _check_application_imports() -> str | None:
    modules = (
        "core.config",
        "core.orchestrator",
        "agents.crew",
        "tools.wordpress_client",
    )
    for module_name in modules:
        try:
            importlib.import_module(module_name)
        except Exception as exc:  # noqa: BLE001 - surface setup failures clearly
            return f"Could not load {module_name}: {exc}"
    return None


def _smoke_import_check() -> tuple[bool, str]:
    app_error = _check_application_imports()
    if app_error:
        return False, app_error
    return True, "Core modules imported successfully."


def _prepare_runtime_directories(log: Callable[[str], None]) -> None:
    for path in (PROJECT_ROOT / "data", PROJECT_ROOT / "logs"):
        path.mkdir(parents=True, exist_ok=True)
        log(f"  {path}")

    env_example = PROJECT_ROOT / "config" / ".env.example"
    env_file = PROJECT_ROOT / ".env"
    if env_example.exists() and not env_file.exists():
        env_file.write_text(env_example.read_text(encoding="utf-8"), encoding="utf-8")
        log(f"Created {env_file.name} from config/.env.example")


def _pip_install(args: list[str], log: Callable[[str], None] | None) -> int:
    command = [sys.executable, "-m", "pip", "install", *args]
    if log:
        log(f"$ {' '.join(command)}")
    completed = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    output = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
    if log and output.strip():
        for line in output.splitlines()[-40:]:
            log(line)
    return completed.returncode


def _write_environment_marker() -> None:
    ENV_MARKER_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "ready": True,
        "python": sys.version,
        "executable": sys.executable,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    ENV_MARKER_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
