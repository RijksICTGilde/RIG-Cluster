"""Tests for the connect-failure retry in the ArgoCD connector.

argocd-server restarts its API server in-process roughly every 40 minutes, and about
1 in 35 of those restarts runs into its 20 second graceful-shutdown deadline. During
that window the service has no endpoint and every call fails with a
ClientConnectorError within milliseconds. The wait loop in argo_manager cannot tell
that apart from a terminal state (get_application_status turns it into a RuntimeError,
which the loop re-raises on purpose), so a deployment task would die on it.

These tests pin what makes riding that out safe:
  - a connect failure followed by a success yields the success,
  - the wait loop this retry exists for rides the window out instead of dying on it,
  - an exhausted budget raises the original ClientConnectorError, which
    get_application_status still turns into a RuntimeError, while sync_application
    still reports False,
  - a budget of 0 performs exactly one attempt and never sleeps,
  - a POST is retried as well, because the request never reached the server,
  - an error that is not a connect failure is not retried,
  - the last sleep is capped at the budget instead of overshooting it,
  - the 401 re-login the retry now sits in front of still runs,
  - a fail-fast caller does not queue behind a login in flight elsewhere, while a free
    lock is still taken straight away on budget 0, so a request whose 401 re-login meets
    that lock degrades instead of hanging,
  - a caller that did ask to ride out a restart does wait for that login, even when it
    outlasts its own budget, because giving up there kills the deployment task,
  - the login branches that moved out of the old try still hold: an answer without a
    usable token reports False and caches nothing, and a redirect to HTTPS switches the
    connector over.
"""

import asyncio
import json
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from aiohttp.client_reqrep import ConnectionKey
from fastapi.responses import HTMLResponse
from opi.api.v2.router import _connect_status_backend
from opi.connectors import argo as argo_module
from opi.connectors.argo import ArgoConnector, create_argo_connector
from opi.manager.argo_manager import ArgoManager
from opi.web.router import argocd_status_fragment, dashboard

_APPLICATIONS_URL = "http://argocd-server:80/api/v1/applications"
_APPLICATION_URL = f"{_APPLICATIONS_URL}/my-app"
_SESSION_URL = "http://argocd-server:80/api/v1/session"


@pytest.fixture(autouse=True)
def _clear_token_cache():
    """The token cache is process-wide, so each test starts from an empty one."""
    argo_module._token_cache.clear()
    yield
    argo_module._token_cache.clear()


@pytest.fixture(autouse=True)
def _fresh_refresh_lock():
    """The refresh lock is process-wide too, and each test gets its own event loop.

    An asyncio.Lock binds to the loop that first WAITS on it, so a lock that was
    contended once would raise "bound to a different event loop" in the next test that
    contends. Production has a single loop and does not have that problem.
    """
    original = argo_module._token_refresh_lock
    argo_module._token_refresh_lock = asyncio.Lock()
    yield
    argo_module._token_refresh_lock = original


def _connect_error() -> aiohttp.ClientConnectorError:
    """The exact failure production saw: the TCP connection was never established."""
    key = ConnectionKey(
        host="argocd-server",
        port=80,
        is_ssl=False,
        ssl=False,
        proxy=None,
        proxy_auth=None,
        proxy_headers_hash=None,
    )
    return aiohttp.ClientConnectorError(key, OSError(111, "Connect call failed"))


class _FakeResponse:
    def __init__(self, status: int, text: str) -> None:
        self.status = status
        self._text = text
        self.url = "http://argocd-server:80/"

    async def text(self) -> str:
        return self._text

    async def json(self) -> Any:
        return json.loads(self._text)


class _FakeRequest:
    """One queued outcome: either the response, or the error raised on connect."""

    def __init__(self, outcome: _FakeResponse | Exception) -> None:
        self._outcome = outcome

    async def __aenter__(self) -> _FakeResponse:
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome

    async def __aexit__(self, *args: Any) -> bool:
        return False


class _FakeSession:
    """Stands in for aiohttp.ClientSession and hands out one outcome per request.

    A real session is unusable once it has left its async with, so a retry that spans
    the session has to build a new one per attempt. Raising here the way aiohttp does
    keeps that honest: reusing the closed session shows up instead of silently working.
    """

    def __init__(self, network: _FakeNetwork) -> None:
        self._network = network
        self.closed = False

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *args: Any) -> bool:
        self.closed = True
        return False

    def request(self, method: str, url: str, **kwargs: Any) -> _FakeRequest:
        if self.closed:
            raise RuntimeError("Session is closed")
        return _FakeRequest(self._network.next_outcome(method, url))

    def post(self, url: str, **kwargs: Any) -> _FakeRequest:
        return self.request("POST", url, **kwargs)


class _FakeNetwork:
    """The queued outcomes, shared by every session the code under test opens."""

    def __init__(self, outcomes: list[_FakeResponse | Exception]) -> None:
        self._outcomes = outcomes
        self.calls: list[tuple[str, str]] = []
        self.sessions: list[_FakeSession] = []

    def new_session(self, *args: Any, **kwargs: Any) -> _FakeSession:
        session = _FakeSession(self)
        self.sessions.append(session)
        return session

    def next_outcome(self, method: str, url: str) -> _FakeResponse | Exception:
        self.calls.append((method, url))
        assert self._outcomes, f"unexpected extra request: {method} {url}"
        return self._outcomes.pop(0)


def _authenticated_connector(connect_retry_seconds: float = 30.0) -> ArgoConnector:
    """A connector with a token in hand, so no login happens in these tests."""
    with patch.object(ArgoConnector, "_perform_login", return_value=False):
        connector = ArgoConnector(connect_retry_seconds=connect_retry_seconds)
    connector.auth_token = "token-1"
    return connector


def _unauthenticated_connector(connect_retry_seconds: float) -> ArgoConnector:
    """A connector without a token, so _ensure_authenticated has to go for the lock."""
    with patch.object(ArgoConnector, "_perform_login", return_value=False):
        return ArgoConnector(connect_retry_seconds=connect_retry_seconds)


class _Harness:
    def __init__(self, network: _FakeNetwork, sleeps: list[float]) -> None:
        self.calls = network.calls
        self.sessions = network.sessions
        self.sleeps = sleeps


def _harness(outcomes: list[_FakeResponse | Exception]):
    """Patch out the network and the clock, and record both."""
    network = _FakeNetwork(outcomes)
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    return (
        patch("opi.connectors.argo.aiohttp.ClientSession", MagicMock(side_effect=network.new_session)),
        patch("opi.connectors.argo.aiohttp.TCPConnector", MagicMock()),
        patch("opi.connectors.argo.asyncio.sleep", AsyncMock(side_effect=fake_sleep)),
        _Harness(network, sleeps),
    )


@pytest.mark.asyncio
async def test_connect_failure_followed_by_success_returns_the_success() -> None:
    """The 20 second window is ridden out instead of failing the caller."""
    session_patch, connector_patch, sleep_patch, h = _harness([_connect_error(), _FakeResponse(200, '{"ok": true}')])
    connector = _authenticated_connector()

    with session_patch, connector_patch, sleep_patch:
        status, text = await connector._make_authenticated_request("GET", _APPLICATIONS_URL)

    assert (status, text) == (200, '{"ok": true}')
    assert len(h.calls) == 2, "the request was not retried"
    assert h.sleeps == [1.0]


@pytest.mark.asyncio
async def test_exhausted_budget_raises_the_original_connect_error() -> None:
    """Six attempts, backoff 1-2-4-8-15, then the untouched ClientConnectorError."""
    last = _connect_error()
    errors: list[_FakeResponse | Exception] = [_connect_error() for _ in range(5)]
    errors.append(last)
    session_patch, connector_patch, sleep_patch, h = _harness(errors)
    connector = _authenticated_connector()

    with session_patch, connector_patch, sleep_patch, pytest.raises(aiohttp.ClientConnectorError) as excinfo:
        await connector._make_authenticated_request("GET", _APPLICATIONS_URL)

    assert excinfo.value is last, "the error the caller sees is not the last connect failure"
    assert len(h.calls) == 6
    assert h.sleeps == [1.0, 2.0, 4.0, 8.0, 15.0]
    assert sum(h.sleeps) == 30.0, "the budget is the upper bound, and argocd_overview quotes that number"


@pytest.mark.parametrize(
    ("budget", "expected_sleeps"),
    [
        pytest.param(5.0, [1.0, 2.0, 2.0], id="doubling-overshoots-the-remainder"),
        pytest.param(0.5, [0.5], id="budget-is-smaller-than-the-first-delay"),
    ],
)
@pytest.mark.asyncio
async def test_the_last_sleep_is_capped_at_what_is_left_of_the_budget(
    budget: float, expected_sleeps: list[float]
) -> None:
    """The budget is the upper bound, not the moment the doubling happens to pass it.

    Measured on budgets the doubling does not land on. At 5 the 1 and the 2 fit and the
    4 that follows does not, so it becomes the 2 that is left. At 0.5 even the first
    delay does not fit, so the cap has to hold on the very first sleep too.
    """
    attempts = len(expected_sleeps) + 1
    session_patch, connector_patch, sleep_patch, h = _harness([_connect_error() for _ in range(attempts)])
    connector = _authenticated_connector(connect_retry_seconds=budget)

    with session_patch, connector_patch, sleep_patch, pytest.raises(aiohttp.ClientConnectorError):
        await connector._make_authenticated_request("GET", _APPLICATIONS_URL)

    assert h.sleeps == expected_sleeps
    assert sum(h.sleeps) == budget
    assert len(h.calls) == attempts


@pytest.mark.asyncio
async def test_get_application_status_still_raises_runtime_error_when_argocd_is_really_gone() -> None:
    """Nothing changes at the outside when the retries do not help."""
    session_patch, connector_patch, sleep_patch, _ = _harness([_connect_error() for _ in range(6)])
    connector = _authenticated_connector()

    with session_patch, connector_patch, sleep_patch, pytest.raises(RuntimeError) as excinfo:
        await connector.get_application_status("some-app")

    assert "Cannot connect to host argocd-server" in str(excinfo.value)


@pytest.mark.asyncio
async def test_the_wait_loop_rides_out_a_connect_failure_instead_of_failing_the_task() -> None:
    """The reason this retry exists, measured where the damage would be done.

    get_application_status turns any failure into a RuntimeError, and the wait loop in
    argo_manager re-raises RuntimeError on purpose: a terminal state (degraded, sync
    failed) is not distinguishable from it. So without a retry underneath, a 20 second
    restart window ends the deployment task on the spot, and the loop's own poll never
    gets a second chance.
    """
    synced = json.dumps({"status": {"sync": {"status": "Synced"}, "health": {"status": "Healthy"}}})
    session_patch, connector_patch, sleep_patch, h = _harness(
        [_FakeResponse(200, '{"token": "token-1"}'), _connect_error(), _FakeResponse(200, synced)]
    )
    with patch.object(ArgoConnector, "_perform_login", return_value=False):
        connector = ArgoConnector()

    with (
        session_patch,
        connector_patch,
        sleep_patch,
        patch("opi.connectors.argo.create_argo_connector", MagicMock(return_value=connector)),
    ):
        result = await ArgoManager(MagicMock()).wait_for_application_synced("my-app", timeout=10, poll_interval=1)

    assert result is True, "the connect failure reached the wait loop and killed the task"
    assert h.calls == [("POST", _SESSION_URL), ("GET", _APPLICATION_URL), ("GET", _APPLICATION_URL)]


@pytest.mark.asyncio
async def test_zero_budget_performs_one_attempt_and_never_sleeps() -> None:
    """What an interactive status poll uses: fail now, the next poll shows the answer."""
    session_patch, connector_patch, sleep_patch, h = _harness([_connect_error()])
    connector = _authenticated_connector(connect_retry_seconds=0)

    with session_patch, connector_patch, sleep_patch, pytest.raises(aiohttp.ClientConnectorError):
        await connector._make_authenticated_request("GET", _APPLICATIONS_URL)

    assert len(h.calls) == 1
    assert h.sleeps == []


@pytest.mark.asyncio
async def test_a_post_is_retried_as_well() -> None:
    """Safe because the connection was never established, so the sync never arrived."""
    session_patch, connector_patch, sleep_patch, h = _harness([_connect_error(), _FakeResponse(200, "{}")])
    connector = _authenticated_connector()

    with session_patch, connector_patch, sleep_patch:
        assert await connector.sync_application("some-app") is True

    assert [method for method, _ in h.calls] == ["POST", "POST"]
    assert h.sleeps == [1.0]


@pytest.mark.asyncio
async def test_a_sync_that_never_connects_reports_false_instead_of_raising() -> None:
    """Unchanged at the outside on the POST path too, once the budget is gone.

    sync_application swallows the failure and reports False, which is what its callers
    branch on. The raise out of the retry must therefore not become the answer they see.
    """
    session_patch, connector_patch, sleep_patch, h = _harness([_connect_error() for _ in range(6)])
    connector = _authenticated_connector()

    with session_patch, connector_patch, sleep_patch:
        assert await connector.sync_application("some-app") is False

    assert len(h.calls) == 6, "the sync was not retried for the full budget"
    assert h.sleeps == [1.0, 2.0, 4.0, 8.0, 15.0]


@pytest.mark.asyncio
async def test_an_error_that_is_not_a_connect_failure_is_not_retried() -> None:
    """A disconnect mid-request may already have been processed, so it stands."""
    session_patch, connector_patch, sleep_patch, h = _harness([aiohttp.ServerDisconnectedError()])
    connector = _authenticated_connector()

    with session_patch, connector_patch, sleep_patch, pytest.raises(aiohttp.ServerDisconnectedError):
        await connector._make_authenticated_request("GET", _APPLICATIONS_URL)

    assert len(h.calls) == 1
    assert h.sleeps == []


@pytest.mark.asyncio
async def test_a_401_still_re_authenticates_and_repeats_the_request() -> None:
    """The retry reads the response before the 401 handling, which still has to run."""
    session_patch, connector_patch, sleep_patch, h = _harness(
        [
            _FakeResponse(401, "token expired"),
            _FakeResponse(200, '{"token": "token-fresh"}'),
            _FakeResponse(200, '{"ok": true}'),
        ]
    )
    connector = _authenticated_connector()

    with session_patch, connector_patch, sleep_patch:
        status, text = await connector._make_authenticated_request("GET", _APPLICATIONS_URL)

    assert (status, text) == (200, '{"ok": true}')
    assert h.calls == [("GET", _APPLICATIONS_URL), ("POST", _SESSION_URL), ("GET", _APPLICATIONS_URL)]
    assert connector.auth_token == "token-fresh"
    assert h.sleeps == []


@pytest.mark.asyncio
async def test_a_401_after_re_authentication_is_reported_instead_of_repeated_again() -> None:
    """One re-login per request, so a rejected fresh token does not loop."""
    session_patch, connector_patch, sleep_patch, h = _harness(
        [
            _FakeResponse(401, "token expired"),
            _FakeResponse(200, '{"token": "token-fresh"}'),
            _FakeResponse(401, "still no"),
        ]
    )
    connector = _authenticated_connector()

    with session_patch, connector_patch, sleep_patch:
        status, text = await connector._make_authenticated_request("GET", _APPLICATIONS_URL)

    assert (status, text) == (401, "Authentication failed after retry")
    assert h.calls == [("GET", _APPLICATIONS_URL), ("POST", _SESSION_URL), ("GET", _APPLICATIONS_URL)]


@pytest.mark.asyncio
async def test_the_status_backend_builds_a_fail_fast_connector() -> None:
    """The budget has to arrive at the connector the UI actually polls with.

    Measured on the attribute rather than on the call, so it also covers the factory
    passing the argument through.
    """

    def _login(self: ArgoConnector) -> bool:
        self.auth_token = "token-1"
        return True

    with (
        patch.object(ArgoConnector, "_perform_login", _login),
        patch("opi.api.v2.router.create_kubectl_connector", MagicMock()),
    ):
        argo, _ = await _connect_status_backend()

    assert argo.connect_retry_seconds == 0, "an interactive status poll would wait out an ArgoCD restart"


def test_a_connector_defaults_to_the_thirty_second_budget() -> None:
    """Everything that is not an interactive poll rides out the restart window."""
    with patch.object(ArgoConnector, "_perform_login", return_value=False):
        assert create_argo_connector().connect_retry_seconds == 30.0


@pytest.mark.asyncio
async def test_a_connect_failure_during_login_is_retried() -> None:
    """The login post never reached the server either, so it is safe to repeat."""
    session_patch, connector_patch, sleep_patch, h = _harness(
        [_connect_error(), _FakeResponse(200, '{"token": "token-fresh"}')]
    )
    with patch.object(ArgoConnector, "_perform_login", return_value=False):
        connector = ArgoConnector()

    with session_patch, connector_patch, sleep_patch:
        assert await connector.login() is True

    assert connector.auth_token == "token-fresh"
    assert len(h.calls) == 2, "the login was not retried"
    assert len(h.sessions) == 2, "the second attempt reused the session the first one closed"
    assert h.sleeps == [1.0]


@pytest.mark.asyncio
async def test_login_degrades_to_false_when_the_budget_runs_out() -> None:
    """Unchanged at the outside: the caller sees no token, not an exception."""
    session_patch, connector_patch, sleep_patch, h = _harness([_connect_error() for _ in range(6)])
    with patch.object(ArgoConnector, "_perform_login", return_value=False):
        connector = ArgoConnector()

    with session_patch, connector_patch, sleep_patch:
        assert await connector.login() is False

    assert len(h.calls) == 6
    assert h.sleeps == [1.0, 2.0, 4.0, 8.0, 15.0]


@pytest.mark.asyncio
async def test_a_fail_fast_connector_does_not_sleep_on_the_login_path_either() -> None:
    """The budget of the connector governs its login too, not the module default.

    An interactive caller reaches that path: a 401 invalidates the token the request used,
    and the re-authentication behind it goes through login(). More failures are queued than
    one, so a login that spends the default budget shows up as extra attempts.
    """
    session_patch, connector_patch, sleep_patch, h = _harness([_connect_error() for _ in range(6)])
    with patch.object(ArgoConnector, "_perform_login", return_value=False):
        connector = ArgoConnector(connect_retry_seconds=0)

    with session_patch, connector_patch, sleep_patch:
        assert await connector.login() is False

    assert len(h.calls) == 1, "a page render that asked to fail fast would sleep in the login"
    assert h.sleeps == []


@pytest.mark.asyncio
async def test_a_login_failure_that_is_not_a_connect_error_degrades_without_retrying() -> None:
    """Unchanged for every other failure too: login reports False, it never raises."""
    session_patch, connector_patch, sleep_patch, h = _harness([aiohttp.ServerDisconnectedError()])
    with patch.object(ArgoConnector, "_perform_login", return_value=False):
        connector = ArgoConnector()

    with session_patch, connector_patch, sleep_patch:
        assert await connector.login() is False

    assert len(h.calls) == 1
    assert h.sleeps == []


@pytest.mark.parametrize(
    ("status", "body"),
    [
        pytest.param(200, '{"message": "session created"}', id="200-without-a-token"),
        pytest.param(401, "invalid username or password", id="401-refused"),
        pytest.param(503, "upstream connect error", id="503-unavailable"),
    ],
)
@pytest.mark.asyncio
async def test_a_login_answer_that_carries_no_usable_token_reports_false(status: int, body: str) -> None:
    """Reaching the server is not the same as being logged in.

    Splitting login() into a retrying wrapper and _login_once moved these two branches
    out of the try that used to hold them, so they are pinned here: a caller sees False
    and nothing lands in the process-wide cache for the next caller to adopt.
    """
    session_patch, connector_patch, sleep_patch, h = _harness([_FakeResponse(status, body)])
    with patch.object(ArgoConnector, "_perform_login", return_value=False):
        connector = ArgoConnector()

    with session_patch, connector_patch, sleep_patch:
        assert await connector.login() is False

    assert connector.auth_token is None, "a login without a token still handed one out"
    assert argo_module._token_cache == {}, "a failed login poisoned the shared token cache"
    assert len(h.calls) == 1, "an answer from the server is not a connect failure, so it is not repeated"
    assert h.sleeps == []


@pytest.mark.asyncio
async def test_a_login_redirected_to_https_switches_the_connector_over() -> None:
    """ArgoCD redirects plain HTTP to HTTPS, and the connector follows for every later call.

    This block moved into _login_once as well. It rewrites the base URL, which is also
    half of the token cache key, so losing it would send every later request back to the
    port that redirected.
    """
    answer = _FakeResponse(200, '{"token": "token-fresh"}')
    answer.url = "https://argocd-server:443/api/v1/session"
    session_patch, connector_patch, sleep_patch, h = _harness([answer])
    with patch.object(ArgoConnector, "_perform_login", return_value=False):
        connector = ArgoConnector()
    assert connector.base_url == "http://argocd-server:80"

    with session_patch, connector_patch, sleep_patch:
        assert await connector.login() is True

    assert connector.base_url == "https://argocd-server:443", "a later call would go back to the plain port"
    assert connector._actual_base_url == "https://argocd-server:443"
    assert argo_module._token_cache == {("https://argocd-server:443", "admin"): "token-fresh"}
    assert len(h.calls) == 1


def _connector_recorder(built: list[ArgoConnector]):
    """A create_argo_connector that hands back the real thing and keeps a reference.

    The budget is then read from the ATTRIBUTE of the connector the handler really built,
    the same measurement as for the status backend, so it covers the factory passing the
    argument through as well as the call site naming it.
    """

    def _record(**kwargs: Any) -> ArgoConnector:
        connector = create_argo_connector(**kwargs)
        built.append(connector)
        return connector

    return _record


def _logs_in(self: ArgoConnector) -> bool:
    self.auth_token = "token-1"
    return True


def _project_with_one_deployment() -> Any:
    store = MagicMock()
    project = SimpleNamespace(
        name="proj",
        data={"deployments": [{"name": "dep", "cluster": "odcn-production", "namespace": "proj"}]},
        users=[],
    )
    store.get_all.return_value = [project]
    store.get.return_value = project
    return store


@pytest.mark.asyncio
async def test_the_dashboard_builds_a_fail_fast_connector() -> None:
    """/dashboard asks ArgoCD per deployment, sequentially, inside a single request.

    On the default budget an unreachable ArgoCD costs 30 seconds per deployment there,
    where the page used to fail within milliseconds and put "Unknown" in the column.
    """
    built: list[ArgoConnector] = []

    with (
        patch.object(ArgoConnector, "_perform_login", _logs_in),
        patch("opi.connectors.argo.create_argo_connector", _connector_recorder(built)),
        patch.object(ArgoConnector, "get_application_status", AsyncMock(return_value=None)),
        patch("opi.web.router.get_current_user", MagicMock(return_value={"email": "u@example.com"})),
        patch("opi.web.router.get_project_store", MagicMock(return_value=_project_with_one_deployment())),
        patch("opi.web.router.is_user_authorized_for_project", MagicMock(return_value=True)),
        patch("opi.web.router.render", MagicMock(return_value=HTMLResponse("ok"))),
    ):
        await dashboard(MagicMock())

    assert [c.connect_retry_seconds for c in built] == [0], "a page render would wait out an ArgoCD restart"


@pytest.mark.asyncio
async def test_the_status_fragment_builds_a_fail_fast_connector() -> None:
    """The lazy-loaded card sits in a page too, so it falls under the same rule."""
    built: list[ArgoConnector] = []

    with (
        patch.object(ArgoConnector, "_perform_login", _logs_in),
        patch("opi.connectors.argo.create_argo_connector", _connector_recorder(built)),
        patch("opi.web.router.get_current_user", MagicMock(return_value={"email": "u@example.com"})),
        patch("opi.web.router.get_project_store", MagicMock(return_value=_project_with_one_deployment())),
        patch("opi.web.router.is_user_authorized_for_project", MagicMock(return_value=True)),
        patch("opi.web.router._fetch_argocd_deployment_status", AsyncMock(return_value={})),
        patch("opi.connectors.kubectl.create_kubectl_connector", MagicMock()),
        patch("opi.web.router.render", MagicMock(return_value=HTMLResponse("ok"))),
    ):
        await argocd_status_fragment(MagicMock(), "proj", "dep")

    assert [c.connect_retry_seconds for c in built] == [0], "a lazy-loaded card would wait out an ArgoCD restart"


@pytest.mark.asyncio
async def test_a_fail_fast_connector_still_logs_in_when_the_refresh_lock_is_free() -> None:
    """Budget 0 means "do not queue behind someone else", not "never log in".

    The login itself already fails fast on its own budget, so an uncontended lock has
    to be handed over immediately instead of being refused along with the waiting.
    """
    connector = _unauthenticated_connector(0)
    logins: list[str] = []

    async def _login(self: ArgoConnector) -> bool:
        logins.append("login")
        self.auth_token = "token-fresh"
        self._store_token(self.auth_token)
        return True

    with patch.object(ArgoConnector, "login", _login):
        assert await connector._ensure_authenticated() is True

    assert logins == ["login"], "a free lock refused a fail-fast caller its login"
    assert connector.auth_token == "token-fresh"
    assert not argo_module._token_refresh_lock.locked(), "the lock was not released"


@pytest.mark.asyncio
async def test_a_fail_fast_connector_does_not_queue_behind_another_login() -> None:
    """The lock is held across a login that now retries, so waiting on it costs 30s.

    Reachable with an empty token cache: at process start, and after every 401, which
    clears the process-wide cache. Without a bound on the wait a page render on budget
    0 sits out the whole backoff of a background caller, which is what that 0 is for.
    """
    connector = _unauthenticated_connector(0)
    logins: list[str] = []

    async def _login(self: ArgoConnector) -> bool:
        logins.append("login")
        return True

    async with argo_module._token_refresh_lock:
        started = time.monotonic()
        with patch.object(ArgoConnector, "login", _login):
            # An unbounded wait would hang here, so the wait_for turns that into red.
            answer = await asyncio.wait_for(connector._ensure_authenticated(), timeout=2)
        waited = time.monotonic() - started

    assert answer is False, "the caller has to degrade, the same as it does on a failed login"
    assert logins == [], "the lock was taken from under the caller that is holding it"
    assert waited < 0.5, f"a fail-fast caller waited {waited:.2f}s for someone else's login"


@pytest.mark.asyncio
async def test_a_background_caller_still_adopts_the_token_of_the_login_in_flight() -> None:
    """Bounding the wait must not break the collapse: one login for both callers.

    The first caller sleeps inside the login the way a real one does, so the second
    really queues on the lock instead of finding it free.
    """
    first = _unauthenticated_connector(30.0)
    second = _unauthenticated_connector(30.0)
    logins: list[str] = []

    async def _slow_login(self: ArgoConnector) -> bool:
        logins.append("login")
        await asyncio.sleep(0.05)
        self.auth_token = "token-shared"
        self._store_token(self.auth_token)
        return True

    with patch.object(ArgoConnector, "login", _slow_login):
        answers = await asyncio.gather(first._ensure_authenticated(), second._ensure_authenticated())

    assert answers == [True, True]
    assert logins == ["login"], "the second caller logged in again instead of adopting the token"
    assert second.auth_token == "token-shared"


@pytest.mark.asyncio
async def test_a_background_caller_waits_out_a_login_that_outlasts_its_own_budget() -> None:
    """A caller that asked to ride out a restart must not bound that wait on its own budget.

    On budget 30 with argocd 20 seconds away the holder's login attempts land on t=0, 1, 3,
    7, 15 and 30, and the one that succeeds costs another ~700ms of bcrypt. A bound taken
    from the waiter's own budget therefore expires about 0,7s BEFORE the token it is waiting
    for lands, and the False that comes out of it becomes a 401 -> RuntimeError that kills
    the deployment task this retry exists for. Scaled down here: the holder finishes after
    the waiter's whole budget is gone.
    """
    budget = 0.05
    holder = _unauthenticated_connector(budget)
    waiter = _unauthenticated_connector(budget)
    logins: list[str] = []

    async def _slow_login(self: ArgoConnector) -> bool:
        logins.append("login")
        await asyncio.sleep(budget * 4)
        self.auth_token = "token-shared"
        self._store_token(self.auth_token)
        return True

    with patch.object(ArgoConnector, "login", _slow_login):
        answers = await asyncio.gather(holder._ensure_authenticated(), waiter._ensure_authenticated())

    assert answers == [True, True], "the waiter gave up before the token it was waiting for landed"
    assert logins == ["login"], "the waiter logged in again instead of adopting the token"
    assert waiter.auth_token == "token-shared"


@pytest.mark.asyncio
async def test_a_fail_fast_request_whose_401_relogin_meets_the_lock_degrades_at_once() -> None:
    """The window the bounded wait was added for, measured from the caller's side.

    A 401 clears the process-wide token cache, so the re-authentication behind it goes
    for the shared refresh lock. Holding that lock now costs the holder's whole retry
    budget, so an interactive request on budget 0 would sit out a background caller's
    backoff, which is exactly what its 0 is meant to prevent. It has to come back with
    the same answer it gives for a login that failed.
    """
    session_patch, connector_patch, sleep_patch, h = _harness([_FakeResponse(401, "token expired")])
    connector = _authenticated_connector(connect_retry_seconds=0)

    async with argo_module._token_refresh_lock:
        started = time.monotonic()
        with session_patch, connector_patch, sleep_patch:
            # An unbounded wait would hang here, so the wait_for turns that into red.
            status, text = await asyncio.wait_for(
                connector._make_authenticated_request("GET", _APPLICATIONS_URL), timeout=2
            )
        waited = time.monotonic() - started

    assert (status, text) == (401, "Re-authentication failed")
    assert len(h.calls) == 1, "the request went out again without ever getting a fresh token"
    assert waited < 0.5, f"an interactive request waited {waited:.2f}s for someone else's login"
