"""Lightweight SEO helpers used by the content agent."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class SeoSuggestion:
    title: str
    meta_description: str
    focus_keywords: list[str]
    slug: str


STOP_WORDS = {
    "a",
    "about",
    "and",
    "for",
    "in",
    "my",
    "of",
    "on",
    "the",
    "to",
    "with",
    "wordpress",
    "website",
}


def suggest_seo(title: str, content_hint: str) -> SeoSuggestion:
    """Generate pragmatic SEO metadata without requiring an LLM."""

    words = [
        word.lower()
        for word in re.findall(r"[A-Za-z][A-Za-z0-9-]+", f"{title} {content_hint}")
        if word.lower() not in STOP_WORDS
    ]
    unique_keywords = list(dict.fromkeys(words))[:6]
    slug = "-".join(unique_keywords[:5]) or "wordpressgenius-update"
    clean_title = title.strip()[:58]
    if not clean_title:
        clean_title = "New WordPress Update"
    meta = f"Learn about {', '.join(unique_keywords[:3]) or clean_title.lower()} from our team."
    return SeoSuggestion(
        title=clean_title,
        meta_description=meta[:155],
        focus_keywords=unique_keywords,
        slug=slug,
    )
