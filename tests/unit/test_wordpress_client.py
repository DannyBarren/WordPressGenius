from __future__ import annotations

import pytest
import responses

from tools.wordpress_client import WordPressClient, WordPressClientError


@responses.activate
def test_validate_connection_reads_site_and_current_user(wp_base_url: str) -> None:
    responses.add(
        responses.GET,
        f"{wp_base_url}/wp-json/",
        json={"name": "Test Site", "url": wp_base_url},
        status=200,
    )
    responses.add(
        responses.GET,
        f"{wp_base_url}/wp-json/wp/v2/users/me",
        json={"id": 1, "name": "Admin", "roles": ["administrator"]},
        status=200,
    )
    client = WordPressClient(wp_base_url, "admin", "app password", max_retries=0)

    summary = client.validate_connection()

    assert summary["site_name"] == "Test Site"
    assert summary["user"]["roles"] == ["administrator"]


@responses.activate
def test_auth_error_returns_user_friendly_message(wp_base_url: str) -> None:
    responses.add(
        responses.GET,
        f"{wp_base_url}/wp-json/wp/v2/posts",
        json={"code": "rest_forbidden", "message": "Sorry, not allowed."},
        status=403,
    )
    client = WordPressClient(wp_base_url, "admin", "app password", max_retries=0)

    with pytest.raises(WordPressClientError, match="credentials or permissions"):
        client.get_posts()


@responses.activate
def test_server_error_does_not_expose_raw_html(wp_base_url: str) -> None:
    responses.add(
        responses.GET,
        f"{wp_base_url}/wp-json/wp/v2/posts",
        body="<html><body>Fatal secret stack</body></html>",
        status=500,
    )
    client = WordPressClient(wp_base_url, "admin", "app password", max_retries=0)

    with pytest.raises(WordPressClientError) as exc_info:
        client.get_posts()

    assert "server error" in str(exc_info.value)
    assert "Fatal secret stack" not in str(exc_info.value)


def test_invalid_site_url_is_rejected() -> None:
    with pytest.raises(WordPressClientError, match="full WordPress website address"):
        WordPressClient("not-a-url", "admin", "app password", max_retries=0)
