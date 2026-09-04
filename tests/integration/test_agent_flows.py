from __future__ import annotations

import json

import responses

from core.orchestrator import WordPressGeniusOrchestrator
from core.models import WordPressCredentials
from tests.helpers import post_payload


def _credentials() -> WordPressCredentials:
    return WordPressCredentials(
        site_url="https://example.test",
        username="admin",
        application_password="app password",
    )


def _mock_research_endpoints(wp_base_url: str, roles: list[str] | None = None) -> None:
    roles = roles or ["administrator"]
    responses.add(
        responses.GET,
        f"{wp_base_url}/wp-json/",
        json={"name": "Test Site", "url": wp_base_url},
        status=200,
    )
    responses.add(
        responses.GET,
        f"{wp_base_url}/wp-json/wp/v2/users/me",
        json={"id": 1, "name": "Admin", "roles": roles},
        status=200,
    )
    responses.add(
        responses.GET,
        f"{wp_base_url}/wp-json/wp/v2/posts",
        json=[],
        status=200,
    )
    responses.add(
        responses.GET,
        f"{wp_base_url}/wp-json/wp/v2/pages",
        json=[],
        status=200,
    )


@responses.activate
def test_draft_post_flow_creates_draft_without_confirmation(
    test_config, wp_base_url: str
) -> None:
    _mock_research_endpoints(wp_base_url)
    responses.add(
        responses.POST,
        f"{wp_base_url}/wp-json/wp/v2/posts",
        json=post_payload(321, status="draft"),
        status=201,
    )
    orchestrator = WordPressGeniusOrchestrator(test_config)

    result = orchestrator.run(
        "Create a blog post about summer specials.",
        credentials=_credentials(),
        approved=False,
    )

    assert result.requires_confirmation is False
    assert "Done" in result.final_response
    post_calls = [
        call
        for call in responses.calls
        if call.request.method == "POST"
        and call.request.url == f"{wp_base_url}/wp-json/wp/v2/posts"
    ]
    assert len(post_calls) == 1
    assert json.loads(post_calls[0].request.body)["status"] == "draft"


@responses.activate
def test_update_flow_without_approval_never_calls_update_endpoint(
    test_config, wp_base_url: str
) -> None:
    _mock_research_endpoints(wp_base_url)
    orchestrator = WordPressGeniusOrchestrator(test_config)

    result = orchestrator.run(
        "Update post 123 with our new promotion.",
        credentials=_credentials(),
        approved=False,
    )

    assert result.requires_confirmation is True
    assert "need your approval" in result.final_response
    assert all(
        not (
            call.request.method == "POST"
            and call.request.url == f"{wp_base_url}/wp-json/wp/v2/posts/123"
        )
        for call in responses.calls
    )


@responses.activate
def test_approved_update_flow_creates_backup_and_updates_post(
    test_config, wp_base_url: str
) -> None:
    _mock_research_endpoints(wp_base_url)
    responses.add(
        responses.GET,
        f"{wp_base_url}/wp-json/wp/v2/posts/123",
        json=post_payload(123, status="publish"),
        status=200,
    )
    responses.add(
        responses.POST,
        f"{wp_base_url}/wp-json/wp/v2/posts/123",
        json=post_payload(123, status="publish"),
        status=200,
    )
    orchestrator = WordPressGeniusOrchestrator(test_config)

    result = orchestrator.run(
        "Update post 123 with our new promotion.",
        credentials=_credentials(),
        approved=True,
    )

    assert result.requires_confirmation is True
    assert "Backup created" in result.final_response
    assert (test_config.backup_dir / "latest_backup.txt").exists()


@responses.activate
def test_undo_flow_restores_latest_backup(test_config, wp_base_url: str) -> None:
    backup_dir = test_config.backup_dir
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / "wordpress_backup_test.json"
    backup_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "created_at": "2026-05-28T15:00:00+00:00",
                "resources": [
                    {
                        "operation": "update_post",
                        "action_title": "Update post",
                        "payload": {"id": 123},
                        "resource": post_payload(123, status="publish"),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (backup_dir / "latest_backup.txt").write_text(str(backup_path), encoding="utf-8")
    _mock_research_endpoints(wp_base_url)
    responses.add(
        responses.POST,
        f"{wp_base_url}/wp-json/wp/v2/posts/123",
        json=post_payload(123, status="publish"),
        status=200,
    )
    orchestrator = WordPressGeniusOrchestrator(test_config)

    result = orchestrator.run(
        "Undo the last supported WordPress change.",
        credentials=_credentials(),
        approved=True,
    )

    assert "restored the latest supported backup" in result.final_response


@responses.activate
def test_approved_seo_flow_updates_post_metadata(test_config, wp_base_url: str) -> None:
    _mock_research_endpoints(wp_base_url)
    responses.add(
        responses.GET,
        f"{wp_base_url}/wp-json/wp/v2/posts/123",
        json=post_payload(123, status="draft"),
        status=200,
    )
    responses.add(
        responses.POST,
        f"{wp_base_url}/wp-json/wp/v2/posts/123",
        json=post_payload(123, status="draft"),
        status=200,
    )
    orchestrator = WordPressGeniusOrchestrator(test_config)

    result = orchestrator.run(
        "Optimize SEO for post 123 about emergency plumbing.",
        credentials=_credentials(),
        approved=True,
    )

    assert "Optimize SEO metadata" in result.final_response
    seo_calls = [
        call
        for call in responses.calls
        if call.request.method == "POST"
        and call.request.url == f"{wp_base_url}/wp-json/wp/v2/posts/123"
    ]
    assert seo_calls
    body = json.loads(seo_calls[-1].request.body)
    assert "slug" in body
    assert "excerpt" in body


@responses.activate
def test_role_block_prevents_admin_only_plugin_change(test_config, wp_base_url: str) -> None:
    _mock_research_endpoints(wp_base_url, roles=["editor"])
    orchestrator = WordPressGeniusOrchestrator(test_config)

    result = orchestrator.run(
        "Deactivate plugin 'hello-dolly'.",
        credentials=_credentials(),
        approved=True,
    )

    assert "requires one of" in result.final_response
    assert all(
        not (call.request.method == "POST" and "/wp/v2/plugins/" in call.request.url)
        for call in responses.calls
    )
