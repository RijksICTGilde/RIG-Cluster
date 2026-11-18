"""TransIP REST API v6 connector for DNS administration.

Authenticate, list the domains this account holds, read a zone, and add or delete a
record. Two ways to authenticate: an account name plus a private key that signs the
``/auth`` call, or a ready-made access token (:meth:`TransIPConnector.with_access_token`).

The API key is IP-whitelisted at TransIP, so these calls only succeed from a
whitelisted source (in our setup: a pod in the production cluster, the same
egress IP external-dns already uses).
"""

import base64
import json
import logging
import secrets
from typing import Any

import aiohttp
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

from opi.connectors.http import client_session

logger = logging.getLogger(__name__)

TRANSIP_API_BASE = "https://api.transip.nl/v6"

#: Budget for one TransIP call. The API answers in well under a second; this is the
#: ceiling that keeps a startup reconciliation from waiting on a silent endpoint.
TRANSIP_TIMEOUT_SECONDS = 30.0


class TransIPError(Exception):
    """A TransIP API call failed."""


class TransIPAuthError(TransIPError):
    """TransIP refused the credential: the token expired, or this source is not whitelisted.

    A type of its own so a caller can name that cause instead of the status code. The
    credential itself never reaches the message.
    """


class TransIPConnector:
    """Minimal client for the TransIP REST API v6."""

    def __init__(self, account: str, private_key_pem: str, base_url: str = TRANSIP_API_BASE) -> None:
        self.account = account
        self.private_key_pem = private_key_pem
        self.base_url = base_url.rstrip("/")
        self._token: str | None = None

    @classmethod
    def with_access_token(cls, access_token: str, base_url: str = TRANSIP_API_BASE) -> TransIPConnector:
        """A connector that carries a ready-made access token instead of signing in with a key.

        For an account whose token is handed to us (the fieldlab zone): there is no private
        key to sign the ``/auth`` call with, so that call never happens.
        """
        connector = cls("", "", base_url=base_url)
        connector._token = access_token
        return connector

    async def _get_token(self) -> str:
        """Fetch (and cache) an access token.

        The signature covers the exact bytes of the body, so the body is built once and both
        signed and sent: serializing it twice yields other bytes and TransIP rejects the
        signature.
        """
        if self._token:
            return self._token

        body = json.dumps(
            {
                "login": self.account,
                "nonce": secrets.token_hex(16),
                "read_only": False,
                "expiration_time": "30 minutes",
                "label": f"opi-caa-{secrets.token_hex(4)}",
                "global_key": True,
            },
            separators=(",", ":"),
        ).encode()

        private_key = serialization.load_pem_private_key(self.private_key_pem.encode(), password=None)
        if not isinstance(private_key, RSAPrivateKey):
            msg = "TransIP authentication requires an RSA private key"
            raise TransIPError(msg)
        signature = private_key.sign(body, padding.PKCS1v15(), hashes.SHA512())

        try:
            async with (
                client_session(TRANSIP_TIMEOUT_SECONDS) as session,
                session.post(
                    f"{self.base_url}/auth",
                    data=body,
                    headers={
                        "Content-Type": "application/json",
                        "Signature": base64.b64encode(signature).decode(),
                    },
                ) as response,
            ):
                text = await response.text()
                status = response.status
        except (aiohttp.ClientError, TimeoutError) as e:
            msg = f"TransIP auth is not reachable: {str(e) or type(e).__name__}"
            raise TransIPError(msg) from e
        if status >= 400:
            msg = f"TransIP auth failed: {status} {text}"
            raise TransIPError(msg)
        try:
            token = json.loads(text)["token"]
        except (json.JSONDecodeError, KeyError) as e:
            msg = f"TransIP auth returned no token: {text}"
            raise TransIPError(msg) from e

        self._token = token
        return token

    async def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        token = await self._get_token()
        headers = {"Authorization": f"Bearer {token}"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"

        try:
            async with (
                client_session(TRANSIP_TIMEOUT_SECONDS) as session,
                session.request(method, f"{self.base_url}{path}", data=data, headers=headers) as response,
            ):
                text = await response.text()
                status = response.status
        except (aiohttp.ClientError, TimeoutError) as e:
            msg = f"TransIP {method} {path} is not reachable: {str(e) or type(e).__name__}"
            raise TransIPError(msg) from e
        if status in (401, 403):
            msg = f"TransIP refused the credential on {method} {path}: {status} {text}"
            raise TransIPAuthError(msg)
        if status >= 400:
            msg = f"TransIP {method} {path} failed: {status} {text}"
            raise TransIPError(msg)
        if not text:
            return {}
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            msg = f"TransIP {method} {path} returned no JSON: {text}"
            raise TransIPError(msg) from e

    async def list_domains(self) -> list[str]:
        """Domain names this account holds."""
        result = await self._request("GET", "/domains")
        return [domain["name"] for domain in result.get("domains", [])]

    async def get_dns_entries(self, zone: str) -> list[dict[str, Any]]:
        """All DNS entries in a zone, as TransIP returns them."""
        result = await self._request("GET", f"/domains/{zone}/dns")
        return list(result.get("dnsEntries", []))

    async def add_dns_entry(self, zone: str, name: str, record_type: str, content: str, ttl: int) -> None:
        """Add a single DNS entry to a zone."""
        logger.info(f"Adding {record_type} record '{name}' to zone {zone}: {content}")
        await self._request(
            "POST",
            f"/domains/{zone}/dns",
            {"dnsEntry": {"name": name, "expire": ttl, "type": record_type, "content": content}},
        )

    async def delete_dns_entry(self, zone: str, name: str, record_type: str, content: str, ttl: int) -> None:
        """Delete a single DNS entry from a zone.

        TransIP identifies the entry by its whole body, so the content and the TTL have to
        match the record as the zone holds it; read it with :meth:`get_dns_entries` first.
        """
        logger.info(f"Deleting {record_type} record '{name}' from zone {zone}: {content}")
        await self._request(
            "DELETE",
            f"/domains/{zone}/dns",
            {"dnsEntry": {"name": name, "expire": ttl, "type": record_type, "content": content}},
        )
