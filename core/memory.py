"""Simple durable memory for conversations and WordPress changes."""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from threading import Lock
from typing import Any

from core.logging_config import redact
from core.models import ActivityEvent


class ActivityLog:
    """Append-only JSONL activity log.

    The log intentionally stores operational metadata and human-readable
    summaries, never WordPress credentials.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(
        self, event_type: str, message: str, details: dict[str, Any] | None = None
    ) -> ActivityEvent:
        event = ActivityEvent(
            event_type=event_type,
            message=redact(message),
            details=_sanitize(details or {}),
        )
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(event.model_dump_json() + "\n")
        return event

    def recent(self, limit: int = 20) -> list[ActivityEvent]:
        if not self.path.exists():
            return []
        with self._lock:
            rows = self.path.read_text(encoding="utf-8").splitlines()
        events: list[ActivityEvent] = []
        for row in rows[-limit:]:
            try:
                events.append(ActivityEvent.model_validate(json.loads(row)))
            except (json.JSONDecodeError, ValueError):
                continue
        return events


class SiteMemory:
    """Durable long-term memory for business and site context.

    The memory file stores summaries and safe metadata only. Credentials and raw
    authentication headers must never be written here.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            if not self.path.exists():
                return self._empty()
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return self._empty()
        base = self._empty()
        base.update(data if isinstance(data, dict) else {})
        base["conversations"] = base.get("conversations", [])[-50:]
        base["site_history"] = base.get("site_history", [])[-100:]
        return base

    def remember_conversation(
        self,
        user_request: str,
        assistant_response: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        data = self.snapshot()
        data.setdefault("conversations", []).append(
            {
                "request": _truncate(redact(user_request), 500),
                "response_summary": _truncate(redact(assistant_response), 700),
                "metadata": _sanitize(metadata or {}),
            }
        )
        data["conversations"] = data["conversations"][-50:]
        self._write(data)

    def remember_site_event(
        self, event_type: str, summary: str, details: dict[str, Any] | None = None
    ) -> None:
        data = self.snapshot()
        data.setdefault("site_history", []).append(
            {
                "event_type": event_type,
                "summary": _truncate(redact(summary), 700),
                "details": _sanitize(details or {}),
            }
        )
        data["site_history"] = data["site_history"][-100:]
        self._write(data)

    def update_business_profile(self, hints: dict[str, Any]) -> None:
        clean_hints = {key: value for key, value in hints.items() if value}
        if not clean_hints:
            return
        data = self.snapshot()
        profile = data.setdefault("business_profile", {})
        profile.update(clean_hints)
        self._write(data)

    def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        """Return stored memories most relevant to *query*.

        A dependency-free lexical retriever (TF weighted by inverse document
        frequency) over past conversations and site events. This gives the
        Researcher targeted long-term recall without embeddings or a vector DB,
        which keeps the self-hosted footprint small while still acting as a
        site/business RAG layer.
        """

        query_tokens = _tokenize(query)
        if not query_tokens:
            return []
        data = self.snapshot()
        documents: list[dict[str, Any]] = []
        for convo in data.get("conversations", []):
            text = f"{convo.get('request', '')} {convo.get('response_summary', '')}".strip()
            if text:
                documents.append({"kind": "conversation", "text": text})
        for event in data.get("site_history", []):
            text = f"{event.get('event_type', '')} {event.get('summary', '')}".strip()
            if text:
                documents.append({"kind": "site_event", "text": text})
        if not documents:
            return []

        token_sets = [set(_tokenize(doc["text"])) for doc in documents]
        total = len(documents)
        doc_freq: dict[str, int] = {}
        for tokens in token_sets:
            for token in tokens:
                doc_freq[token] = doc_freq.get(token, 0) + 1

        scored: list[dict[str, Any]] = []
        for doc, tokens in zip(documents, token_sets):
            score = 0.0
            for token in query_tokens:
                if token in tokens:
                    idf = math.log(1 + total / (1 + doc_freq.get(token, 0)))
                    score += idf
            if score > 0:
                scored.append({**doc, "score": round(score, 4)})
        scored.sort(key=lambda item: item["score"], reverse=True)
        for item in scored[:limit]:
            item["text"] = _truncate(item["text"], 280)
        return scored[:limit]

    def clear(self) -> None:
        self._write(self._empty())

    def _write(self, data: dict[str, Any]) -> None:
        with self._lock:
            self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def _empty(self) -> dict[str, Any]:
        return {
            "business_profile": {},
            "conversations": [],
            "site_history": [],
        }


_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "your", "you", "are", "from",
    "have", "has", "was", "were", "will", "can", "please", "create", "update",
    "make", "site", "page", "post", "about", "into", "all",
}


def _tokenize(text: str) -> list[str]:
    words = re.findall(r"[a-z0-9]{3,}", str(text).lower())
    return [word for word in words if word not in _STOPWORDS]


def _truncate(value: str, limit: int) -> str:
    value = " ".join(str(value).split())
    if len(value) <= limit:
        return value
    return value[: limit - 3].rstrip() + "..."



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
