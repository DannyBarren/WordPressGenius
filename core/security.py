"""Authentication, credential vault, audit, and prompt guardrails."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Literal

import yaml
from cryptography.fernet import Fernet, InvalidToken

from core.logging_config import redact
from core.models import WordPressCredentials


AppRole = Literal["viewer", "editor", "admin"]

ROLE_RANK: dict[str, int] = {"viewer": 0, "editor": 1, "admin": 2}


@dataclass(frozen=True)
class AuthenticatedUser:
    """Application user authenticated into WordPressGenius."""

    username: str
    role: AppRole


@dataclass(frozen=True)
class PromptValidationResult:
    """Validated prompt text and any warnings produced by guardrails."""

    allowed: bool
    sanitized_text: str
    warnings: list[str]


class AuthManager:
    """Simple YAML-backed authentication for self-hosted small teams.

    The users file should contain:

    ```yaml
    users:
      alice:
        password_sha256: "<sha256 hex>"
        role: admin
    ```

    This is intentionally simple and self-host friendly. Production SaaS should
    replace it with Auth0, Supabase, Firebase Auth, or another identity provider.
    """

    def __init__(self, users_path: Path, *, enabled: bool = True) -> None:
        self.users_path = users_path
        self.enabled = enabled

    def authenticate(self, username: str, password: str) -> AuthenticatedUser | None:
        """Return an authenticated user when credentials match."""

        if not self.enabled:
            return AuthenticatedUser(username="local-admin", role="admin")
        users = self._load_users()
        record = users.get(username)
        if not record:
            return None
        expected = str(record.get("password_sha256", ""))
        supplied = hashlib.sha256(password.encode("utf-8")).hexdigest()
        if not hmac.compare_digest(expected, supplied):
            return None
        role = str(record.get("role", "viewer")).lower()
        if role not in ROLE_RANK:
            role = "viewer"
        return AuthenticatedUser(username=username, role=role)  # type: ignore[arg-type]

    def _load_users(self) -> dict[str, dict[str, Any]]:
        if not self.users_path.exists():
            return {}
        data = yaml.safe_load(self.users_path.read_text(encoding="utf-8")) or {}
        users = data.get("users", {})
        return users if isinstance(users, dict) else {}


class CredentialVault:
    """Encrypted file-backed WordPress credential storage per app user."""

    def __init__(self, path: Path, key: str) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fernet = make_fernet(key)

    def save(self, user: AuthenticatedUser, credentials: WordPressCredentials) -> None:
        """Encrypt and store WordPress credentials for *user*."""

        data = self._load()
        data[user.username] = credentials.model_dump(mode="json")
        self._write(data)

    def load(self, user: AuthenticatedUser) -> WordPressCredentials | None:
        """Load decrypted WordPress credentials for *user*, if present."""

        data = self._load()
        raw = data.get(user.username)
        if not raw:
            return None
        return WordPressCredentials.model_validate(raw)

    def delete(self, user: AuthenticatedUser) -> None:
        """Remove stored credentials for *user*."""

        data = self._load()
        data.pop(user.username, None)
        self._write(data)

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            decrypted = self.fernet.decrypt(self.path.read_bytes())
        except InvalidToken:
            return {}
        return json.loads(decrypted.decode("utf-8"))

    def _write(self, data: dict[str, Any]) -> None:
        encoded = json.dumps(data, sort_keys=True).encode("utf-8")
        self.path.write_bytes(self.fernet.encrypt(encoded))
        try:
            self.path.chmod(0o600)
        except OSError:
            pass


GENESIS_HASH = "0" * 64


class SecurityAuditLog:
    """Append-only, hash-chained audit log with per-user attribution.

    Every record carries a sequence number, the previous record's hash, and its
    own hash computed over its canonical content. This makes the log
    tamper-evident: editing, reordering, or deleting any record breaks the chain,
    which :meth:`verify` detects. Credentials are always redacted before writing.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(
        self,
        *,
        user: AuthenticatedUser | None,
        event_type: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        record = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "event_type": event_type,
            "message": redact(message),
            "username": user.username if user else "anonymous",
            "role": user.role if user else "anonymous",
            "details": _sanitize(details or {}),
        }
        with self._lock:
            prev_hash, prev_seq = self._tail()
            record["seq"] = prev_seq + 1
            record["prev_hash"] = prev_hash
            record["hash"] = _chain_hash(prev_hash, record)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, sort_keys=True) + "\n")
        return record

    def read(self, limit: int | None = None) -> list[dict[str, Any]]:
        """Return parsed audit records (most recent last)."""

        if not self.path.exists():
            return []
        with self._lock:
            rows = self.path.read_text(encoding="utf-8").splitlines()
        records: list[dict[str, Any]] = []
        for row in rows:
            row = row.strip()
            if not row:
                continue
            try:
                records.append(json.loads(row))
            except json.JSONDecodeError:
                continue
        return records[-limit:] if limit else records

    def verify(self) -> dict[str, Any]:
        """Recompute the hash chain and report any tampering.

        Returns ``{"ok": bool, "count": int, "problems": [str, ...]}``.
        """

        records = self.read()
        problems: list[str] = []
        prev_hash = GENESIS_HASH
        for index, record in enumerate(records):
            if not isinstance(record, dict) or "hash" not in record:
                problems.append(f"Record {index} is malformed or missing its hash.")
                continue
            if record.get("prev_hash") != prev_hash:
                problems.append(f"Record {index} (seq {record.get('seq')}) has a broken previous-hash link.")
            expected = _chain_hash(record.get("prev_hash", prev_hash), record)
            if record.get("hash") != expected:
                problems.append(f"Record {index} (seq {record.get('seq')}) content does not match its hash.")
            prev_hash = record.get("hash", prev_hash)
        return {"ok": not problems, "count": len(records), "problems": problems}

    def _tail(self) -> tuple[str, int]:
        if not self.path.exists():
            return GENESIS_HASH, 0
        last: dict[str, Any] | None = None
        for row in self.path.read_text(encoding="utf-8").splitlines():
            row = row.strip()
            if not row:
                continue
            try:
                last = json.loads(row)
            except json.JSONDecodeError:
                continue
        if not isinstance(last, dict):
            return GENESIS_HASH, 0
        return str(last.get("hash", GENESIS_HASH)), int(last.get("seq", 0))


class PromptGuard:
    """Input validation and lightweight prompt-injection guardrails.

    Three tiers:
    - INJECTION/EXFIL patterns block instruction-override and secret-stealing.
    - DESTRUCTIVE patterns block obviously catastrophic mass-deletion or
      shell/SQL injection attempts (defense in depth; the agent never runs shell
      or SQL, but we refuse to even plan from such input).
    - SUSPICIOUS patterns are allowed but attach a warning so the orchestrator can
      record it in the audit log for review.
    """

    DANGEROUS_PATTERNS = (
        re.compile(r"ignore (all )?(previous|prior|above|system|developer) instructions", re.I),
        re.compile(r"disregard (all )?(previous|prior|the) (instructions|rules|guardrails)", re.I),
        re.compile(r"reveal (the )?(system prompt|developer message|instructions|secrets?)", re.I),
        re.compile(r"(print|show|dump|expose|leak).{0,30}(api[ _-]?key|password|token|secret|credential)", re.I),
        re.compile(r"you are now (a|an|in) .{0,40}(developer|jailbreak|dan|unfiltered) mode", re.I),
        re.compile(r"pretend (you|that you) (are|have) no (rules|restrictions|guardrails)", re.I),
    )

    DESTRUCTIVE_PATTERNS = (
        re.compile(r"delete (all|every|the entire|all of (my|the)) (posts?|pages?|products?|content|site)", re.I),
        re.compile(r"(remove|wipe|erase|destroy) (everything|the (whole|entire) site|all data)", re.I),
        re.compile(r"\brm\s+-rf\b", re.I),
        re.compile(r"\b(drop|truncate)\s+table\b", re.I),
        re.compile(r"\bdelete\s+from\s+wp_", re.I),
    )

    SUSPICIOUS_PATTERNS = (
        re.compile(r"<\s*script", re.I),
        re.compile(r"javascript:", re.I),
        re.compile(r"base64,", re.I),
        re.compile(r"\b(curl|wget)\s+https?://", re.I),
    )

    def __init__(self, max_length: int) -> None:
        self.max_length = max_length

    def validate(self, prompt: str) -> PromptValidationResult:
        """Validate and sanitize user prompt text before orchestration."""

        sanitized = _normalize_text(prompt)
        warnings: list[str] = []
        if not sanitized:
            return PromptValidationResult(False, "", ["Prompt is empty after sanitization."])
        if len(sanitized) > self.max_length:
            return PromptValidationResult(
                allowed=False,
                sanitized_text=sanitized[: self.max_length],
                warnings=[f"Prompt exceeds {self.max_length} characters."],
            )
        for pattern in self.DANGEROUS_PATTERNS:
            if pattern.search(sanitized):
                warnings.append("Prompt includes instruction-override or secret-exfiltration language.")
                return PromptValidationResult(False, sanitized, warnings)
        for pattern in self.DESTRUCTIVE_PATTERNS:
            if pattern.search(sanitized):
                warnings.append("Prompt requests mass deletion or destructive commands.")
                return PromptValidationResult(False, sanitized, warnings)
        for pattern in self.SUSPICIOUS_PATTERNS:
            if pattern.search(sanitized):
                warnings.append("Prompt contains potentially unsafe markup or links; proceeding with caution.")
                break
        return PromptValidationResult(True, sanitized, warnings)


def _chain_hash(prev_hash: str, record: dict[str, Any]) -> str:
    """Hash a record's canonical content chained to the previous hash."""

    payload = {key: value for key, value in record.items() if key != "hash"}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"{prev_hash}{canonical}".encode("utf-8")).hexdigest()


def role_at_least(role: str, required: str) -> bool:
    """Return True if *role* has at least *required* privileges."""

    return ROLE_RANK.get(role, -1) >= ROLE_RANK.get(required, 99)


def hash_password(password: str) -> str:
    """Return a SHA-256 hash for simple self-hosted user config files."""

    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def generate_fernet_key() -> str:
    """Return a new Fernet key suitable for CREDENTIAL_ENCRYPTION_KEY."""

    return Fernet.generate_key().decode("ascii")


def make_fernet(key: str) -> Fernet:
    """Build a Fernet cipher from a configured key, deriving one if needed.

    Falls back to an ephemeral per-process key when none is provided so the app
    still runs locally; persisted data then only survives within that process.
    """

    if not key:
        key = _ephemeral_key()
    return Fernet(_normalize_fernet_key(key))


def _normalize_fernet_key(value: str) -> bytes:
    raw = value.encode("utf-8")
    try:
        Fernet(raw)
        return raw
    except (ValueError, TypeError):
        digest = hashlib.sha256(raw).digest()
        return base64.urlsafe_b64encode(digest)


def _ephemeral_key() -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(os.urandom(32)).digest()).decode("ascii")


_ZERO_WIDTH = {"\u200b", "\u200c", "\u200d", "\u2060", "\ufeff"}


def _strip_control_chars(value: str) -> str:
    return "".join(ch for ch in value if ch == "\n" or ch == "\t" or ord(ch) >= 32)


def _normalize_text(value: str) -> str:
    """Normalize prompt text: NFKC, remove zero-width/control chars, trim runs.

    Zero-width characters are a common prompt-injection obfuscation trick, so we
    drop them before pattern matching.
    """

    import unicodedata

    normalized = unicodedata.normalize("NFKC", value or "")
    without_zero_width = "".join(ch for ch in normalized if ch not in _ZERO_WIDTH)
    stripped = _strip_control_chars(without_zero_width)
    # Collapse pathological character runs (e.g. flooding) to keep prompts sane.
    collapsed = re.sub(r"(.)\1{200,}", lambda m: m.group(1) * 200, stripped)
    return collapsed.strip()


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for key, item in value.items():
            lowered = key.lower()
            if any(secret in lowered for secret in ["password", "authorization", "token", "secret", "api_key"]):
                clean[key] = "[REDACTED]"
            else:
                clean[key] = _sanitize(item)
        return clean
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    if isinstance(value, str):
        return redact(value)
    return value
