from __future__ import annotations


def post_payload(post_id: int = 123, *, status: str = "draft") -> dict:
    return {
        "id": post_id,
        "date": "2026-05-28T15:00:00",
        "slug": "summer-specials",
        "status": status,
        "link": f"https://example.test/?p={post_id}",
        "title": {"rendered": "Summer Specials", "raw": "Summer Specials"},
        "content": {"rendered": "<p>Old content</p>", "raw": "<p>Old content</p>"},
        "excerpt": {"rendered": "Old excerpt", "raw": "Old excerpt"},
        "featured_media": 0,
    }


def page_payload(page_id: int = 42, *, status: str = "publish") -> dict:
    payload = post_payload(page_id, status=status)
    payload["link"] = f"https://example.test/page-{page_id}/"
    payload["slug"] = f"page-{page_id}"
    payload["title"] = {"rendered": "Service Page", "raw": "Service Page"}
    return payload
