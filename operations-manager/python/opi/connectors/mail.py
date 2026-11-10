"""Connector for the platform SMTP relay's management API.

The ONE place OPI talks to the relay. Everything above it (``MailManager``, the
``send-email`` service) speaks in accounts and limits and never in HTTP.

The relay is Stalwart: its management API models an SMTP account as a *principal*, so
creating a project's account is one POST, changing its limit is one PATCH and removing it is
one DELETE.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any

import aiohttp

from opi.connectors.http import client_session
from opi.core.config import settings
from opi.utils.age import decrypt_password_smart_auto
from opi.utils.connect_retry import retry_on_connect_error
from opi.utils.naming import mail_sender_address_error

logger = logging.getLogger(__name__)

#: How long a management call may keep trying to connect. A pod swap of the relay leaves the
#: Service without an endpoint for a moment, and without this margin that costs a whole
#: rollout. Same budget and same helper as the ArgoCD connector.
RELAY_CONNECT_RETRY_SECONDS = 30.0

#: A readiness check may not hang in a slow relay: the outcome of a check that takes longer
#: than the tick itself is no longer current anyway.
RELAY_READY_TIMEOUT_SECONDS = 5.0

#: Budget for one management call. Generous: the relay writes its account store to disk.
#: Without it aiohttp waits out its own 300 seconds.
RELAY_REQUEST_TIMEOUT_SECONDS = 30.0


class MailRelayError(RuntimeError):
    """The relay refused or could not answer a management call."""


class MailRelayUnavailableError(MailRelayError):
    """The relay was unreachable: a platform outage, not a project's.

    Task handlers that report a rollout with ``processing_failed`` set
    ``error_category: InternalError``, so the user knows there is nothing to repair.
    """


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
    #: Daily budget for this account; enforced by the relay via ``set_rate_limit``.
    messages_per_day: int


#: Settings prefix under which each account's display name is stored, one key per
#: account. This is the DATA: OPI reads it back to see what it wrote, and writes a key only
#: when it changes.
MAIL_SENDER_NAME_PREFIX = "zad.afzender.naam"

#: Settings prefix for each account's sender address override. The exception and not the
#: rule: an account derives its address from its name, and a key here exists only for an
#: account that has a different one (Keycloak's, and a project whose requested address a
#: platform administrator approved). A second series and not a second field on the same key,
#: because relay settings are flat: one key, one value. Both are rendered into the same script.
MAIL_SENDER_ADDRESS_PREFIX = "zad.afzender.adres"

#: Settings prefix under which each account's PROJECT is stored, one key per account.
#:
#: An account belongs to a DEPLOYMENT and its name carries a hash, so the project is not
#: readable from it, and the relay cannot look anything up per account while a message is
#: being accepted. So OPI writes it here, and the identity rules read it to compose the
#: ``X-ZAD-Project`` header. An account without a key gets no header, which is exactly right
#: for the platform accounts: they send on nobody's behalf.
MAIL_ACCOUNT_PROJECT_PREFIX = "zad.project"

#: Settings prefix for each account's own rate limiter. ``queue.limiter.*`` is not a
#: local key, so the API can write it and ``/api/reload`` makes it take effect.
MAIL_LIMITER_PREFIX = "queue.limiter.inbound.zad"

#: The sieve script name. The script is a projection of ``MAIL_SENDER_NAME_PREFIX`` and
#: ``MAIL_SENDER_ADDRESS_PREFIX`` keys; lookup stores cannot be rewritten via the API,
#: but sieve scripts are recompiled on ``POST /api/reload``, so they stay current.
MAIL_SENDER_SCRIPT_NAME = "zad-afzenders"

#: The settings key that script is stored under. A constant because it is hardcoded in
#: ``mail/controller/base/config.toml``; drift would fail silently (``include :optional``
#: skips a missing script, so projects would send without a display name).
MAIL_SENDER_TABLE_KEY = f"sieve.trusted.scripts.{MAIL_SENDER_SCRIPT_NAME}.contents"

#: What an account name may look like before it is written into that generated script.
#: Belt and braces around a value that ``generate_mail_account_name`` already computes: it
#: ends up inside a sieve string literal, and this is the layer that would have to hold if it
#: ever stopped being computed.
#:
#: ``\A``/``\Z`` and not ``^``/``$``: ``$`` also matches just BEFORE a closing newline, so
#: ``"zad-keycloak\n"`` passes an anchored ``$`` pattern, and a newline is exactly the
#: character that ends the sieve string literal this value goes into. ``\Z`` is the end of
#: the string and nothing else.
_ACCOUNT_PATTERN = re.compile(r"\A[a-z0-9][a-z0-9-]*\Z")

#: Same for a display name. This list and ``FROM_NAME_PATTERN`` (the rule the form and the
#: API validate against) hold exactly the same characters, and they have to: a character only
#: THIS one refuses turns a name the form just approved into an exception halfway through
#: processing a project, where nothing catches it. Refusing them here as well means the
#: generated script cannot be broken open by a value that reached this connector some other
#: way. Note ``$``: a sieve string interpolates ``${...}``, so a name containing it would
#: read a variable instead of being text, which is why it belongs in both lists, and why
#: ``TestDeWeergavenaamWordtGetoetst`` runs its refused names past this layer too.
_NAME_FORBIDDEN = re.compile(r'[@<>"\\$\x00-\x1F\x7F]')


#: How many accounts one page of the principal listing asks for.
_PRINCIPAL_PAGE = 100


#: Serialises the read-modify-write of the generated table.
#:
#: Writing one account's name means rendering the table from ALL of them, so two projects
#: being processed at the same time would both read the table as it was, and the one that
#: writes last would drop the other one's name, silently, and until someone happens to
#: process that project again. OPI runs one replica per cluster, so a lock in the process is
#: the whole of the concurrency; a second replica would need the relay to offer a conditional
#: write, which v0.11.8 does not.
_TABLE_LOCK = asyncio.Lock()


class MailSenderNameError(ValueError):
    """A display name or account name that may not be written into the relay's script."""


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
        """One management call. ``allow_missing`` turns a 404 into ``None``.

        A relay that does not answer becomes a ``MailRelayUnavailableError`` here. Without
        that translation aiohttp's transport error escapes every caller that counts on
        ``MailRelayError``, and reaches the user as an unexplained failure on their own
        project.
        """

        async def _once() -> dict[str, Any] | None:
            return await self._single_request(method, path, payload, allow_missing)

        try:
            return await retry_on_connect_error(_once, RELAY_CONNECT_RETRY_SECONDS, f"{method} {path}")
        except (aiohttp.ClientError, TimeoutError, OSError) as error:
            raise MailRelayUnavailableError(
                "De mailservice van het platform is niet bereikbaar, dit ligt niet aan je project "
                f"({method} {path}: {error or type(error).__name__})"
            ) from error

    async def _single_request(
        self,
        method: str,
        path: str,
        payload: Any | None,
        allow_missing: bool,
    ) -> dict[str, Any] | None:
        """One management call, without retries and without translating transport errors."""
        url = f"{self.base_url}{path}"
        auth = aiohttp.BasicAuth(self._username, self._password)
        async with (
            client_session(RELAY_REQUEST_TIMEOUT_SECONDS, ssl=self._verify_tls) as session,
            session.request(method, url, json=payload, auth=auth) as response,
        ):
            if response.status == 404 and allow_missing:
                return None
            body = await response.text()
            if response.status >= 400:
                # Truncated: the relay's error body can be a whole response and it lands in
                # the log line. The first line says what is wrong.
                detail = body.strip().splitlines()[0][:200] if body.strip() else ""
                raise MailRelayError(f"{method} {path} gaf {response.status}: {detail}")
            if not body:
                return {}
            result = await response.json(content_type=None)
            if not isinstance(result, dict):
                return result
            # An unknown account is NOT a 404 on Stalwart: the answer is 200 with
            # {"error": "notFound", "item": "<name>"} in the body (measured against v0.11.8).
            # Looking only at the status code reads that as an existing account and updates
            # instead of creating, so the account is never made and the application
            # authenticates nowhere.
            error = result.get("error")
            if error is None:
                return result
            if error == "notFound" and allow_missing:
                return None
            raise MailRelayError(f"{method} {path} gaf fout {error}: {str(result.get('details') or result)[:200]}")

    async def is_ready(self) -> bool:
        """Whether the relay answers its own ``/healthz/ready`` with 200.

        The same entry as the pod's readiness probe: no credentials, no mail session, and so
        no tick in ``queue.limiter.inbound.platform``. Kept outside ``_request`` because the
        response body is not JSON and a retry is exactly what does not belong here: whoever
        measures whether the relay is there wants the answer of right now.
        """
        try:
            async with (
                client_session(RELAY_READY_TIMEOUT_SECONDS, ssl=self._verify_tls) as session,
                session.get(f"{self.base_url}/healthz/ready") as response,
            ):
                return response.status == 200
        except (aiohttp.ClientError, TimeoutError, OSError) as error:
            logger.warning(f"Mailrelay antwoordt niet op /healthz/ready: {str(error) or type(error).__name__}")
            return False

    async def get_principal(self, name: str) -> dict[str, Any] | None:
        """The account as the relay holds it, or ``None`` if it does not exist."""
        result = await self._request("GET", f"/api/principal/{name}", allow_missing=True)
        if result is None:
            return None
        # Stalwart wraps a successful read in {"data": {...}}.
        data = result.get("data") if isinstance(result, dict) else None
        return data if isinstance(data, dict) else result

    async def list_principals(self) -> list[str]:
        """Every individual account the relay holds, by name.

        What the orphan sweep needs: it compares the relay's inventory with the accounts the
        project files ask for, and an inventory it cannot read must be reported as INCOMPLETE
        and never as empty: an empty answer would make every account look like an orphan. So
        a failure raises (``MailRelayError``) rather than returning nothing.

        Paged, because the management API answers a page at a time and a cluster has one
        account per deployment plus the platform's own.
        """
        names: list[str] = []
        page = 1
        while True:
            result = await self._request("GET", f"/api/principal?types=individual&page={page}&limit={_PRINCIPAL_PAGE}")
            data = (result or {}).get("data") or {}
            items = data.get("items") if isinstance(data, dict) else data
            if not isinstance(items, list):
                raise MailRelayError(f"De relay gaf geen lijst accounts terug op pagina {page}: {str(data)[:200]}")
            for item in items:
                # Stalwart answers a list of principal objects; older builds answer a list
                # of plain names. Both are read, because an inventory that silently skips
                # every entry reads as "no accounts" and that is the one wrong answer here.
                name = item.get("name") if isinstance(item, dict) else item
                if isinstance(name, str) and name:
                    names.append(name)
            if len(items) < _PRINCIPAL_PAGE:
                return names
            page += 1

    async def create_principal(
        self,
        name: str,
        password: str,
    ) -> None:
        """Create the account. The relay refuses a duplicate; the manager checks first.

        The account carries a name and secret only; Stalwart v0.11 has no per-principal
        limit field, so daily limits live in the relay's config, keyed on the account name.
        """
        await self._request(
            "POST",
            "/api/principal",
            payload={
                "type": "individual",
                "name": name,
                "secrets": [password],
                # No "emails" field: sieve script pins sender and envelope anyway.
                "roles": ["user"],
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
    ) -> None:
        """Bring an existing account in line with what the project asks for."""
        changes: list[dict[str, Any]] = []
        if password is not None:
            changes.append({"action": "set", "field": "secrets", "value": [password]})
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

    async def get_sender_names(self) -> dict[str, str]:
        """Every account's display name, as the relay currently holds it.

        The whole table in one call, and that is on purpose: the generated script is
        rendered from ALL of them, so writing one name means knowing the others.
        """
        return await self._get_sender_keys(MAIL_SENDER_NAME_PREFIX)

    async def get_sender_addresses(self) -> dict[str, str]:
        """Every account's configured sender address, as the relay currently holds it.

        Nearly empty: an address is DERIVED from the account name unless a key says
        otherwise (see ``MAIL_SENDER_ADDRESS_PREFIX``).
        """
        return await self._get_sender_keys(MAIL_SENDER_ADDRESS_PREFIX)

    async def get_account_projects(self) -> dict[str, str]:
        """Which project each account belongs to, as the relay currently holds it.

        The whole table in one call, for the same reason as the names and addresses: the
        generated script is rendered from ALL of them.
        """
        return await self._get_sender_keys(MAIL_ACCOUNT_PROJECT_PREFIX)

    async def _get_sender_keys(self, prefix: str) -> dict[str, str]:
        """One settings series, keyed by account name with the prefix stripped."""
        result = await self._request("GET", f"/api/settings/list?prefix={prefix}.")
        items = ((result or {}).get("data") or {}).get("items") or {}
        return {account: value for account, value in items.items() if value}

    async def set_sender(self, account: str, display_name: str, from_address: str = "", project: str = "") -> bool:
        """Give this account the sender recipients see. Returns whether anything changed.

        Three halves in ONE call, and one lock around them, because all three are rendered
        into the same generated script: writing them separately would render that script
        three times, reload three times, and let two concurrent writers drop each other's
        value.

        Idempotent by comparison and not by luck: a project is processed again on every
        change it makes, and every write here drags a rebuild of the relay's whole
        configuration behind it.

        ``project`` is which project this account sends for, for the ``X-ZAD-Project``
        header; the account name no longer says so. Empty for a platform account, which sends
        on nobody's behalf and gets no header.

        An empty value is a REMOVAL, not an empty value stored: no display name is a valid
        outcome (the message then leaves with the account's address and nothing in front of
        it) and no address means the including script derives one, which is what nearly
        every account does. A key that says nothing is one an administrator reading the
        settings has to interpret. That removal is also how a withdrawn approval lands: the
        next run passes an empty address and the account falls back to the derived one.
        """
        _validate_display_name(account, display_name)
        _validate_address(account, from_address)
        _validate_project(account, project)
        async with _TABLE_LOCK:
            names = await self.get_sender_names()
            addresses = await self.get_sender_addresses()
            projects = await self.get_account_projects()
            if (
                names.get(account, "") == display_name
                and addresses.get(account, "") == from_address
                and projects.get(account, "") == project
            ):
                return False
            self._set_or_remove(names, account, display_name)
            self._set_or_remove(addresses, account, from_address)
            self._set_or_remove(projects, account, project)
            await self._write_sender_keys(account, display_name, from_address, project, names, addresses, projects)
            return True

    async def set_sender_name(self, account: str, display_name: str) -> bool:
        """Give this account the display name recipients see, keeping its address derived.

        The short form for a caller that has no address to set; it WIPES any address the
        account had, which is what makes "derive it again" a single call.
        """
        return await self.set_sender(account, display_name, "")

    async def delete_sender_name(self, account: str) -> None:
        """Forget this account's display name, address AND project. Replay-safe: missing keys are fine."""
        await self.set_sender(account, "", "", "")

    @staticmethod
    def _set_or_remove(values: dict[str, str], account: str, value: str) -> None:
        """Store the value, or drop the key when it is empty."""
        if value:
            values[account] = value
        else:
            values.pop(account, None)

    async def _write_sender_keys(
        self,
        account: str,
        display_name: str,
        from_address: str,
        project: str,
        names: dict[str, str],
        addresses: dict[str, str],
        projects: dict[str, str],
    ) -> None:
        """The keys and the generated script in ONE request, then a reload.

        One request, because the keys are the data and the script is what the relay actually
        reads: two requests could leave the relay reading a table that no longer matches
        what OPI thinks it wrote.
        """
        changes: list[dict[str, Any]] = []
        for prefix, value in (
            (MAIL_SENDER_NAME_PREFIX, display_name),
            (MAIL_SENDER_ADDRESS_PREFIX, from_address),
            (MAIL_ACCOUNT_PROJECT_PREFIX, project),
        ):
            key = f"{prefix}.{account}"
            if value:
                changes.append({"type": "insert", "values": [[key, value]], "assert_empty": False})
            else:
                changes.append({"type": "delete", "keys": [key]})
        changes.append(
            {
                "type": "insert",
                "values": [[MAIL_SENDER_TABLE_KEY, render_sender_table(names, addresses, projects)]],
                "assert_empty": False,
            }
        )
        await self._request("POST", "/api/settings", payload=changes)
        await self.reload()
        logger.info(
            f"Afzender van mailaccount {account} bijgewerkt op de relay: naam {display_name!r}, "
            f"adres {from_address!r}, project {project!r}"
        )

    async def set_rate_limit(self, account: str, messages_per_day: int) -> bool:
        """Give this account its own daily limit. Returns whether anything changed.

        Idempotent by comparison, like ``set_sender`` and for the same reason: a project is
        processed again on every change it makes, and every write drags a rebuild of the
        relay's whole configuration behind it.

        The account name ends up inside the limiter's ``match`` EXPRESSION, between single
        quotes, so it is checked against the same pattern the sender script uses: a name with
        a quote or a space in it would break that expression open.
        """
        _validate_account(account)
        prefix = f"{MAIL_LIMITER_PREFIX}-{account}"
        rate = f"{messages_per_day}/1d"
        # Compare all four: a limiter without ``match`` counts per connection, not per message.
        wanted = {
            "enable": "true",
            "key": "authenticated_as",
            "match": f"authenticated_as == '{account}'",
            "rate": rate,
        }
        existing = await self._get_limiter_keys(prefix)
        if all(existing.get(part) == value for part, value in wanted.items()):
            return False
        await self._request(
            "POST",
            "/api/settings",
            payload=[
                {
                    "type": "insert",
                    "assert_empty": False,
                    "values": [[f"{prefix}.{part}", value] for part, value in wanted.items()],
                }
            ],
        )
        await self.reload()
        logger.info(f"Dagbudget van mailaccount {account} op de relay gezet op {rate}")
        return True

    async def delete_rate_limit(self, account: str) -> None:
        """Forget this account's daily limit. Replay-safe: missing keys are fine.

        Without this the limiter stays behind after the account is gone, and the next
        account by that name silently inherits a number nobody asked for.
        """
        _validate_account(account)
        prefix = f"{MAIL_LIMITER_PREFIX}-{account}"
        if not await self._get_limiter_keys(prefix):
            return
        await self._request(
            "POST",
            "/api/settings",
            payload=[{"type": "delete", "keys": [f"{prefix}.{part}" for part in ("enable", "key", "match", "rate")]}],
        )
        await self.reload()
        logger.info(f"Dagbudget van mailaccount {account} van de relay verwijderd")

    async def _get_limiter_keys(self, prefix: str) -> dict[str, str]:
        """This account's limiter keys, by their last segment. Empty when it has none.

        ``removeprefix`` because the relay strips the prefix it was asked for; doing it here
        as well means a relay that ever stops doing so does not turn every run into a write.
        """
        result = await self._request("GET", f"/api/settings/list?prefix={prefix}.")
        items = ((result or {}).get("data") or {}).get("items") or {}
        return {key.removeprefix(f"{prefix}."): value for key, value in items.items()}

    async def reload(self) -> None:
        """Rebuild the relay's configuration, so a written value takes effect.

        A build error on ANY key leaves the whole rebuild where it was, measured, and it is
        the trap in this path: a value written correctly stays invisible because something
        entirely unrelated does not compile. The errors the relay reports are therefore
        logged rather than swallowed, even when none of them is ours.
        """
        result = await self._request("GET", "/api/reload")
        errors = ((result or {}).get("data") or {}).get("errors") or {}
        if errors:
            logger.warning(f"De mailrelay meldt configuratiefouten bij het herladen: {list(errors)}")


def _validate_account(account: str) -> None:
    """Refuse an account name that would not be text inside a generated expression.

    Belt and braces around a value ``generate_mail_account_name`` computes, and the layer
    that has to hold if it ever stops being computed: the name is written into a sieve
    string literal (the sender script) and into a limiter ``match`` expression, both between
    quotes.

    Raises:
        MailSenderNameError: The name carries a character that would end the string it is
            written into.
    """
    if not _ACCOUNT_PATTERN.match(account):
        raise MailSenderNameError(
            f"Mailaccountnaam {account!r} hoort alleen kleine letters, cijfers en streepjes te bevatten"
        )


def _validate_display_name(account: str, display_name: str) -> None:
    """Refuse anything that would not be text inside the generated sieve script.

    Raises:
        MailSenderNameError: The account name or the display name carries a character that
            would end the string it is written into.
    """
    _validate_account(account)
    if _NAME_FORBIDDEN.search(display_name):
        raise MailSenderNameError(
            f"Weergavenaam {display_name!r} bevat een teken dat niet in een mailheader hoort "
            "(regeleinde, @, punthaak, aanhalingsteken, backslash of dollarteken)"
        )
    if len(display_name) > 64:
        raise MailSenderNameError(f"Weergavenaam {display_name!r} is langer dan 64 tekens")


def _validate_address(account: str, address: str) -> None:
    """Refuse a sender address that may not be written into the generated sieve script.

    THE SECOND LOCK. An address can come out of a project file, past the form, the API and an
    approval. This layer checks it once more against the same rule
    (``mail_sender_address_error``), so a value that got around the approval still fails here
    before it lands in the sieve string literal.

    An empty address is valid and means "derive it from the account name".

    Raises:
        MailSenderNameError: The address is malformed or lives outside
            ``MAIL_SENDER_DOMAIN_ALLOWLIST`` (``opi.utils.naming``).
    """
    error = mail_sender_address_error(address)
    if error:
        raise MailSenderNameError(f"Afzenderadres {address!r} voor {account!r}: {error}")


def _validate_project(account: str, project: str) -> None:
    """Refuse a project name that would not be text inside the generated sieve script.

    The name goes into a sieve string literal and from there into the ``X-ZAD-Project``
    header, so the same characters are refused as in a display name. A project name is
    computed nowhere, it comes straight out of the project file, which is exactly why this
    layer has to hold.

    Raises:
        MailSenderNameError: The account or the project name carries a character that would
            end the string it is written into.
    """
    _validate_account(account)
    if _NAME_FORBIDDEN.search(project):
        raise MailSenderNameError(
            f"Projectnaam {project!r} bevat een teken dat niet in een mailheader hoort "
            "(regeleinde, @, punthaak, aanhalingsteken, backslash of dollarteken)"
        )


def render_sender_table(
    names: dict[str, str],
    addresses: dict[str, str] | None = None,
    projects: dict[str, str] | None = None,
) -> str:
    """The sieve script the identity rules include, rendered from the stored names and addresses.

    ``global`` is what makes a value visible to the including script (RFC 6609: an included
    script shares only variables declared global), and both are declared in both scripts.

    ``addresses`` is almost always empty. An account's address is DERIVED from its name by
    the including script, and a key here exists only for an account that has to send under a
    different one; setting ``afzender`` is what makes the including script skip its own
    derivation. An account with an address but no name is a normal outcome and gets a rule of
    its own, which is why the maps are walked together instead of nested.

    ``projects`` holds which project an account sends for, and it is the only place that can
    come from: an account belongs to a DEPLOYMENT and its name carries a hash, so the
    including script can no longer cut the project off it. An account without an entry gets
    no ``project`` and therefore no ``X-ZAD-Project`` header, which is what the platform
    accounts need.
    """
    addresses = addresses or {}
    projects = projects or {}
    lines = [
        "# Gegenereerd door ZAD (RC-145/RC-159/RC-401). Niet met de hand bewerken: elke wijziging",
        f"# van een project schrijft dit script opnieuw, uit de sleutels onder {MAIL_SENDER_NAME_PREFIX}.",
        f"# {MAIL_SENDER_ADDRESS_PREFIX}. en {MAIL_ACCOUNT_PROJECT_PREFIX}.",
        'require ["variables"];',
        'global "naam";',
        'global "afzender";',
        'global "project";',
    ]
    for account in sorted(set(names) | set(addresses) | set(projects)):
        name = names.get(account, "")
        address = addresses.get(account, "")
        project = projects.get(account, "")
        if not name and not address and not project:
            # Nothing to set is no rule: an empty entry would yield a rule that does nothing.
            continue
        _validate_display_name(account, name)
        _validate_address(account, address)
        _validate_project(account, project)
        assignments = []
        if name:
            assignments.append(f'set "naam" "{name}";')
        if address:
            assignments.append(f'set "afzender" "{address}";')
        if project:
            assignments.append(f'set "project" "{project}";')
        lines.append(f'if string :is "${{env.authenticated_as}}" "{account}" {{ {" ".join(assignments)} }}')
    return "\n".join(lines) + "\n"


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
