"""Secure WordPress REST API client."""

from __future__ import annotations

import json
import logging
import mimetypes
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from requests.auth import HTTPBasicAuth

from core.cache import TTLCache
from core.logging_config import redact

LOGGER = logging.getLogger(__name__)


class WordPressClientError(RuntimeError):
    """Raised when the WordPress API returns an error or cannot be reached."""


class WordPressClient:
    """Small wrapper around the WordPress REST API.

    Authentication uses WordPress Application Passwords, which are safer and
    easier for non-technical users than sharing account passwords.
    """

    def __init__(
        self,
        site_url: str,
        username: str,
        application_password: str,
        timeout: tuple[int, int] | int = (10, 45),
        max_retries: int = 2,
        retry_backoff_seconds: float = 0.5,
        cache_ttl_seconds: int = 60,
    ) -> None:
        parsed = urlparse(site_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise WordPressClientError(
                "Enter a full WordPress website address, such as https://example.com."
            )
        self.site_url = site_url.rstrip("/") + "/"
        self.api_root = urljoin(self.site_url, "wp-json/")
        self.wp_v2 = urljoin(self.api_root, "wp/v2/")
        self.timeout = timeout
        self.max_retries = max(0, max_retries)
        self.retry_backoff_seconds = max(0.0, retry_backoff_seconds)
        self.cache = TTLCache(cache_ttl_seconds)
        self._secret_values = [application_password]
        self.session = requests.Session()
        self.session.auth = HTTPBasicAuth(username, application_password)
        self.session.headers.update(
            {
                "Accept": "application/json",
                "User-Agent": "WordPressGenius/0.2",
            }
        )

    def validate_connection(self) -> dict[str, Any]:
        """Validate credentials and return current user/site metadata."""

        site = self.request("GET", self.api_root)
        user = self.request("GET", "users/me", params={"context": "edit"})
        # WordPress returns a `capabilities` map (capability -> bool) under the
        # edit context. Capabilities are a far more precise permission signal than
        # the coarse role name, so we surface the granted ones for the safety layer.
        raw_caps = user.get("capabilities", {})
        capabilities = (
            sorted(name for name, granted in raw_caps.items() if granted)
            if isinstance(raw_caps, dict)
            else []
        )
        return {
            "site_name": site.get("name", "WordPress site"),
            "site_url": site.get("url", self.site_url),
            "user": {
                "id": user.get("id"),
                "name": user.get("name"),
                "roles": user.get("roles", []),
                "capabilities": capabilities,
            },
        }

    def request_api(
        self,
        endpoint: str,
        *,
        method: str = "GET",
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        """Execute a request against any wp-json namespace."""

        return self.request(
            method,
            urljoin(self.api_root, endpoint.lstrip("/")),
            params=params,
            json_body=json_body,
        )

    def paginated_request_api(
        self,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
        per_page: int = 100,
        max_pages: int = 10,
    ) -> list[dict[str, Any]]:
        """Read a list endpoint across pages with a conservative page cap."""

        items: list[dict[str, Any]] = []
        base_params = dict(params or {})
        base_params.setdefault("per_page", per_page)
        for page in range(1, max_pages + 1):
            page_params = {**base_params, "page": page}
            response = self.request_api(endpoint, params=page_params)
            if not isinstance(response, list):
                raise WordPressClientError("Expected a list response from WordPress pagination.")
            items.extend(response)
            if len(response) < int(base_params["per_page"]):
                break
        return items

    def request(
        self,
        method: str,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        """Execute a WordPress REST API request."""

        url = endpoint if endpoint.startswith("http") else urljoin(self.wp_v2, endpoint)
        LOGGER.info("WordPress API request: %s %s", method.upper(), endpoint)
        response: requests.Response | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self.session.request(
                    method=method.upper(),
                    url=url,
                    params=params,
                    json=json_body,
                    data=data,
                    headers=headers,
                    timeout=self.timeout,
                )
            except requests.Timeout as exc:
                if self._can_retry(attempt):
                    self._sleep_before_retry(attempt, method, endpoint)
                    continue
                LOGGER.warning("WordPress request timed out: %s %s", method, endpoint)
                raise WordPressClientError(
                    "WordPress did not respond in time. Try again, or ask your host if the site is slow."
                ) from exc
            except requests.exceptions.SSLError as exc:
                LOGGER.warning("WordPress SSL failure: %s %s", method, endpoint)
                raise WordPressClientError(
                    "The website SSL certificate could not be verified. Check that the site opens securely with https."
                ) from exc
            except requests.ConnectionError as exc:
                if self._can_retry(attempt):
                    self._sleep_before_retry(attempt, method, endpoint)
                    continue
                LOGGER.warning("WordPress connection failure: %s %s", method, endpoint)
                raise WordPressClientError(
                    "Could not connect to the WordPress site. Check the website address and try again."
                ) from exc
            except requests.RequestException as exc:
                LOGGER.exception("WordPress request failed: %s %s", method, endpoint)
                raise WordPressClientError(
                    "Could not reach the WordPress site. Check the URL and network access."
                ) from exc

            if response.ok:
                break
            if response.status_code in {429, 500, 502, 503, 504} and self._can_retry(attempt):
                self._sleep_before_retry(attempt, method, endpoint)
                continue
            raise WordPressClientError(self._format_error(response))

        if response is None:
            raise WordPressClientError("WordPress did not return a response.")

        if response.status_code == 204 or not response.content:
            return {}

        content_type = response.headers.get("content-type", "")
        if "application/json" not in content_type:
            return {"raw": response.text}

        try:
            return response.json()
        except json.JSONDecodeError as exc:
            raise WordPressClientError("WordPress returned malformed JSON.") from exc


    def _cached(self, key: tuple, loader):
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        value = loader()
        self.cache.set(key, value)
        return value

    def clear_cache(self) -> None:
        """Clear cached WordPress read responses after writes."""

        self.cache.clear()

    def get_posts(
        self,
        *,
        search: str | None = None,
        status: str = "any",
        per_page: int = 20,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "status": status,
            "per_page": per_page,
            "context": "edit",
        }
        if search:
            params["search"] = search
        return self._cached(("posts", tuple(sorted(params.items()))), lambda: self.request("GET", "posts", params=params))

    def get_pages(
        self,
        *,
        search: str | None = None,
        status: str = "any",
        per_page: int = 20,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "status": status,
            "per_page": per_page,
            "context": "edit",
        }
        if search:
            params["search"] = search
        return self._cached(("pages", tuple(sorted(params.items()))), lambda: self.request("GET", "pages", params=params))


    def get_all_posts(
        self, *, search: str | None = None, status: str = "any", max_pages: int = 10
    ) -> list[dict[str, Any]]:
        """Return posts across paginated WordPress REST responses."""

        params: dict[str, Any] = {"status": status, "context": "edit"}
        if search:
            params["search"] = search
        return self._cached(("all_posts", tuple(sorted(params.items())), max_pages), lambda: self.paginated_request_api("wp/v2/posts", params=params, max_pages=max_pages))

    def get_all_pages(
        self, *, search: str | None = None, status: str = "any", max_pages: int = 10
    ) -> list[dict[str, Any]]:
        """Return pages across paginated WordPress REST responses."""

        params: dict[str, Any] = {"status": status, "context": "edit"}
        if search:
            params["search"] = search
        return self._cached(("all_pages", tuple(sorted(params.items())), max_pages), lambda: self.paginated_request_api("wp/v2/pages", params=params, max_pages=max_pages))

    def get_post(self, post_id: int) -> dict[str, Any]:
        return self.request("GET", f"posts/{post_id}", params={"context": "edit"})

    def get_page(self, page_id: int) -> dict[str, Any]:
        return self.request("GET", f"pages/{page_id}", params={"context": "edit"})

    def create_post(
        self,
        title: str,
        content: str,
        *,
        status: str = "draft",
        excerpt: str | None = None,
        categories: list[int] | None = None,
        tags: list[int] | None = None,
        featured_media: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "title": title,
            "content": content,
            "status": status,
        }
        if excerpt:
            payload["excerpt"] = excerpt
        if categories:
            payload["categories"] = categories
        if tags:
            payload["tags"] = tags
        if featured_media:
            payload["featured_media"] = featured_media
        self.clear_cache()
        return self.request("POST", "posts", json_body=payload)

    def update_post(self, post_id: int, **fields: Any) -> dict[str, Any]:
        self.clear_cache()
        return self.request("POST", f"posts/{post_id}", json_body=fields)

    def delete_post(self, post_id: int, *, force: bool = False) -> dict[str, Any]:
        self.clear_cache()
        return self.request("DELETE", f"posts/{post_id}", params={"force": force})

    def create_page(
        self,
        title: str,
        content: str,
        *,
        status: str = "draft",
        parent: int | None = None,
        featured_media: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "title": title,
            "content": content,
            "status": status,
        }
        if parent:
            payload["parent"] = parent
        if featured_media:
            payload["featured_media"] = featured_media
        self.clear_cache()
        return self.request("POST", "pages", json_body=payload)

    def update_page(self, page_id: int, **fields: Any) -> dict[str, Any]:
        self.clear_cache()
        return self.request("POST", f"pages/{page_id}", json_body=fields)

    def delete_page(self, page_id: int, *, force: bool = False) -> dict[str, Any]:
        self.clear_cache()
        return self.request("DELETE", f"pages/{page_id}", params={"force": force})

    def upload_media(
        self,
        file_path: Path,
        *,
        title: str | None = None,
        alt_text: str | None = None,
        caption: str | None = None,
    ) -> dict[str, Any]:
        """Upload a media file from disk."""

        if not file_path.exists():
            raise WordPressClientError(f"Media file does not exist: {file_path}")
        if not file_path.is_file():
            raise WordPressClientError(f"Media path is not a file: {file_path}")
        max_size_bytes = 25 * 1024 * 1024
        if file_path.stat().st_size > max_size_bytes:
            raise WordPressClientError("Media file is larger than the 25 MB safety limit.")
        content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        if not content_type.startswith("image/"):
            raise WordPressClientError("Only image uploads are currently supported from chat.")
        headers = {
            "Content-Disposition": f'attachment; filename="{file_path.name}"',
            "Content-Type": content_type,
        }
        uploaded = self.request(
            "POST",
            "media",
            data=file_path.read_bytes(),
            headers=headers,
        )
        updates: dict[str, Any] = {}
        if title:
            updates["title"] = title
        if alt_text:
            updates["alt_text"] = alt_text
        if caption:
            updates["caption"] = caption
        if updates:
            uploaded = self.request("POST", f"media/{uploaded['id']}", json_body=updates)
        return uploaded

    def get_settings(self) -> dict[str, Any]:
        return self._cached(("settings",), lambda: self.request("GET", "settings"))

    def update_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        self.clear_cache()
        return self.request("POST", "settings", json_body=settings)

    def list_plugins(self) -> list[dict[str, Any]]:
        return self.request("GET", "plugins")

    def update_plugin(self, plugin_slug: str, **fields: Any) -> dict[str, Any]:
        if not plugin_slug:
            raise WordPressClientError("I need the plugin slug before changing a plugin.")
        self.clear_cache()
        return self.request("POST", f"plugins/{plugin_slug}", json_body=fields)

    def list_themes(self) -> list[dict[str, Any]]:
        return self.request("GET", "themes")

    def update_theme(self, stylesheet: str, **fields: Any) -> dict[str, Any]:
        if not stylesheet:
            raise WordPressClientError("I need the theme stylesheet name before changing a theme.")
        self.clear_cache()
        return self.request("POST", f"themes/{stylesheet}", json_body=fields)


    def _can_retry(self, attempt: int) -> bool:
        return attempt < self.max_retries

    def _sleep_before_retry(self, attempt: int, method: str, endpoint: str) -> None:
        delay = self.retry_backoff_seconds * (2**attempt)
        LOGGER.info(
            "Retrying WordPress request after transient failure: %s %s in %.2fs",
            method,
            endpoint,
            delay,
        )
        if delay > 0:
            time.sleep(delay)

    def _format_error(self, response: requests.Response) -> str:
        try:
            body = response.json()
            message = body.get("message") or body.get("code") or response.text
            code = body.get("code")
        except json.JSONDecodeError:
            message = response.text
            code = None
        clean = redact(_clean_message(str(message)), self._secret_values)
        if response.status_code in {401, 403}:
            return (
                "WordPress rejected the credentials or permissions. Confirm the username, "
                "Application Password, and that the user has permission for this action."
            )
        if response.status_code == 404:
            return "WordPress could not find that resource. Check the post/page ID or site URL."
        if response.status_code == 429:
            return "WordPress is rate limiting requests. Wait a moment and try again."
        if response.status_code >= 500:
            return (
                "WordPress returned a server error. No further changes were attempted. "
                "Try again later or contact your hosting provider."
            )
        detail = f" ({code})" if code else ""
        return f"WordPress API error {response.status_code}{detail}: {clean}"


def _clean_message(message: str) -> str:
    no_html = re.sub(r"<[^>]+>", " ", message)
    return re.sub(r"\s+", " ", no_html).strip()
