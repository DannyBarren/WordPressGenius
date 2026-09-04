from __future__ import annotations

import pytest

from core.models import WordPressCredentials
from core.security import AuthenticatedUser, generate_fernet_key
from core.sites_store import SiteVault


@pytest.fixture
def user() -> AuthenticatedUser:
    return AuthenticatedUser(username="alice", role="admin")


def _creds(url: str = "https://staging.example.com") -> WordPressCredentials:
    return WordPressCredentials(
        site_url=url,
        username="admin",
        application_password="abcd EFGH ijkl MNOP",
    )


def test_add_and_list_sites_persists_encrypted(tmp_path, user) -> None:
    path = tmp_path / "sites.enc"
    key = generate_fernet_key()
    vault = SiteVault(path, key)

    site = vault.add_site(user, label="Staging", credentials=_creds())

    reopened = SiteVault(path, key)
    sites = reopened.list_sites(user)
    assert len(sites) == 1
    assert sites[0].label == "Staging"
    assert reopened.get_active_id(user) == site.id
    # secret must not be stored in plaintext
    assert b"abcd EFGH" not in path.read_bytes()


def test_sites_are_isolated_per_user(tmp_path) -> None:
    vault = SiteVault(tmp_path / "sites.enc", generate_fernet_key())
    alice = AuthenticatedUser(username="alice", role="admin")
    bob = AuthenticatedUser(username="bob", role="editor")

    vault.add_site(alice, label="Alice Site", credentials=_creds("https://alice.example.com"))

    assert vault.list_sites(alice)
    assert vault.list_sites(bob) == []


def test_set_active_and_delete_reassigns_active(tmp_path, user) -> None:
    vault = SiteVault(tmp_path / "sites.enc", generate_fernet_key())
    first = vault.add_site(user, label="First", credentials=_creds("https://first.example.com"))
    second = vault.add_site(user, label="Second", credentials=_creds("https://second.example.com"))

    vault.set_active(user, first.id)
    assert vault.get_active_id(user) == first.id

    vault.delete_site(user, first.id)
    # active falls back to a remaining site
    assert vault.get_active_id(user) == second.id

    vault.delete_site(user, second.id)
    assert vault.get_active_id(user) is None
    assert vault.list_sites(user) == []


def test_get_active_returns_credentials(tmp_path, user) -> None:
    vault = SiteVault(tmp_path / "sites.enc", generate_fernet_key())
    vault.add_site(user, label="Staging", credentials=_creds())

    active = vault.get_active(user)
    assert active is not None
    creds = active.credentials()
    assert creds.username == "admin"
    assert str(creds.site_url).startswith("https://staging.example.com")
