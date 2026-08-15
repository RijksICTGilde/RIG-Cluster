"""Connector for the platform SMTP relay's management API.

The ONE place OPI talks to the relay. Everything above it (``MailManager``, the
``send-email`` service) speaks in accounts and limits and never in HTTP.

The relay is Stalwart (see ``plans/mailrelay.md`` for why): its management API models an
SMTP account as a *principal*, so creating a project's account is one POST, changing its
limit is one PATCH and removing it is one DELETE. That is the whole reason the product was
chosen over Postfix -- the alternative is writing account files and reloading a daemon,
which is provisioning by side effect.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import aiohttp

from opi.core.config import settings
from opi.utils.age import decrypt_password_smart_auto

logger = logging.getLogger(__name__)


class MailRelayError(RuntimeError):
    """The relay refused or could not answer a management call."""


class MailRelayNotConfiguredError(MailRelayError):
    """No relay is configured for this cluster.

    Raised rather than silently skipped: an account that was never created hands a
    deployment credentials that authenticate nowhere, and the application only finds
    out when it tries to send.
    """


@dataclass(frozen=True)
class MailAccount:
    """One SMTP account on the relay, as the relay holds it."""

    #: The SASL username the application authenticates with.
    username: str
    #: The address the relay pins ``MAIL FROM`` and ``From:`` to for this account.
    from_address: str
    #: Where bounces for this account come back to; carries the account name so a
    #: returned message is traceable to one project.
    bounce_address: str
    #: Daily budget agreed for this account. Recorded here and in the project file; the
    #: relay enforces one ceiling for every account (see the limiter in its configmap),
    #: because a principal in Stalwart v0.11 carries no limit of its own.
    messages_per_day: int


class MailConnector:
    """Talks to the relay's management API over HTTP."""

    def __init__(self, base_url: str, username: str, password: str, verify_tls: bool = True) -> None:
        self.base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._verify_tls = verify_tls

    async def _request(
        self,
        method: str,
        path: str,
        payload: Any | None = None,
        allow_missing: bool = False,
    ) -> dict[str, Any] | None:
        """One management call. ``allow_missing`` turns a 404 into ``None``."""
        url = f"{self.base_url}{path}"
        auth = aiohttp.BasicAuth(self._username, self._password)
        connector = aiohttp.TCPConnector(ssl=self._verify_tls)
        async with (
            aiohttp.ClientSession(connector=connector) as session,
            session.request(method, url, json=payload, auth=auth) as response,
        ):
            if response.status == 404 and allow_missing:
                return None
            body = await response.text()
            if response.status >= 400:
                # Afgekapt: het foutlichaam van de relay kan een heel antwoord zijn en de
                # tekst komt in de logregel terecht. De eerste regel zegt wat er mis is.
                detail = body.strip().splitlines()[0][:200] if body.strip() else ""
                raise MailRelayError(f"{method} {path} gaf {response.status}: {detail}")
            if not body:
                return {}
            result = await response.json(content_type=None)
            if not isinstance(result, dict):
                return result
            # Een onbekend account is bij Stalwart GEEN 404: het antwoord is 200 met
            # {"error": "notFound", "item": "<naam>"} in het lichaam (gemeten tegen
            # v0.11.8). Wie alleen op de statuscode kijkt, leest dat als een bestaand
            # account en gaat bijwerken in plaats van aanmaken -- dan wordt het account
            # nooit gemaakt en authenticeert de applicatie nergens.
            error = result.get("error")
            if error is None:
                return result
            if error == "notFound" and allow_missing:
                return None
            raise MailRelayError(f"{method} {path} gaf fout {error}: {str(result.get('details') or result)[:200]}")

    async def get_principal(self, name: str) -> dict[str, Any] | None:
        """The account as the relay holds it, or ``None`` if it does not exist."""
        result = await self._request("GET", f"/api/principal/{name}", allow_missing=True)
        if result is None:
            return None
        # Stalwart wraps a successful read in {"data": {...}}.
        data = result.get("data") if isinstance(result, dict) else None
        return data if isinstance(data, dict) else result

    async def create_principal(
        self,
        name: str,
        password: str,
        from_address: str,
        bounce_address: str,
    ) -> None:
        """Create the account. The relay refuses a duplicate; the manager checks first.

        Both addresses go on the account, and that is not cosmetic. The relay rewrites the
        envelope to the bounce address and only THEN checks ``must-match-sender``, so an
        account that does not own its own bounce address cannot hand in a single message
        ("501 5.5.4 You are not allowed to send from this address", measured).

        There is deliberately no daily limit here: a principal in Stalwart v0.11 has no
        such field (the API answers "JSON deserialization failed" on one), so the limit
        lives in the relay's own configuration, keyed on the authenticated account.
        """
        await self._request(
            "POST",
            "/api/principal",
            payload={
                "type": "individual",
                "name": name,
                "secrets": [password],
                "emails": [from_address, bounce_address],
                # Without a role the account authenticates and is then refused with "550
                # 5.7.1 Your account is not authorized to use this service" (measured):
                # enabledPermissions alone grants nothing to build on.
                "roles": ["user"],
                # Submission only: this account may hand mail in, never read a mailbox.
                "enabledPermissions": ["email-send"],
                "description": f"ZAD send-email account voor {name}",
                "quota": 0,
            },
        )
        logger.info(f"Mailaccount {name} aangemaakt op de relay")

    async def update_principal(
        self,
        name: str,
        password: str | None = None,
        from_address: str | None = None,
        bounce_address: str | None = None,
    ) -> None:
        """Bring an existing account in line with what the project asks for."""
        changes: list[dict[str, Any]] = []
        if password is not None:
            changes.append({"action": "set", "field": "secrets", "value": [password]})
        if from_address is not None and bounce_address is not None:
            changes.append({"action": "set", "field": "emails", "value": [from_address, bounce_address]})
        if not changes:
            return
        await self._request("PATCH", f"/api/principal/{name}", payload=changes)
        logger.info(f"Mailaccount {name} bijgewerkt op de relay")

    async def delete_principal(self, name: str) -> bool:
        """Remove the account. Returns False when it was already gone (replay-safe)."""
        result = await self._request("DELETE", f"/api/principal/{name}", allow_missing=True)
        if result is None:
            logger.info(f"Mailaccount {name} bestond al niet meer op de relay")
            return False
        logger.info(f"Mailaccount {name} verwijderd van de relay")
        return True


async def create_mail_connector() -> MailConnector:
    """The configured relay connector, or a refusal when no relay is configured.

    Refusing here rather than at the first send keeps the failure where someone is
    looking: provisioning a project fails visibly instead of the project quietly
    receiving SMTP credentials nothing accepts.
    """
    if not settings.MAIL_RELAY_API_URL:
        raise MailRelayNotConfiguredError(
            "MAIL_RELAY_API_URL is niet ingesteld: er draait geen mailrelay op dit cluster, "
            "dus de dienst send-email kan geen account aanmaken."
        )
    password = await decrypt_password_smart_auto(settings.MAIL_RELAY_ADMIN_PASSWORD)
    return MailConnector(
        base_url=settings.MAIL_RELAY_API_URL,
        username=settings.MAIL_RELAY_ADMIN_USERNAME,
        password=password,
        verify_tls=settings.MAIL_RELAY_VERIFY_TLS,
    )
