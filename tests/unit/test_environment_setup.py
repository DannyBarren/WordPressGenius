from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import environment_setup as setup


def test_check_environment_reports_missing_packages(monkeypatch) -> None:
    monkeypatch.setattr(setup, "_missing_runtime_imports", lambda: ["streamlit"])

    status = setup.check_environment()

    assert status.ready is False
    assert status.python_ok is True
    assert "streamlit" in status.missing_packages


def test_check_environment_ready_when_imports_ok(monkeypatch) -> None:
    monkeypatch.setattr(setup, "_missing_runtime_imports", lambda: [])
    monkeypatch.setattr(setup, "_check_application_imports", lambda: None)

    status = setup.check_environment()

    assert status.ready is True


def test_run_setup_fails_when_requirements_install_fails(monkeypatch) -> None:
    monkeypatch.setattr(setup, "_prepare_runtime_directories", lambda log: None)

    # Let the pip upgrade succeed, then fail on the requirements install step.
    def fake_pip(args, log):
        return 0 if args == ["--upgrade", "pip"] else 1

    monkeypatch.setattr(setup, "_pip_install", fake_pip)

    result = setup.run_setup()

    assert result.success is False
    assert "installation failed" in result.message.lower()


def test_run_setup_fails_when_pip_upgrade_fails(monkeypatch) -> None:
    monkeypatch.setattr(setup, "_prepare_runtime_directories", lambda log: None)
    monkeypatch.setattr(setup, "_pip_install", lambda args, log: 1)

    result = setup.run_setup()

    assert result.success is False
    assert result.message


def test_run_setup_success_path(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(setup, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(setup, "REQUIREMENTS_FILE", tmp_path / "requirements.txt")
    monkeypatch.setattr(setup, "ENV_MARKER_FILE", tmp_path / "data" / ".environment_ready.json")
    (tmp_path / "requirements.txt").write_text("streamlit\n", encoding="utf-8")

    monkeypatch.setattr(setup, "_prepare_runtime_directories", lambda log: None)
    monkeypatch.setattr(setup, "_pip_install", lambda args, log: 0)
    monkeypatch.setattr(setup, "_missing_runtime_imports", lambda: [])
    monkeypatch.setattr(setup, "_check_application_imports", lambda: None)
    monkeypatch.setattr(setup, "run_readiness_tests", lambda: (True, "ok"))

    result = setup.run_setup()

    assert result.success is True
    assert setup.ENV_MARKER_FILE.exists()


def test_ensure_streamlit_installed_skips_when_present() -> None:
    assert setup.ensure_streamlit_installed() is True


def test_ensure_streamlit_installed_runs_pip(monkeypatch) -> None:
    import importlib

    real_import_module = importlib.import_module

    def fake_import_module(name: str):
        if name == "streamlit":
            raise ImportError
        return real_import_module(name)

    monkeypatch.setattr(importlib, "import_module", fake_import_module)
    pip = MagicMock(return_value=0)
    monkeypatch.setattr(setup, "_pip_install", lambda args, log: pip(args, log))

    assert setup.ensure_streamlit_installed() is True
    pip.assert_called_once()
