"""
ArgoCD connector for managing ArgoCD applications and authentication.

This module provides functionality to authenticate with ArgoCD and manage applications,
including triggering synchronization of applications.
"""

import asyncio
import json
import logging
import ssl
import threading
from typing import Any

import aiohttp
import requests

from opi.utils.logging_redact import redact_sensitive_headers

logger = logging.getLogger(__name__)

# ArgoCD session tokens are JWTs valid for 24 hours, but a connector is built per
# operation (16 call sites) and each construction used to log in again. That login
# costs ~700ms because ArgoCD verifies the password with bcrypt, so it dominated
# every page load and task step that touched ArgoCD.
#
# The token is therefore shared process-wide, keyed by (server, user). Two locks
# guard it because there are two login paths: a synchronous one in __init__ and an
# async one for requests. Both re-check the cache after acquiring, so a burst of
# callers that all find the cache empty produces ONE login instead of one each.
_token_cache: dict[tuple[str, str], str] = {}
_token_cache_lock = threading.Lock()
_token_refresh_lock = asyncio.Lock()


class ArgoConnector:
    """Connector for interacting with ArgoCD server."""

    def __init__(
        self,
        server_host: str = "argocd-server",
        server_port: int = 80,
        username: str = "admin",
        password: str = "admin",  # noqa: S107
        use_tls: bool = False,
        verify_ssl: bool = False,
    ):
        """
        Initialize the ArgoCD connector and perform login.

        Args:
            server_host: ArgoCD server hostname or service name
            server_port: ArgoCD server port
            username: Username for authentication
            password: Password for authentication
            use_tls: Whether to use TLS/HTTPS
            verify_ssl: Whether to verify SSL certificates
        """
        self.server_host = server_host
        self.server_port = server_port
        self.username = username
        self.password = password
        self.use_tls = use_tls
        self.verify_ssl = verify_ssl

        # Build base URL
        protocol = "https" if use_tls else "http"
        self.base_url = f"{protocol}://{server_host}:{server_port}"

        # Handle auto-redirect to HTTPS
        # ArgoCD often redirects HTTP to HTTPS, so we need to detect this
        self._actual_base_url = self.base_url

        # Authentication token
        self.auth_token: str | None = None

        # Default application name
        self.default_app_name = "user-applications"

        logger.debug(f"ArgoConnector initialized with server: {self.base_url}")

        # Reuse the process-wide token when one is already known. Without this every
        # construction paid a ~700ms blocking bcrypt login, on the event loop.
        self.auth_token = self._cached_token()
        if self.auth_token is None:
            # Try to perform initial login during initialization
            # If this fails, async methods will handle re-authentication
            try:
                self._perform_login()
            except Exception as e:
                logger.warning(f"Initial login failed during initialization: {e}")
                logger.info("Will attempt async login when methods are called")

    def _cache_key(self) -> tuple[str, str]:
        return (self.base_url, self.username)

    def _cached_token(self) -> str | None:
        with _token_cache_lock:
            return _token_cache.get(self._cache_key())

    def _store_token(self, token: str) -> None:
        with _token_cache_lock:
            _token_cache[self._cache_key()] = token

    def _invalidate_token(self, used_token: str | None) -> None:
        """Drop the shared token, but only if it is still the one that just failed.

        Compare-and-clear: another caller may already have replaced it after our
        request went out. Clearing unconditionally would throw away that fresh
        token and send everyone back through a login they do not need.
        """
        if used_token is None:
            return
        with _token_cache_lock:
            if _token_cache.get(self._cache_key()) == used_token:
                _token_cache.pop(self._cache_key(), None)

    def _perform_login(self) -> bool:
        """
        Perform login during initialization (synchronous).

        Returns:
            True if login successful, False otherwise
        """
        logger.info(f"Logging in to ArgoCD server: {self.base_url}")

        login_url = f"{self.base_url}/api/v1/session"
        login_data = {"username": self.username, "password": self.password}

        try:
            # Use requests for synchronous login
            response = requests.post(
                login_url,
                json=login_data,
                headers={"Content-Type": "application/json"},
                verify=self.verify_ssl,
                timeout=10,
            )

            # Check if we got redirected to HTTPS
            if response.url.startswith("https://") and self.base_url.startswith("http://"):
                logger.info(f"Detected redirect to HTTPS: {response.url}")
                # Update base URL to use HTTPS
                old_base = self.base_url
                self.base_url = self.base_url.replace("http://", "https://").replace(":80", ":443")
                self._actual_base_url = self.base_url
                logger.info(f"Updated base URL from {old_base} to {self.base_url}")

            if response.status_code == 200:
                response_data = response.json()
                logger.debug("Processing sync login response")
                self.auth_token = response_data.get("token")
                if self.auth_token:
                    self._store_token(self.auth_token)
                    logger.info("Successfully logged in to ArgoCD (sync) - token received")
                    return True
                else:
                    logger.error("Sync login response missing token")
                    return False
            else:
                logger.error(f"Login failed with status {response.status_code}: {response.text}")
                return False

        except Exception as e:
            logger.error(f"Error during ArgoCD login: {e}")
            return False

    async def login(self) -> bool:
        """
        Login to ArgoCD server and obtain authentication token.

        Returns:
            True if login successful, False otherwise
        """
        logger.info(f"Logging in to ArgoCD server: {self.base_url}")

        login_url = f"{self.base_url}/api/v1/session"
        login_data = {"username": self.username, "password": self.password}

        try:
            # Create SSL context
            ssl_context = ssl.create_default_context()
            if not self.verify_ssl:
                ssl_context.check_hostname = False
                ssl_context.verify_mode = ssl.CERT_NONE

            connector = aiohttp.TCPConnector(ssl=ssl_context)
            request_timeout = aiohttp.ClientTimeout(total=30)

            async with (
                aiohttp.ClientSession(connector=connector, timeout=request_timeout) as session,
                session.post(login_url, json=login_data, headers={"Content-Type": "application/json"}) as response,
            ):
                # Check if we got redirected to HTTPS
                if str(response.url).startswith("https://") and self.base_url.startswith("http://"):
                    logger.info(f"Detected redirect to HTTPS: {response.url}")
                    # Update base URL to use HTTPS
                    old_base = self.base_url
                    self.base_url = self.base_url.replace("http://", "https://").replace(":80", ":443")
                    self._actual_base_url = self.base_url
                    logger.info(f"Updated base URL from {old_base} to {self.base_url}")

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

        except Exception as e:
            logger.error(f"Error during ArgoCD login: {e}")
            return False

    async def _create_ssl_context(self) -> ssl.SSLContext:
        """Create SSL context based on configuration."""
        ssl_context = ssl.create_default_context()
        if not self.verify_ssl:
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
        return ssl_context

    async def _ensure_authenticated(self) -> bool:
        """Ensure we have a valid authentication token, logging in at most once.

        The shared token is re-checked after acquiring the refresh lock: when many
        callers hit an empty cache at the same moment, the first performs the login
        and the rest adopt its token instead of each paying another ~700ms.
        """
        if self.auth_token:
            return True

        cached = self._cached_token()
        if cached:
            self.auth_token = cached
            return True

        async with _token_refresh_lock:
            cached = self._cached_token()
            if cached:
                self.auth_token = cached
                return True
            logger.info("No authentication token available. Performing async login.")
            return await self.login()

    async def _make_authenticated_request(
        self,
        method: str,
        url: str,
        json_data: dict | None = None,
        retry_count: int = 0,
        timeout_seconds: int = 30,
    ) -> tuple[int, str]:
        """
        Make an authenticated HTTP request with automatic retry on 401.

        Args:
            method: HTTP method (GET, POST, etc.)
            url: Request URL
            json_data: Optional JSON body
            retry_count: Current retry attempt (for 401 handling)
            timeout_seconds: Request timeout in seconds (default: 30)

        Returns:
            Tuple of (status_code, response_text)
        """
        if not await self._ensure_authenticated():
            return 401, "Authentication failed"

        ssl_context = await self._create_ssl_context()
        connector = aiohttp.TCPConnector(ssl=ssl_context)
        request_timeout = aiohttp.ClientTimeout(total=timeout_seconds)

        async with aiohttp.ClientSession(connector=connector, timeout=request_timeout) as session:
            headers = {"Authorization": f"Bearer {self.auth_token}", "Content-Type": "application/json"}
            logger.debug(f"Request headers: {redact_sensitive_headers(headers)}")
            logger.debug(f"Making {method} request to: {url}")

            async with session.request(method, url, json=json_data or {}, headers=headers) as response:
                response_text = await response.text()
                logger.debug(f"Response status: {response.status}")
                logger.debug(
                    f"Response text: {response_text[:200]}..."
                    if len(response_text) > 200
                    else f"Response text: {response_text}"
                )

                if response.status == 401 and retry_count == 0:
                    logger.warning("Received 401 Unauthorized. Attempting to re-login and retry.")
                    # Invalidate only the token this request actually used, then
                    # re-authenticate through the shared path so concurrent 401s
                    # collapse into a single login.
                    self._invalidate_token(self.auth_token)
                    self.auth_token = None
                    if await self._ensure_authenticated():
                        logger.info("Re-authentication successful, retrying request")
                        return await self._make_authenticated_request(method, url, json_data, retry_count + 1)
                    else:
                        logger.error("Re-authentication failed")
                        return 401, "Re-authentication failed"
                elif response.status == 401:
                    logger.error("Still receiving 401 after re-authentication attempt")
                    return 401, "Authentication failed after retry"

                return response.status, response_text

    async def sync_application(self, app_name: str | None = None) -> bool:
        """
        Trigger synchronization of an ArgoCD application.

        Args:
            app_name: Name of the application to sync. If None, uses default_app_name

        Returns:
            True if sync was triggered successfully, False otherwise
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
            Application status dictionary if successful, None if application doesn't exist (404)

        Raises:
            PermissionError: If access to the application is denied (403)
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
                # Expected transient right after creating an app/AppProject (ArgoCD RBAC
                # still propagating). Logged at debug; callers decide severity - retry loops
                # warn while retrying and only error if it never resolves.
                logger.debug(
                    f"Permission denied accessing application {app_name} - this is OK, the app may "
                    f"not exist yet / ArgoCD RBAC may still be propagating: {response_text}"
                )
                raise PermissionError(f"Permission denied accessing application '{app_name}'")
            else:
                logger.error(f"Status request failed with status {status_code}: {response_text}")
                raise RuntimeError(f"Failed to get application status: HTTP {status_code}")

        except PermissionError:
            raise
        except RuntimeError:
            raise
        except Exception as e:
            logger.error(f"Error getting application status: {e}")
            raise RuntimeError(f"Error getting application status: {e}")

    async def get_application_resource_tree(self, app_name: str | None = None) -> list[dict[str, Any]]:
        """
        Get the resource tree for an ArgoCD application.

        The resource tree includes child resources (Pods, ReplicaSets) with their
        health status and messages, which provides more detail than the top-level
        application status (e.g., image pull errors on Pods).

        Args:
            app_name: Name of the application. If None, uses default_app_name

        Returns:
            List of resource tree nodes, or empty list on error
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
            logger.error(f"Error getting resource tree for {app_name}: {e}")
            return []

    async def list_applications(self) -> list[dict[str, Any]]:
        """
        List all ArgoCD applications.

        Returns:
            List of application dictionaries if successful, empty list otherwise
        """
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
            logger.error(f"Error listing applications: {e}")
            return []

    async def login_and_sync(self, app_name: str | None = None) -> str | None:
        """
        Convenience method to login and refresh an application in one call.

        Args:
            app_name: Name of the application to refresh. If None, uses default_app_name

        Returns:
            The ``reconciledAt`` timestamp on success, ``None`` on failure.
        """
        # This method is now redundant since refresh_application handles authentication automatically
        return await self.refresh_application(app_name)

    async def refresh_application(self, app_name: str | None = None, hard_refresh: bool = False) -> str | None:
        """
        Refresh an ArgoCD application.

        Args:
            app_name: Name of the application to refresh. If None, uses default_app_name
            hard_refresh: If True, performs a hard refresh (clears manifest cache, slower).
                         If False, performs a soft refresh (only checks for source changes, faster).
                         Default is False (soft refresh) for better performance.

        Returns:
            The ``reconciledAt`` timestamp from the response on success, or
            ``None`` on failure.  Callers can pass this value to
            ``wait_for_application_synced`` so it knows when the status is
            fresh.
        """
        app_name = app_name or self.default_app_name
        refresh_type = "hard" if hard_refresh else "normal"
        logger.info(f"Triggering {refresh_type} refresh for application: {app_name}")

        # Use GET request with refresh query parameter (correct ArgoCD API usage)
        # Soft refresh: refresh=normal (checks for source changes only, faster)
        # Hard refresh: refresh=hard (clears cache, forces re-render, slower)
        refresh_param = "hard" if hard_refresh else "normal"
        refresh_url = f"{self._actual_base_url}/api/v1/applications/{app_name}?refresh={refresh_param}"

        try:
            status_code, response_text = await self._make_authenticated_request("GET", refresh_url, timeout_seconds=120)

            if status_code == 200:
                logger.info(f"Successfully triggered {refresh_type} refresh for application: {app_name}")
                response_data = json.loads(response_text)
                status = response_data.get("status", {}) or {}
                # Surface a render/compare error the moment it appears, at the source. ArgoCD
                # sets an *Error condition (e.g. ComparisonError) when it cannot generate or
                # compare the manifests; logging it here puts the real cause in the OPI logs
                # instead of only in the ArgoCD UI.
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
            # Some transport errors stringify to "" — fall back to the type name so
            # the line is never a bare "... refresh:" with no reason.
            reason = str(e) or repr(e)
            logger.error(f"Error during {refresh_type} refresh of application '{app_name}': {reason}")
            return None

    async def hard_refresh_application(self, app_name: str | None = None) -> str | None:
        """
        Convenience method to perform a hard refresh (clears cache, forces re-render).

        Use sparingly as it's resource-intensive and can slow down ArgoCD.
        For most use cases, the default soft refresh is sufficient and faster.

        Args:
            app_name: Name of the application to refresh. If None, uses default_app_name

        Returns:
            The ``reconciledAt`` timestamp on success, ``None`` on failure.
        """
        return await self.refresh_application(app_name, hard_refresh=True)

    async def application_exists(self, app_name: str) -> bool:
        """
        Check if an ArgoCD application exists.

        Args:
            app_name: Name of the application to check

        Returns:
            True if application exists, False if it doesn't exist (404)

        Raises:
            PermissionError: If access to the application is denied (403)
            RuntimeError: If an unexpected error occurs
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
    ) -> bool:
        """
        Wait for an ArgoCD application to be fully deleted.

        ArgoCD is queried first - it is, and ought to remain, our primary source of
        truth. But its API has proven untrustworthy under control-plane stress: it
        returns 'permission denied' to an admin caller for applications that still
        exist, conflating "gone", "can't see it", and "I'm stalled". So we never treat
        that response as "deleted". When ArgoCD's answer is anything but a confident
        "still exists", we double-check against the Kubernetes API, which fails
        honestly (the object, a clean NotFound, or a distinguishable error).

        Args:
            app_name: Name of the application to wait for
            max_retries: Maximum number of retries
            retry_delay: Delay between retries in seconds
            kubectl_connector: Connector used to confirm absence against the Kubernetes
                API when ArgoCD is ambiguous. Without it, an ambiguous ArgoCD answer
                cannot be confirmed and deletion is reported as unconfirmed (False).
            namespace: Namespace holding the ArgoCD Application CR; defaults to this instance's cluster

        Returns:
            True only if the application is confirmed deleted, False otherwise.
        """
        import asyncio

        logger.info(f"Waiting for application deletion: {app_name} (max {max_retries} retries)")

        async def _confirmed_gone_via_k8s() -> bool:
            # Ground-truth fallback. Only a clean NotFound (False) counts as gone;
            # still-present (True) or unknown (None) must not be read as deleted.
            if kubectl_connector is None:
                return False
            return (await kubectl_connector.argocd_application_exists(app_name, namespace)) is False

        for attempt in range(max_retries):
            try:
                exists = await self.application_exists(app_name)
                # ArgoCD reports the app gone. A clean 404 is fairly reliable, but since
                # the same API lies under stress we still confirm via the Kubernetes API
                # when we can before declaring success.
                if not exists and (kubectl_connector is None or await _confirmed_gone_via_k8s()):
                    logger.info(f"Application {app_name} confirmed deleted after {attempt + 1} checks")
                    return True

                logger.debug(f"Application {app_name} still exists, retry {attempt + 1}/{max_retries}")

            except PermissionError:
                # FALLBACK: 'permission denied' is NOT proof the app is gone. The ArgoCD
                # API has proven it cannot be trusted here - it returns this to an admin
                # while merely stalled, for apps that still exist. Until ArgoCD can be
                # trusted again (and it really ought to be our single source of truth,
                # and may be in the future), we double-check the Kubernetes API directly.
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
                logger.error(f"Error checking application deletion status: {e}")

            if attempt < max_retries - 1:  # Don't sleep on the last attempt
                await asyncio.sleep(retry_delay)

        logger.warning(f"Application {app_name} NOT confirmed deleted after {max_retries} retries")
        return False


def create_argo_connector(
    server_host: str | None = None,
    server_port: int | None = None,
    username: str | None = None,
    password: str | None = None,
    use_tls: bool | None = None,
    verify_ssl: bool | None = None,
) -> ArgoConnector:
    """
    Create and return an ArgoConnector instance.

    Uses configuration values from settings if parameters are not provided.

    Args:
        server_host: ArgoCD server hostname or service name (defaults to config)
        server_port: ArgoCD server port (defaults to config)
        username: Username for authentication (defaults to config)
        password: Password for authentication (defaults to config)
        use_tls: Whether to use TLS/HTTPS (defaults to config)
        verify_ssl: Whether to verify SSL certificates (defaults to config)

    Returns:
        ArgoConnector instance
    """
    from opi.core.config import settings

    # Use settings as defaults if parameters are not provided
    final_server_host = server_host if server_host is not None else settings.ARGOCD_HOST
    final_server_port = server_port if server_port is not None else settings.ARGOCD_PORT
    final_username = username if username is not None else settings.ARGOCD_USERNAME
    final_password = password if password is not None else settings.ARGOCD_PASSWORD
    final_use_tls = use_tls if use_tls is not None else settings.ARGOCD_USE_TLS
    final_verify_ssl = verify_ssl if verify_ssl is not None else settings.ARGOCD_VERIFY_SSL

    logger.debug(f"Creating ArgoConnector for server: {final_server_host}:{final_server_port}")
    return ArgoConnector(
        server_host=final_server_host,
        server_port=final_server_port,
        username=final_username,
        password=final_password,
        use_tls=final_use_tls,
        verify_ssl=final_verify_ssl,
    )
