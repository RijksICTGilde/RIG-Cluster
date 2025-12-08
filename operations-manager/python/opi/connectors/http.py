"""
HTTP connector for bootstrap API actions.

This connector provides HTTP client functionality with support for various
authentication methods including Keycloak OAuth2, Basic Auth, and Bearer tokens.
"""

import json
import logging
from enum import Enum
from typing import Any

import aiohttp

from opi.utils.age import decrypt_password_smart_auto

logger = logging.getLogger(__name__)


class AuthType(Enum):
    """Authentication types supported by HTTP connector."""

    NONE = "none"
    BASIC = "basic"
    BEARER = "bearer"
    KEYCLOAK_PASSWORD = "keycloak-password"  # Resource Owner Password Credentials
    KEYCLOAK_CLIENT = "keycloak-client"  # Client Credentials


class HttpConnector:
    """HTTP connector with authentication support for bootstrap API actions."""

    def __init__(self, base_url: str, auth_config: dict[str, Any] | None = None):
        """
        Initialize HTTP connector.

        Args:
            base_url: Base URL for API requests
            auth_config: Authentication configuration dictionary
        """
        self.base_url = base_url.rstrip("/")
        self.auth_config = auth_config or {"type": "none"}
        self.session: aiohttp.ClientSession | None = None
        self._access_token: str | None = None

    async def __aenter__(self):
        """Async context manager entry."""
        self.session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        if self.session:
            await self.session.close()

    async def request(
        self,
        method: str,
        path: str,
        headers: dict[str, str] | None = None,
        body: str | dict | None = None,
    ) -> tuple[int, dict[str, Any] | str]:
        """
        Execute HTTP request with authentication.

        Args:
            method: HTTP method (GET, POST, PUT, DELETE, etc.)
            path: Request path (will be appended to base_url)
            headers: Optional request headers
            body: Optional request body (string or dict)

        Returns:
            Tuple of (status_code, response_data)
        """
        if not self.session:
            msg = "HTTP connector session not initialized. Use async context manager."
            raise RuntimeError(msg)

        url = f"{self.base_url}{path}"
        headers = headers or {}

        # Add authentication headers
        await self._add_auth_headers(headers)

        # Prepare body
        request_body = None
        if body is not None:
            if isinstance(body, dict):
                request_body = json.dumps(body)
                if "Content-Type" not in headers:
                    headers["Content-Type"] = "application/json"
            else:
                request_body = body

        logger.info(f"HTTP {method} {url}")
        logger.debug(f"Headers: {self._sanitize_headers_for_logging(headers)}")

        async with self.session.request(method, url, headers=headers, data=request_body) as response:
            status = response.status
            logger.info(f"Response status: {status}")

            # Try to parse as JSON
            try:
                data = await response.json()
                return status, data
            except (aiohttp.ContentTypeError, json.JSONDecodeError):
                # Fall back to text
                text = await response.text()
                return status, text

    async def _add_auth_headers(self, headers: dict[str, str]) -> None:
        """
        Add authentication headers based on auth configuration.

        Args:
            headers: Headers dictionary to modify
        """
        auth_type = AuthType(self.auth_config.get("type", "none"))

        if auth_type == AuthType.NONE:
            return

        if auth_type == AuthType.BASIC:
            await self._add_basic_auth(headers)
        elif auth_type == AuthType.BEARER:
            await self._add_bearer_auth(headers)
        elif auth_type in (AuthType.KEYCLOAK_PASSWORD, AuthType.KEYCLOAK_CLIENT):
            await self._add_keycloak_auth(headers)

    async def _add_basic_auth(self, headers: dict[str, str]) -> None:
        """Add Basic authentication header."""
        import base64

        username = self.auth_config.get("username", "")
        password = self.auth_config.get("password", "")

        # Auto-decrypt password if encrypted
        password = await decrypt_password_smart_auto(password)

        credentials = base64.b64encode(f"{username}:{password}".encode()).decode()
        headers["Authorization"] = f"Basic {credentials}"

    async def _add_bearer_auth(self, headers: dict[str, str]) -> None:
        """Add Bearer token authentication header."""
        token = self.auth_config.get("token", "")

        # Auto-decrypt token if encrypted
        token = await decrypt_password_smart_auto(token)

        headers["Authorization"] = f"Bearer {token}"

    async def _add_keycloak_auth(self, headers: dict[str, str]) -> None:
        """Add Keycloak OAuth2 authentication header."""
        # Get or refresh access token
        if not self._access_token:
            await self._fetch_keycloak_token()

        if self._access_token:
            headers["Authorization"] = f"Bearer {self._access_token}"

    async def _fetch_keycloak_token(self) -> None:
        """Fetch access token from Keycloak."""
        if not self.session:
            msg = "HTTP connector session not initialized"
            raise RuntimeError(msg)

        token_url = self.auth_config.get("token_url")
        if not token_url:
            msg = "token_url required for Keycloak authentication"
            raise ValueError(msg)

        auth_type = AuthType(self.auth_config.get("type"))

        if auth_type == AuthType.KEYCLOAK_PASSWORD:
            # Resource Owner Password Credentials flow
            client_id = self.auth_config.get("client_id")
            username = self.auth_config.get("username")
            password = self.auth_config.get("password")

            if not all([client_id, username, password]):
                msg = "client_id, username, and password required for keycloak-password auth"
                raise ValueError(msg)

            # Auto-decrypt password
            password = await decrypt_password_smart_auto(password)

            data = {
                "grant_type": "password",
                "client_id": client_id,
                "username": username,
                "password": password,
            }

        elif auth_type == AuthType.KEYCLOAK_CLIENT:
            # Client Credentials flow
            client_id = self.auth_config.get("client_id")
            client_secret = self.auth_config.get("client_secret")

            if not all([client_id, client_secret]):
                msg = "client_id and client_secret required for keycloak-client auth"
                raise ValueError(msg)

            # Auto-decrypt client_secret
            client_secret = await decrypt_password_smart_auto(client_secret)

            data = {
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
            }
        else:
            msg = f"Unsupported auth type: {auth_type}"
            raise ValueError(msg)

        logger.info(f"Fetching Keycloak token from {token_url}")

        async with self.session.post(
            token_url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"}
        ) as response:
            if response.status != 200:
                text = await response.text()
                msg = f"Failed to fetch Keycloak token: {response.status} - {text}"
                raise RuntimeError(msg)

            token_data = await response.json()
            self._access_token = token_data.get("access_token")

            if not self._access_token:
                msg = "No access_token in Keycloak response"
                raise RuntimeError(msg)

            logger.info("Successfully obtained Keycloak access token")

    @staticmethod
    def _sanitize_headers_for_logging(headers: dict[str, str]) -> dict[str, str]:
        """Sanitize headers for logging by masking sensitive values."""
        sanitized = headers.copy()
        for key in sanitized:
            if key.lower() in ("authorization", "x-api-key", "api-key"):
                sanitized[key] = "***REDACTED***"
        return sanitized
