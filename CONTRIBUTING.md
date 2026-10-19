# Contributing to WordPressGenius

This is a small, safety-first codebase. Changes should keep the existing pytest suite green and should not weaken the approval gates.

## Setup

Python 3.10+ (CI uses 3.11).

```bash
python -m pip install -r requirements.txt
cp .env.example .env
cp config/users.example.yml config/users.yml
```

The example login password is `change-me`. Do not commit `config/users.yml`, `.env`, vault files, or anything under `data/` that holds secrets or live site state.

Run the UI with `python launch.py` or `streamlit run app.py`.

## Tests

```bash
python -m pytest -q
```

`pytest.ini` collects `tests/` and `evals/`. Unit and integration tests mock WordPress with `responses`. Do not add tests that need a live site or a real API key.

First-run readiness (`environment_setup.py`) runs `tests/unit/test_config_logging.py` and `tests/unit/test_safety.py`. Keep those files fast and offline.

## Golden-prompt evals

`evals/` is an offline harness. A scripted model returns fixture plans. `SafetyLayer` is the system under test.

```bash
python -m evals
python -m pytest evals -q
```

Destructive cases (publish, delete, refund, WooCommerce writes, settings, plugins, and the rest) must expect `requires_confirmation: true`. If you add a new write operation, add a golden case that labels it `risk: low` and still requires approval.

Cases live in `evals/cases.yaml`. The runner is `evals/harness.py`.

## Password hashes

New hashes must come from `core.security.hash_password` (Argon2id). Quote the string in YAML:

```bash
python -c "from core.security import hash_password; print(hash_password('your-password'))"
```

Leave `config/users.example.yml` as a clearly fake example. It may still contain the documented SHA-256 digest of `change-me`; login upgrades that row to Argon2id.

## Dependencies

Direct dependencies are pinned with `==` in `requirements.txt`. Bootstrap pins live in `requirements-minimal.txt`. If you add a package, pin it and add a runtime import to `environment_setup.RUNTIME_IMPORTS` when the app cannot start without it.

Do not commit secrets, API keys, or lock files that embed credentials.

## Pull requests

- One concern per PR when you can.
- Keep approval, backup, and permission decisions in Python.
- Update `evals/cases.yaml` when you add or change a gated operation.
- Say what you tested and what you skipped (live WordPress, paid model calls, Docker, browsers).
