"""DuckDuckGo web search tool for the Researcher agent.

This tool is intentionally conservative and read-only. It returns DuckDuckGo's
public result snippets so the Researcher can fetch real-time, real-world facts
that are not in site memory or the WordPress site itself — product details,
specifications, current pricing, customer reviews, comparisons, and recent
events. It performs no authentication, no scraping of restricted/paywalled
sites, runs no code, and makes no changes. Results must be summarized cleanly
and fact-checked before being used in content creation.

The optional packages (``duckduckgo-search`` and ``langchain-community``) are
imported lazily so the app still runs and degrades gracefully when they are not
installed.
"""

from __future__ import annotations

import logging

LOGGER = logging.getLogger(__name__)

DEFAULT_MAX_RESULTS = 6
_MAX_RESULTS_CAP = 10
_MAX_QUERY_LEN = 300

# Schemes/markers that should never appear in a search query. Web search is for
# plain natural-language research only — not for fetching local files or code.
_FORBIDDEN_FRAGMENTS = ("file://", "javascript:", "data:", "<script", "\x00")


try:  # langchain_core ships with langgraph, but stay defensive.
    from langchain_core.tools import tool
except Exception:  # noqa: BLE001 - provide a no-op decorator fallback.

    def tool(func=None, **_kwargs):  # type: ignore[no-redef]
        def _wrap(inner):
            return inner

        return _wrap(func) if callable(func) else _wrap


def web_search_available() -> bool:
    """Return True when the optional web-search dependencies are importable."""

    try:
        import duckduckgo_search  # noqa: F401
        import langchain_community  # noqa: F401

        return True
    except Exception:  # noqa: BLE001 - availability check must never raise.
        return False


def _sanitize_query(query: str) -> str:
    cleaned = (query or "").strip()
    lowered = cleaned.lower()
    if any(fragment in lowered for fragment in _FORBIDDEN_FRAGMENTS):
        LOGGER.warning("Rejected unsafe web search query.")
        return ""
    return cleaned[:_MAX_QUERY_LEN]


def _clamp_results(max_results: int) -> int:
    try:
        value = int(max_results)
    except (TypeError, ValueError):
        return DEFAULT_MAX_RESULTS
    return max(1, min(value, _MAX_RESULTS_CAP))


def run_web_search(query: str, max_results: int = DEFAULT_MAX_RESULTS) -> str:
    """Run a DuckDuckGo search and return concatenated text snippets.

    Returns an empty string when the query is empty/unsafe, when the optional
    dependencies are missing, or when the search backend errors out. This keeps
    the agent flow resilient and offline-safe.
    """

    clean = _sanitize_query(query)
    if not clean:
        return ""

    try:
        from langchain_community.tools import DuckDuckGoSearchRun
        from langchain_community.utilities import DuckDuckGoSearchAPIWrapper
    except Exception as exc:  # noqa: BLE001 - optional dependency missing.
        LOGGER.warning("Web search unavailable (install duckduckgo-search): %s", exc)
        return ""

    count = _clamp_results(max_results)
    try:
        wrapper = DuckDuckGoSearchAPIWrapper(max_results=count)
        search = DuckDuckGoSearchRun(api_wrapper=wrapper)
        return (search.run(clean) or "").strip()
    except Exception as exc:  # noqa: BLE001 - never let search break the workflow.
        LOGGER.warning("Web search failed for %r: %s", clean, exc)
        return ""


@tool
def web_search(query: str, max_results: int = DEFAULT_MAX_RESULTS) -> str:
    """Search the public web via DuckDuckGo for real-time information.

    Use this for facts that are NOT in site memory or the WordPress site, such as:
    product details and specifications, current pricing, customer reviews and
    ratings, brand/product comparisons, ingredient or nutrition information,
    images to reference, and recent news or events.

    The tool is read-only and safe: it performs no logins, does not scrape
    restricted or paywalled sites, runs no code, and changes nothing. It is meant
    for the Researcher agent only — never for executing WordPress changes. Always
    summarize and fact-check the snippets before using them in content.

    Args:
        query: Natural-language search terms, e.g. "Optimum Nutrition Gold
            Standard Whey specs and price 2026".
        max_results: How many results to consider (1-10, default 6).

    Returns:
        Plain-text snippets from the top results, or an empty string if search is
        unavailable or returns nothing.
    """

    return run_web_search(query, max_results=max_results)
