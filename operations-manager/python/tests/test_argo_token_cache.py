"""Tests for the process-wide ArgoCD token cache.

A connector is constructed per operation (16 call sites), and each construction
used to perform a full login. That login costs roughly 700ms because ArgoCD
verifies the password with bcrypt, so it dominated every request and task step
that touched ArgoCD - while the token it returns is a JWT valid for 24 hours.

These tests pin the three behaviours that make sharing the token safe:
  - a second connector reuses the token instead of logging in again,
  - concurrent callers that all find an empty cache produce ONE login,
  - a 401 only invalidates the token that actually failed (compare-and-clear),
    so it cannot throw away a fresher token someone else just stored.
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from opi.connectors import argo as argo_module
from opi.connectors.argo import ArgoConnector


@pytest.fixture(autouse=True)
def _clear_token_cache():
    """Each test starts from an empty shared cache."""
    argo_module._token_cache.clear()
    yield
    argo_module._token_cache.clear()


def _connector_without_login() -> ArgoConnector:
    """Build a connector without performing the synchronous constructor login."""
    with patch.object(ArgoConnector, "_perform_login", return_value=False):
        return ArgoConnector()


def test_constructor_reuses_cached_token() -> None:
    """The second connector must not log in: it adopts the shared token."""
    with patch.object(ArgoConnector, "_perform_login", autospec=True) as perform_login:

        def _login(self) -> bool:
            self.auth_token = "token-1"
            self._store_token("token-1")
            return True

        perform_login.side_effect = _login
        first = ArgoConnector()
        assert first.auth_token == "token-1"
        assert perform_login.call_count == 1

        second = ArgoConnector()

    assert second.auth_token == "token-1"
    assert perform_login.call_count == 1, "second connector logged in again instead of reusing the cached token"


@pytest.mark.asyncio
async def test_concurrent_authentication_performs_single_login() -> None:
    """A burst of callers on an empty cache collapses into one login.

    Without the re-check inside the refresh lock, ten callers would serialize on
    the lock and each perform its own ~700ms login.
    """
    logins = 0

    async def fake_login(self) -> bool:
        nonlocal logins
        logins += 1
        await asyncio.sleep(0.01)  # let the other waiters queue on the lock
        self.auth_token = "token-shared"
        self._store_token("token-shared")
        return True

    with patch.object(ArgoConnector, "login", fake_login):
        connectors = [_connector_without_login() for _ in range(10)]
        results = await asyncio.gather(*(c._ensure_authenticated() for c in connectors))

    assert all(results)
    assert logins == 1, f"expected a single shared login, got {logins}"
    assert all(c.auth_token == "token-shared" for c in connectors)


def test_invalidate_only_clears_the_token_that_failed() -> None:
    """Compare-and-clear: a stale 401 must not discard a newer token."""
    connector = _connector_without_login()
    connector._store_token("token-new")

    # A request that went out with the OLD token comes back 401.
    connector._invalidate_token("token-old")

    assert connector._cached_token() == "token-new", "a stale 401 discarded the freshly stored token"


def test_invalidate_clears_its_own_token() -> None:
    """The token that actually failed is removed, so the next caller re-logs in."""
    connector = _connector_without_login()
    connector._store_token("token-1")

    connector._invalidate_token("token-1")

    assert connector._cached_token() is None


@pytest.mark.asyncio
async def test_ensure_authenticated_reuses_without_lock_when_cached() -> None:
    """A warm cache needs no login at all."""
    connector = _connector_without_login()
    connector._store_token("token-warm")

    with patch.object(ArgoConnector, "login", AsyncMock(return_value=False)) as login:
        assert await connector._ensure_authenticated() is True

    login.assert_not_awaited()
    assert connector.auth_token == "token-warm"
