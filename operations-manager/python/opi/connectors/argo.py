"""
ArgoCD connector for managing ArgoCD applications and authentication.

This module provides functionality to authenticate with ArgoCD and manage applications,
including triggering synchronization of applications.
"""

import json
import logging
import ssl
from typing import Any

import aiohttp
import requests

logger = logging.getLogger(__name__)


class ArgoConnector:
    """Connector for interacting with ArgoCD server."""

    def __init__(
        self,
        server_host: str = "argocd-server",
        server_port: int = 80,
        username: str = "admin",
        password: str = "admin",
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

        # Try to perform initial login during initialization
        # If this fails, async methods will handle re-authentication
        try:
            self._perform_login()
        except Exception as e:
            logger.warning(f"Initial login failed during initialization: {e}")
            logger.info("Will attempt async login when methods are called")

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

            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.post(
                    login_url, json=login_data, headers={"Content-Type": "application/json"}
                ) as response:
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
        """Ensure we have a valid authentication token."""
        if not self.auth_token:
            logger.info("No authentication token available. Performing async login.")
            return await self.login()
        return True

    async def _make_authenticated_request(
        self, method: str, url: str, json_data: dict | None = None, retry_count: int = 0
    ) -> tuple[int, str]:
        """
        Make an authenticated HTTP request with automatic retry on 401.

        Returns:
            Tuple of (status_code, response_text)
        """
        if not await self._ensure_authenticated():
            return 401, "Authentication failed"

        ssl_context = await self._create_ssl_context()
        connector = aiohttp.TCPConnector(ssl=ssl_context)

        async with aiohttp.ClientSession(connector=connector) as session:
            headers = {"Authorization": f"Bearer {self.auth_token}", "Content-Type": "application/json"}
            logger.debug(f"Request headers: {headers}")
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
                    # Clear the current token and re-authenticate
                    self.auth_token = None
                    if await self.login():
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
            logger.error(f"Error during application sync: {e}")
            return False

    async def get_application_status(self, app_name: str | None = None) -> dict[str, Any] | None:
        """
        Get the status of an ArgoCD application.

        Args:
            app_name: Name of the application. If None, uses default_app_name

        Returns:
            Application status dictionary if successful, None otherwise
        """
        app_name = app_name or self.default_app_name
        logger.info(f"Getting status for application: {app_name}")

        status_url = f"{self._actual_base_url}/api/v1/applications/{app_name}"

        try:
            status_code, response_text = await self._make_authenticated_request("GET", status_url)

            if status_code == 200:
                status_data = json.loads(response_text)
                logger.info(f"Successfully retrieved status for application: {app_name}")
                return status_data
            else:
                logger.error(f"Status request failed with status {status_code}: {response_text}")
                return None

        except Exception as e:
            logger.error(f"Error getting application status: {e}")
            return None

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

    async def login_and_sync(self, app_name: str | None = None) -> bool:
        """
        Convenience method to login and refresh an application in one call.

        Args:
            app_name: Name of the application to refresh. If None, uses default_app_name

        Returns:
            True if login and refresh both successful, False otherwise
        """
        # This method is now redundant since refresh_application handles authentication automatically
        return await self.refresh_application(app_name)

    async def refresh_application(self, app_name: str | None = None, hard_refresh: bool = False) -> bool:
        """
        Refresh an ArgoCD application.

        Args:
            app_name: Name of the application to refresh. If None, uses default_app_name
            hard_refresh: If True, performs a hard refresh (clears manifest cache, slower).
                         If False, performs a soft refresh (only checks for source changes, faster).
                         Default is False (soft refresh) for better performance.

        Returns:
            True if refresh was triggered successfully, False otherwise
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
            status_code, response_text = await self._make_authenticated_request("GET", refresh_url)

            if status_code == 200:
                logger.info(f"Successfully triggered {refresh_type} refresh for application: {app_name}")
                return True
            else:
                logger.error(f"{refresh_type.title()} refresh failed with status {status_code}: {response_text}")
                return False

        except Exception as e:
            logger.error(f"Error during application {refresh_type} refresh: {e}")
            return False

    async def hard_refresh_application(self, app_name: str | None = None) -> bool:
        """
        Convenience method to perform a hard refresh (clears cache, forces re-render).

        Use sparingly as it's resource-intensive and can slow down ArgoCD.
        For most use cases, the default soft refresh is sufficient and faster.

        Args:
            app_name: Name of the application to refresh. If None, uses default_app_name

        Returns:
            True if hard refresh was triggered successfully, False otherwise
        """
        return await self.refresh_application(app_name, hard_refresh=True)

    async def application_exists(self, app_name: str) -> bool:
        """
        Check if an ArgoCD application exists.

        Args:
            app_name: Name of the application to check

        Returns:
            True if application exists, False otherwise
        """
        logger.debug(f"Checking if application exists: {app_name}")

        try:
            status_data = await self.get_application_status(app_name)
            exists = status_data is not None
            logger.debug(f"Application {app_name} exists: {exists}")
            return exists
        except Exception as e:
            logger.error(f"Error checking if application exists: {e}")
            return False

    async def wait_for_application_deletion(self, app_name: str, max_retries: int = 5, retry_delay: int = 3) -> bool:
        """
        Wait for an ArgoCD application to be fully deleted.

        Args:
            app_name: Name of the application to wait for
            max_retries: Maximum number of retries
            retry_delay: Delay between retries in seconds

        Returns:
            True if application was deleted, False if it still exists after max retries
        """
        import asyncio

        logger.info(f"Waiting for application deletion: {app_name} (max {max_retries} retries)")

        for attempt in range(max_retries):
            try:
                exists = await self.application_exists(app_name)
                if not exists:
                    logger.info(f"Application {app_name} successfully deleted after {attempt + 1} checks")
                    return True

                logger.debug(f"Application {app_name} still exists, retry {attempt + 1}/{max_retries}")
                if attempt < max_retries - 1:  # Don't sleep on the last attempt
                    await asyncio.sleep(retry_delay)

            except Exception as e:
                logger.error(f"Error checking application deletion status: {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(retry_delay)

        logger.warning(f"Application {app_name} still exists after {max_retries} retries")
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
