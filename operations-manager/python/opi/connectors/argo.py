"""Authenticate with ArgoCD and drive its applications: sync, refresh, status, deletion."""

import asyncio
import functools
import json
import logging
import ssl
import threading
from typing import Any

import aiohttp

from opi.connectors.kubectl import KubectlConnectionError, KubectlExecutionError
from opi.core.logging_redact import redact_sensitive_headers
from opi.utils.connect_retry import retry_on_connect_error

logger = logging.getLogger(__name__)

# An ArgoCD session token is a JWT valid for 24 hours, and a login costs ~700ms because
# ArgoCD verifies the password with bcrypt. The token is therefore shared process-wide, keyed
# by (server, user). Two locks: _token_cache_lock guards the dict (threads read it too), and
# _token_refresh_lock makes a burst of callers that all find the cache empty produce ONE
# login instead of one each.
_token_cache: dict[tuple[str, str], str] = {}
_token_cache_lock = threading.Lock()
_token_refresh_lock = asyncio.Lock()

# While argocd-server restarts its API server in-process, the service can be without an
# endpoint for as long as its 20 second graceful-shutdown deadline. 30 seconds covers
# that with margin. Why it restarts: `docs/post-mortems/argocd-server-herstart-elke-40-minuten.md`.
CONNECT_RETRY_SECONDS = 30.0


@functools.cache
def _ssl_context_for(verify_ssl: bool, ca_bundle: str | None) -> ssl.SSLContext:
    """One ssl context per (verify_ssl, ca_bundle), shared across the process.

    Raises:
        ArgoTLSConfigurationError: the bundle at this path is missing or holds no certificate.
    """
    try:
        context = ssl.create_default_context(cafile=ca_bundle)
    except (OSError, ssl.SSLError) as error:
        # The bundle is an optional mount, so a cluster without the ConfigMap arrives here.
        # Falling back to plain HTTP is not a way out.
        raise ArgoTLSConfigurationError(
            f"ARGOCD_CA_BUNDLE={ca_bundle} is niet te lezen als CA-bundel: {_exc_text(error)}"
        ) from error
    if not verify_ssl:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


#: One shared ClientSession per (verify_ssl, ca_bundle) and per event loop. The loop belongs
#: in the key because a session is bound to the loop it was made on.
_sessions: dict[tuple[bool, str | None, int], aiohttp.ClientSession] = {}
_session_lock = asyncio.Lock()


async def _shared_session(verify_ssl: bool, ca_bundle: str | None) -> aiohttp.ClientSession:
    """The shared session for this TLS setting, created on first use."""
    key = (verify_ssl, ca_bundle, id(asyncio.get_running_loop()))
    session = _sessions.get(key)
    if session is not None and not session.closed:
        return session
    async with _session_lock:
        session = _sessions.get(key)
        if session is not None and not session.closed:
            return session
        session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=_ssl_context_for(verify_ssl, ca_bundle)))
        _sessions[key] = session
        return session


async def close_shared_sessions() -> None:
    """Close the shared sessions. Belongs to shutting the process down."""
    for session in list(_sessions.values()):
        if not session.closed:
            await session.close()
    _sessions.clear()


def _exc_text(error: BaseException) -> str:
    """The text of an exception, or its class when that text is empty.

    TimeoutError has an empty str(), so str(error) alone logs nothing on a timeout.
    """
    return str(error) or type(error).__name__


class ArgoTransportError(RuntimeError):
    """The question to ArgoCD did not arrive or did not come back: timeout, connect error, 5xx.

    It says nothing about the state of the application, so a wait loop keeps polling rather
    than breaking off.
    """


class ArgoTLSConfigurationError(ValueError):
    """TLS is on with certificate verification, but there is no CA bundle to verify against.

    The CA of the ArgoCD server (the service CA on ODCN, our own CA on the sandbox) is not in
    the container's system trust, so without a bundle every call fails anyway.
    """


class ArgoAuthConfigurationError(ValueError):
    """There is no way in: neither ARGOCD_TOKEN nor a user with a password.

    OPI works with its own account `opi` and its API token.
    """


class ArgoConnector:
    def __init__(
        self,
        server_host: str = "argocd-server",
        server_port: int = 80,
        username: str | None = None,
        password: str | None = None,
        token: str | None = None,
        use_tls: bool = False,
        verify_ssl: bool = False,
        ca_bundle: str | None = None,
        connect_retry_seconds: float = CONNECT_RETRY_SECONDS,
    ):
        """``token`` is sent as a bearer and skips the login path altogether.

        ``ca_bundle`` is the path of the mounted bundle the server certificate is verified
        against; ``connect_retry_seconds`` is how long a connect failure keeps being retried,
        0 to fail fast.

        Raises:
            ArgoTLSConfigurationError: TLS with certificate verification but no CA bundle.
        """
        if use_tls and verify_ssl and not ca_bundle:
            raise ArgoTLSConfigurationError("ARGOCD_USE_TLS en ARGOCD_VERIFY_SSL staan aan zonder ARGOCD_CA_BUNDLE")

        self.server_host = server_host
        self.server_port = server_port
        self.username = username
        self.password = password
        self.token = token
        self.use_tls = use_tls
        self.verify_ssl = verify_ssl
        self.ca_bundle = ca_bundle
        self.connect_retry_seconds = connect_retry_seconds

        protocol = "https" if use_tls else "http"
        self.base_url = f"{protocol}://{server_host}:{server_port}"
        self._actual_base_url = self.base_url

        self.auth_token: str | None = None
        self.default_app_name = "user-applications"

        logger.debug(f"ArgoConnector initialized with server: {self.base_url}")

        # An API token skips the cache: it does not expire per session and is the same for
        # every connector. Without one, read the cache and do not log in on the loop
        # (~700ms, plus a 10s timeout when ArgoCD is unreachable); ensure_authenticated
        # logs in asynchronously when it has to.
        if token:
            self.auth_token = token
        else:
            self.auth_token = self._cached_token()

    def _cache_key(self) -> tuple[str, str]:
        return (self.base_url, self.username or "")

    def _cached_token(self) -> str | None:
        with _token_cache_lock:
            return _token_cache.get(self._cache_key())

    def _store_token(self, token: str) -> None:
        with _token_cache_lock:
            _token_cache[self._cache_key()] = token

    def _invalidate_token(self, used_token: str | None) -> None:
        """Drop the shared token, but only if it is still the one that just failed.

        Compare-and-clear: another caller may already have replaced it after our request went
        out, and clearing unconditionally would throw away that fresh token.
        """
        if used_token is None:
            return
        with _token_cache_lock:
            if _token_cache.get(self._cache_key()) == used_token:
                _token_cache.pop(self._cache_key(), None)

    async def login(self) -> bool:
        """Obtain a session token; False when the login failed.

        With an API token there is nothing to log in to: the token IS the authorization.
        """
        if self.token:
            self.auth_token = self.token
            return True

        try:
            return await retry_on_connect_error(self._login_once, self.connect_retry_seconds, "login")
        except Exception as e:
            logger.error(f"Error during ArgoCD login: {_exc_text(e)}")
            return False

    async def _login_once(self) -> bool:
        """One login attempt. A connect failure propagates so login() can retry it."""
        logger.info(f"Logging in to ArgoCD server: {self.base_url}")

        login_url = f"{self.base_url}/api/v1/session"
        login_data = {"username": self.username, "password": self.password}

        session = await _shared_session(self.verify_ssl, self.ca_bundle)
        async with session.post(
            login_url,
            json=login_data,
            headers={"Content-Type": "application/json"},
            timeout=aiohttp.ClientTimeout(total=30),
        ) as response:
            if response.status == 200:
                response_data = await response.json()
                logger.debug(f"Login response data: {response_data}")
                self.auth_token = response_data.get("token")
                if self.auth_token:
                    self._store_token(self.auth_token)
                    logger.info("Successfully logged in to ArgoCD - token received")
                    return True
                else:
                    logger.error("Login response missing token")
                    return False
            else:
                error_text = await response.text()
                logger.error(f"Login failed with status {response.status}: {error_text}")
                return False

    def _ssl_context(self) -> ssl.SSLContext:
        return _ssl_context_for(self.verify_ssl, self.ca_bundle)

    async def ensure_authenticated(self) -> bool:
        """Ensure we have a valid authentication token, logging in at most once.

        Also the "is ArgoCD reachable" check of the pages, because the constructor no longer
        logs in.

        The shared token is re-checked after acquiring the refresh lock: when many callers hit
        an empty cache at the same moment, the first performs the login and the rest adopt its
        token instead of each paying another ~700ms.
        """
        if self.auth_token:
            return True

        cached = self._cached_token()
        if cached:
            self.auth_token = cached
            return True

        # The login behind this lock retries connect failures, so holding it can cost the
        # holder's whole budget. Only a caller that asked to fail fast refuses to queue up
        # behind that. An unheld lock is still acquired at once, so this refuses to wait,
        # never to log in.
        #
        # A caller that did ask to ride out a restart waits for the holder WITHOUT a bound of
        # its own: on its own budget it would give up just before the holder succeeds, since
        # the holder's last attempt lands at the end of that same budget and its login costs
        # another ~700ms of bcrypt. The wait is bounded in practice, because the holder is off
        # the lock within its own budget plus one login.
        wait_seconds = None if self.connect_retry_seconds > 0 else 0
        try:
            async with asyncio.timeout(wait_seconds):
                await _token_refresh_lock.acquire()
        except TimeoutError:
            logger.warning("Not waiting for an ArgoCD login in flight elsewhere: this caller asked to fail fast")
            return False
        try:
            cached = self._cached_token()
            if cached:
                self.auth_token = cached
                return True
            logger.info("No authentication token available. Performing async login.")
            return await self.login()
        finally:
            _token_refresh_lock.release()

    async def _make_authenticated_request(
        self,
        method: str,
        url: str,
        json_data: dict | None = None,
        retry_count: int = 0,
        timeout_seconds: int = 30,
    ) -> tuple[int, str]:
        """Make an authenticated request, re-authenticating once on a 401."""
        if not await self.ensure_authenticated():
            return 401, "Authentication failed"

        session = await _shared_session(self.verify_ssl, self.ca_bundle)
        request_timeout = aiohttp.ClientTimeout(total=timeout_seconds)

        headers = {"Authorization": f"Bearer {self.auth_token}", "Content-Type": "application/json"}
        logger.debug(f"Request headers: {redact_sensitive_headers(headers)}")
        logger.debug(f"Making {method} request to: {url}")

        async def _send() -> tuple[int, str]:
            async with session.request(
                method, url, json=json_data or {}, headers=headers, timeout=request_timeout
            ) as response:
                return response.status, await response.text()

        status, response_text = await retry_on_connect_error(_send, self.connect_retry_seconds, f"{method} {url}")
        logger.debug(f"Response status: {status}")
        logger.debug(
            f"Response text: {response_text[:200]}..."
            if len(response_text) > 200
            else f"Response text: {response_text}"
        )

        if status == 401 and self.token:
            logger.error(
                "ArgoCD wees het API-token van dit account af; opnieuw aanbieden helpt niet. "
                "Roteer ARGOCD_TOKEN (scripts/edit-secret.py --component argocd-opi-token)."
            )
            return 401, response_text

        if status == 401 and retry_count == 0:
            logger.warning("Received 401 Unauthorized. Attempting to re-login and retry.")
            # Invalidate only the token this request actually used, then
            # re-authenticate through the shared path so concurrent 401s
            # collapse into a single login.
            self._invalidate_token(self.auth_token)
            self.auth_token = None
            if await self.ensure_authenticated():
                logger.info("Re-authentication successful, retrying request")
                return await self._make_authenticated_request(method, url, json_data, retry_count + 1)
            else:
                logger.error("Re-authentication failed")
                return 401, "Re-authentication failed"
        elif status == 401:
            logger.error("Still receiving 401 after re-authentication attempt")
            return 401, "Authentication failed after retry"

        return status, response_text

    async def sync_application(self, app_name: str | None = None) -> bool:
        """Trigger synchronization of an ArgoCD application.

        ``app_name`` defaults to ``default_app_name``.
        """
        app_name = app_name or self.default_app_name
        logger.info(f"Triggering sync for application: {app_name}")

        sync_url = f"{self._actual_base_url}/api/v1/applications/{app_name}/sync"

        try:
            status_code, response_text = await self._make_authenticated_request("POST", sync_url)

            if status_code in [200, 201]:
                logger.info(f"Successfully triggered sync for application: {app_name}")
                return True
            else:
                logger.error(f"Sync failed with status {status_code}: {response_text}")
                return False

        except Exception as e:
            reason = str(e) or repr(e)
            logger.error(f"Error during sync of application '{app_name}': {reason}")
            return False

    async def get_application_status(self, app_name: str | None = None) -> dict[str, Any] | None:
        """
        Get the status of an ArgoCD application.

        Args:
            app_name: Name of the application. If None, uses default_app_name

        Returns:
            Application status dictionary if successful, None if the application does not
            exist (ArgoCD returns 404, or 403 for a non-existent app)

        Raises:
            ArgoTransportError: If the request did not complete (timeout, connect error, 5xx)
            RuntimeError: If an unexpected error occurs
        """
        app_name = app_name or self.default_app_name
        logger.info(f"Getting status for application: {app_name}")

        status_url = f"{self._actual_base_url}/api/v1/applications/{app_name}"

        try:
            status_code, response_text = await self._make_authenticated_request("GET", status_url, timeout_seconds=10)

            if status_code == 200:
                status_data = json.loads(response_text)
                logger.info(f"Successfully retrieved status for application: {app_name}")
                return status_data
            elif status_code == 404:
                logger.info(f"Application {app_name} not found (404)")
                return None
            elif status_code == 403:
                # ArgoCD returns 403 (not 404) for an application that does not exist, to
                # avoid leaking which apps are present. Since RC-345 OPI is the account opi
                # and no longer the admin; its role:zad-opi allows `applications, get` on
                # `*/*`, so a 403 here is still not a real authorization failure - it means
                # the app is not there yet (just created, AppProject still propagating) or
                # already gone. Narrow that role and a denial would read as "not present",
                # which is why wait_for_application_deletion confirms absence against the
                # Kubernetes API and never on this answer alone. Treat it as not-found;
                # every caller already handled the old PermissionError exactly like the
                # None/404 case (poll-and-wait, or "already deleted"), so this centralizes
                # that and drops the misleading "permission denied" error noise.
                logger.debug(f"Application {app_name} not found (403 - does not exist yet or already gone)")
                return None
            elif status_code >= 500:
                logger.warning(f"Status request failed with status {status_code}: {response_text}")
                raise ArgoTransportError(f"Failed to get application status: HTTP {status_code}")
            else:
                logger.error(f"Status request failed with status {status_code}: {response_text}")
                raise RuntimeError(f"Failed to get application status: HTTP {status_code}")

        except PermissionError:
            raise
        except RuntimeError:
            raise
        except Exception as e:
            logger.warning(f"Error getting application status: {_exc_text(e)}")
            raise ArgoTransportError(f"Error getting application status: {_exc_text(e)}") from e

    async def get_application_manifests(
        self, app_name: str | None = None, revision: str | None = None
    ) -> tuple[bool, str]:
        """Ask ArgoCD to render an application's manifests, to detect generation failures.

        This is the API behind ``argocd app manifests``. On a broken kustomization / CMP
        render ArgoCD returns the generation error (including the plugin stderr) instead of
        manifests, so calling it right after our own push fails fast with the real message
        instead of waiting for the controller to reconcile.

        ``revision`` is the git revision to render; without it ArgoCD renders the
        application's current target revision.

        Returns:
            ``(ok, body)`` where ``ok`` is True on HTTP 200 (the render succeeded; the
            manifests themselves are not needed, only that it worked). On any other status
            or a transport error ``ok`` is False and ``body`` is the response body or error
            text, which for a generation failure holds the underlying cause. The caller
            decides whether the body indicates a render failure worth blocking on.
        """
        app_name = app_name or self.default_app_name
        manifests_url = f"{self._actual_base_url}/api/v1/applications/{app_name}/manifests"
        if revision:
            manifests_url += f"?revision={revision}"

        try:
            status_code, response_text = await self._make_authenticated_request(
                "GET", manifests_url, timeout_seconds=120
            )
            if status_code == 200:
                logger.debug(f"Application '{app_name}' rendered successfully")
                return True, response_text
            logger.warning(f"Manifest render for '{app_name}' returned status {status_code}")
            return False, response_text
        except Exception as e:
            reason = str(e) or repr(e)
            logger.error(f"Error rendering manifests for application '{app_name}': {reason}")
            return False, reason

    async def get_application_resource_tree(self, app_name: str | None = None) -> list[dict[str, Any]]:
        """The resource tree's nodes, or an empty list on any failure.

        The tree holds the child resources (Pods, ReplicaSets) with their health status and
        messages, which is more detail than the top-level application status carries: an image
        pull error, for instance, only shows on the Pod.
        """
        app_name = app_name or self.default_app_name
        logger.info(f"Getting resource tree for application: {app_name}")

        tree_url = f"{self._actual_base_url}/api/v1/applications/{app_name}/resource-tree"

        try:
            status_code, response_text = await self._make_authenticated_request("GET", tree_url, timeout_seconds=5)

            if status_code == 200:
                tree_data = json.loads(response_text)
                nodes = tree_data.get("nodes", [])
                logger.info(f"Successfully retrieved resource tree for {app_name}: {len(nodes)} nodes")
                return nodes
            elif status_code == 404:
                logger.info(f"Application {app_name} not found (404)")
                return []
            else:
                logger.error(f"Resource tree request failed with status {status_code}: {response_text}")
                return []

        except Exception as e:
            logger.error(f"Error getting resource tree for {app_name}: {_exc_text(e)}")
            return []

    async def list_applications(self) -> list[dict[str, Any]]:
        """Every application, or an empty list on any failure."""
        logger.debug("Listing all ArgoCD applications")
        list_url = f"{self._actual_base_url}/api/v1/applications"

        try:
            status_code, response_text = await self._make_authenticated_request("GET", list_url)
            if status_code == 200:
                response_data = json.loads(response_text)
                applications = response_data.get("items", [])
                logger.info(f"Successfully retrieved {len(applications)} applications")
                return applications
            else:
                logger.error(f"List applications request failed with status {status_code}: {response_text}")
                return []
        except Exception as e:
            logger.error(f"Error listing applications: {_exc_text(e)}")
            return []

    async def login_and_sync(self, app_name: str | None = None) -> str | None:
        """The ``reconciledAt`` timestamp on success, ``None`` on failure."""
        return await self.refresh_application(app_name)

    async def refresh_application(self, app_name: str | None = None, hard_refresh: bool = False) -> str | None:
        """Refresh an application; ``hard_refresh`` also clears the manifest cache and is slower.

        Returns the ``reconciledAt`` timestamp on success and ``None`` on failure. Pass that
        value to ``wait_for_application_synced``, which uses it to tell a fresh status from a
        stale one.
        """
        app_name = app_name or self.default_app_name
        refresh_type = "hard" if hard_refresh else "normal"
        logger.info(f"Triggering {refresh_type} refresh for application: {app_name}")

        # A refresh is a GET with a query parameter, not a POST.
        refresh_param = "hard" if hard_refresh else "normal"
        refresh_url = f"{self._actual_base_url}/api/v1/applications/{app_name}?refresh={refresh_param}"

        try:
            status_code, response_text = await self._make_authenticated_request("GET", refresh_url, timeout_seconds=120)

            if status_code == 200:
                logger.info(f"Successfully triggered {refresh_type} refresh for application: {app_name}")
                response_data = json.loads(response_text)
                status = response_data.get("status", {}) or {}
                # ArgoCD sets an *Error condition (e.g. ComparisonError) when it cannot
                # generate or compare the manifests. Logging it here puts the real cause in
                # the OPI logs instead of only in the ArgoCD UI.
                for condition in status.get("conditions", []) or []:
                    if str(condition.get("type", "")).endswith("Error"):
                        logger.warning(
                            "Application '%s' has condition %s: %s",
                            app_name,
                            condition.get("type"),
                            condition.get("message", ""),
                        )
                reconciled_at = status.get("reconciledAt")
                logger.debug(f"Application '{app_name}' reconciledAt after refresh: {reconciled_at}")
                return reconciled_at
            elif status_code == 404:
                # Expected during new-project bootstrap: ArgoCD has not created the
                # application yet. Callers wait for it to appear and retry, so this is a
                # warning, not an error to escalate on. Mirrors get_application_status().
                logger.warning(f"Application {app_name} not present yet (404), skipping {refresh_type} refresh")
                return None
            else:
                logger.error(f"{refresh_type.title()} refresh failed with status {status_code}: {response_text}")
                return None

        except Exception as e:
            # Some transport errors stringify to "": fall back to the repr so the line is
            # never a bare "... refresh:" with no reason.
            reason = str(e) or repr(e)
            logger.error(f"Error during {refresh_type} refresh of application '{app_name}': {reason}")
            return None

    async def hard_refresh_application(self, app_name: str | None = None) -> str | None:
        """A hard refresh, which clears the cache and forces a re-render.

        Use sparingly: it is resource-intensive and can slow ArgoCD down. The default soft
        refresh is enough for nearly everything.
        """
        return await self.refresh_application(app_name, hard_refresh=True)

    async def application_exists(self, app_name: str) -> bool:
        """Whether the application exists.

        Raises:
            PermissionError: access to the application was denied.
            RuntimeError: any other unexpected status.
        """
        logger.debug(f"Checking if application exists: {app_name}")

        status_data = await self.get_application_status(app_name)
        exists = status_data is not None
        logger.debug(f"Application {app_name} exists: {exists}")
        return exists

    async def wait_for_application_deletion(
        self,
        app_name: str,
        max_retries: int = 5,
        retry_delay: int = 3,
        kubectl_connector: Any = None,
        namespace: str | None = None,
        progress_window_seconds: int | None = None,
        stall_retries: int = 12,
    ) -> bool:
        """True only when the application is CONFIRMED deleted.

        ArgoCD is queried first, but its API is not trustworthy under control-plane stress: it
        answers 'permission denied' for applications that still exist, conflating "gone",
        "can't see it" and "I'm stalled". That answer is therefore never read as "deleted":
        anything but a confident "still exists" is double-checked against the Kubernetes API,
        which fails honestly (the object, a clean NotFound, or a distinguishable error).

        Args:
            kubectl_connector: Confirms absence against the Kubernetes API when ArgoCD is
                ambiguous. Without it an ambiguous answer cannot be confirmed and deletion is
                reported as unconfirmed (False).
            namespace: Namespace holding the ArgoCD Application CR; defaults to this
                instance's cluster.
            progress_window_seconds: How long to keep waiting WHILE the namespace is visibly
                EMPTYING, beyond the ``max_retries`` window. ``None`` ends the wait at
                ``max_retries``, progress or not.
            stall_retries: How many consecutive polls without a MEASURED DECREASE end the wait
                once the ``max_retries`` window has passed. A read that failed counts here
                too: it is not a stall, but it is no proof of progress either, and only a
                namespace that empties earns the extra window.
        """
        import asyncio

        # Beyond the plain max_retries window the wait continues while the namespace keeps
        # EMPTYING, and gives up on a stall instead of on the clock. The window AND the stall
        # both have to be reached: on the stall alone a brief hiccup in a healthy cascade
        # would end the wait early. See features/argocd-vastgelopen-verwijdering.md.
        max_progress_retries = 0 if progress_window_seconds is None else progress_window_seconds // max(retry_delay, 1)
        limit = max(max_retries, max_progress_retries)
        progress_window = max_progress_retries > max_retries
        without_progress = 0
        # The LOWEST count seen, not the previous one. Against the previous count a namespace
        # that bounces (30, 31, 30, ...) scores a decrease every other poll and so buys the
        # wider window without ever emptying. A cascade that really works keeps setting a
        # new low.
        lowest_remaining: int | None = None
        progress_types: list[str] | None = None
        types_unavailable = False

        logger.info(
            f"Waiting for application deletion: {app_name} "
            f"(max {max_retries} retries, up to {limit} while its namespace keeps emptying)"
        )

        async def _deletion_progress() -> tuple[bool, int, bool] | None:
            nonlocal progress_types, types_unavailable
            if kubectl_connector is None or not progress_window or types_unavailable:
                return None
            try:
                if progress_types is None:
                    # Discovered once per wait, failure included: the types a cluster has do
                    # not change while a cascade runs, and retrying the discovery would repeat
                    # both the api-resources call and its ERROR on every poll. Without types
                    # there is nothing to measure, so the poll yields no window either.
                    # Only the kinds this Application owns, falling back to full discovery.
                    progress_types = await kubectl_connector.argocd_application_resource_types(app_name, namespace)
                    if progress_types is None:
                        progress_types = await kubectl_connector.list_namespaced_resource_types()
                    if progress_types is None:
                        types_unavailable = True
                        return None
                return await kubectl_connector.argocd_application_deletion_progress(app_name, namespace, progress_types)
            except (KubectlConnectionError, KubectlExecutionError) as e:
                # A kubectl that throws is a FAILED MEASUREMENT, not a failed wait: the same
                # None as an unreadable inventory, so no stall and no wider window. kubectl
                # raises this the moment its connection test flips is_connected to False, which
                # is exactly the stressed control plane this measurement was built for. Letting
                # it out skips the force route at the caller, and with force=True the caller's
                # except marks the Application deleted, so the project file would lose the
                # deployment while the cluster still has it.
                logger.warning(f"Could not measure deletion progress for {app_name}: {e}")
                # Only a failed DISCOVERY is remembered, for the reason above; the inventory is
                # a per-poll read and may well succeed on the next one.
                types_unavailable = progress_types is None
                return None

        async def _confirmed_gone_via_k8s() -> bool:
            # Ground-truth fallback. Only a clean NotFound (False) counts as gone;
            # still-present (True) or unknown (None) must not be read as deleted.
            if kubectl_connector is None:
                return False
            return (await kubectl_connector.argocd_application_exists(app_name, namespace)) is False

        for attempt in range(limit):
            try:
                exists = await self.application_exists(app_name)
                # ArgoCD reports the app gone. A clean 404 is fairly reliable, but since
                # the same API lies under stress we still confirm via the Kubernetes API
                # when we can before declaring success.
                if not exists and (kubectl_connector is None or await _confirmed_gone_via_k8s()):
                    logger.info(f"Application {app_name} confirmed deleted after {attempt + 1} checks")
                    return True

                logger.debug(f"Application {app_name} still exists, retry {attempt + 1}/{limit}")

            except PermissionError:
                # FALLBACK: 'permission denied' is NOT proof the app is gone. ArgoCD answers
                # this while merely stalled, for apps that still exist, so the Kubernetes API
                # is asked directly.
                if await _confirmed_gone_via_k8s():
                    logger.info(
                        f"Application {app_name} confirmed deleted via Kubernetes API "
                        f"(ArgoCD returned permission denied; not trusting it as 'deleted')"
                    )
                    return True
                logger.warning(
                    f"Application {app_name}: ArgoCD returned permission denied but the Kubernetes API "
                    f"shows it still present (or could not confirm) - treating as NOT deleted"
                )

            except Exception as e:
                logger.error(f"Error checking application deletion status: {_exc_text(e)}")

            # Never raises: a measurement that fails is None, see _deletion_progress.
            progress = await _deletion_progress()
            if progress is None:
                # Only a MEASURED decrease buys the extra window. An Application that could
                # not be read is not a stall, but it is no proof of progress either, and on
                # the absence of information the wait keeps its plain max_retries.
                without_progress += 1
            else:
                underway, remaining, complete = progress
                # Emptying, not merely changing. A count that grows or bounces is movement
                # without progress, and deletes are serialised per deployment, so a delete and
                # an add can share one namespace: the add's resources must not buy the wider
                # window for the stuck delete. ``complete`` is logged for the reader and kept
                # out of the comparison, since a flapping inventory is not progress either.
                new_low = lowest_remaining is None or remaining < lowest_remaining
                verdict = (
                    " - the namespace is still emptying, waiting"
                    if new_low
                    else f" - no decrease below {lowest_remaining}"
                )
                logger.info(
                    f"Application {app_name} is still being deleted (deletion underway: {underway}, "
                    f"{remaining} ArgoCD-owned resources left in its namespace"
                    f"{'' if complete else ', inventory incomplete'}){verdict}"
                )
                if new_low:
                    lowest_remaining = remaining
                    without_progress = 0
                else:
                    without_progress += 1

            if progress_window and attempt + 1 >= max_retries and without_progress >= stall_retries:
                logger.warning(
                    f"Application {app_name} showed no measured decrease of its namespace for {without_progress} "
                    f"consecutive polls after {attempt + 1} retries - treating it as stuck"
                )
                return False

            if attempt < limit - 1:  # Don't sleep on the last attempt
                await asyncio.sleep(retry_delay)

        logger.warning(f"Application {app_name} NOT confirmed deleted after {limit} retries")
        return False


def create_argo_connector(
    server_host: str | None = None,
    server_port: int | None = None,
    username: str | None = None,
    password: str | None = None,
    token: str | None = None,
    use_tls: bool | None = None,
    verify_ssl: bool | None = None,
    ca_bundle: str | None = None,
    connect_retry_seconds: float = CONNECT_RETRY_SECONDS,
) -> ArgoConnector:
    """An ArgoConnector; every parameter left at None comes from the settings.

    Raises:
        ArgoAuthConfigurationError: no token and no user with a password configured.
    """
    from opi.core.config import settings

    final_server_host = server_host if server_host is not None else settings.ARGOCD_HOST
    final_server_port = server_port if server_port is not None else settings.ARGOCD_PORT
    final_username = username if username is not None else settings.ARGOCD_USERNAME
    final_password = password if password is not None else settings.ARGOCD_PASSWORD
    final_use_tls = use_tls if use_tls is not None else settings.ARGOCD_USE_TLS
    final_verify_ssl = verify_ssl if verify_ssl is not None else settings.ARGOCD_VERIFY_SSL
    final_ca_bundle = ca_bundle if ca_bundle is not None else settings.ARGOCD_CA_BUNDLE
    final_token = token if token is not None else settings.ARGOCD_TOKEN

    if not final_token and not (final_username and final_password):
        raise ArgoAuthConfigurationError(
            "geen ARGOCD_TOKEN en geen ARGOCD_USERNAME met ARGOCD_PASSWORD; OPI kan niet bij ArgoCD"
        )

    logger.debug(f"Creating ArgoConnector for server: {final_server_host}:{final_server_port}")
    return ArgoConnector(
        server_host=final_server_host,
        server_port=final_server_port,
        username=final_username,
        password=final_password,
        token=final_token,
        use_tls=final_use_tls,
        verify_ssl=final_verify_ssl,
        ca_bundle=final_ca_bundle,
        connect_retry_seconds=connect_retry_seconds,
    )
