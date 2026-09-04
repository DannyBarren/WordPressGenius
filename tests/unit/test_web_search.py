from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import patch

from agents.crew import build_wordpress_crew
from core.safety import SafetyLayer
from tools.web_search import run_web_search, web_search


def _fake_duckduckgo(snippet: str):
    captured: dict = {}

    class FakeWrapper:
        def __init__(self, max_results: int = 6) -> None:
            captured["max_results"] = max_results

    class FakeRun:
        def __init__(self, api_wrapper=None) -> None:
            self.api_wrapper = api_wrapper

        def run(self, query: str) -> str:
            captured["query"] = query
            return snippet

    modules = {
        "langchain_community": SimpleNamespace(),
        "langchain_community.tools": SimpleNamespace(DuckDuckGoSearchRun=FakeRun),
        "langchain_community.utilities": SimpleNamespace(DuckDuckGoSearchAPIWrapper=FakeWrapper),
    }
    return modules, captured


def test_run_web_search_returns_snippets() -> None:
    modules, captured = _fake_duckduckgo("Whey protein: 24g per scoop, $39.99.")
    with patch.dict(sys.modules, modules):
        result = run_web_search("best whey protein 2026", max_results=4)
    assert result == "Whey protein: 24g per scoop, $39.99."
    assert captured["query"] == "best whey protein 2026"
    assert captured["max_results"] == 4


def test_run_web_search_clamps_result_count() -> None:
    modules, captured = _fake_duckduckgo("x")
    with patch.dict(sys.modules, modules):
        run_web_search("query", max_results=99)
    assert captured["max_results"] == 10


def test_run_web_search_empty_query_returns_blank() -> None:
    assert run_web_search("   ") == ""


def test_run_web_search_rejects_unsafe_query() -> None:
    assert run_web_search("file:///etc/passwd") == ""
    assert run_web_search("<script>alert(1)</script>") == ""


def test_run_web_search_blank_when_dependency_missing() -> None:
    # Force the import inside run_web_search to fail.
    with patch.dict(sys.modules, {"langchain_community.tools": None}):
        assert run_web_search("anything") == ""


def test_web_search_tool_metadata() -> None:
    name = getattr(web_search, "name", getattr(web_search, "__name__", ""))
    assert name == "web_search"
    description = getattr(web_search, "description", web_search.__doc__ or "")
    assert "DuckDuckGo" in description


class _FakeRouter:
    """Role-aware fake router for the full crew pipeline."""

    enabled = True

    def __init__(self) -> None:
        self.complete_calls: list[tuple[str, str]] = []

    def complete_json(self, role, system_prompt, user_prompt, *, max_output_tokens=None):
        if role == "planner":
            return {
                "reasoning": "Draft a product page.",
                "actions": [
                    {
                        "operation": "create_page",
                        "title": "Acme Wonder Whey",
                        "description": "Create a product page",
                        "payload": {},
                        "risk": "low",
                    }
                ],
            }
        if role == "researcher":
            return {"search_query": "Acme Wonder Whey specs price", "insight": "need specs"}
        return {}

    def complete(self, role, system_prompt, user_prompt, *, max_output_tokens=None):
        self.complete_calls.append((role, user_prompt))
        if role == "researcher":
            return "Acme Wonder Whey: 24g protein, $39.99."
        if role == "content_writer":
            return "<p>Drafted product page</p>"
        if role == "communicator":
            return "Here is what happened."
        return ""


def _run_pipeline(monkeypatch, *, web_search_enabled: bool, search_calls: list):
    monkeypatch.setattr("agents.crew.web_search_available", lambda: True)

    def _fake_search(query, max_results=6):
        search_calls.append((query, max_results))
        return "RAW: 24g protein per scoop, $39.99 MSRP."

    monkeypatch.setattr("agents.crew.run_web_search", _fake_search)

    router = _FakeRouter()
    crew = build_wordpress_crew(
        tools=None,
        safety_layer=SafetyLayer(),
        activity_log=_NullLog(),
        site_memory=_NullMemory(),
        router=router,
        agentic=True,
        web_search_enabled=web_search_enabled,
        web_search_max_results=5,
    )
    state = crew.invoke(
        {
            "user_request": "Research the latest Acme Wonder Whey specs and create a draft product page",
            "approved": False,
            "statuses": [],
            "memory_context": {},
        }
    )
    return router, state


class _NullLog:
    def append(self, *args, **kwargs) -> None:  # noqa: D401 - test stub
        pass


class _NullMemory:
    def remember_conversation(self, *args, **kwargs) -> None:
        pass

    def remember_site_event(self, *args, **kwargs) -> None:
        pass


def test_researcher_runs_web_search_and_feeds_content_writer(monkeypatch) -> None:
    search_calls: list = []
    router, state = _run_pipeline(monkeypatch, web_search_enabled=True, search_calls=search_calls)

    # The Researcher searched the web with the model's query and summarized it.
    assert search_calls == [("Acme Wonder Whey specs price", 5)]
    assert state["web_research"]["query"] == "Acme Wonder Whey specs price"
    assert state["web_research"]["summary"] == "Acme Wonder Whey: 24g protein, $39.99."

    # The Content Writer received the research summary in its prompt.
    content_prompts = [user for role, user in router.complete_calls if role == "content_writer"]
    assert content_prompts
    assert "24g protein, $39.99" in content_prompts[0]

    # The drafted content landed in the plan.
    page = next(a for a in state["plan"] if a.operation.value == "create_page")
    assert page.payload["content"] == "<p>Drafted product page</p>"


def test_web_search_disabled_skips_search(monkeypatch) -> None:
    search_calls: list = []
    _router, state = _run_pipeline(monkeypatch, web_search_enabled=False, search_calls=search_calls)
    assert search_calls == []
    assert not state.get("web_research")
