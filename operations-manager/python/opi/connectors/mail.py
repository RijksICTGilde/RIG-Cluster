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

import asyncio
import logging
import re
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


#: Settings prefix under which each account's display name is stored, one key per
#: account. This is the DATA: OPI reads it back to see what it wrote, and writes a key only
#: when it changes.
MAIL_SENDER_NAME_PREFIX = "zad.afzender.naam"

#: The sieve script OPI generates from those keys, and the reason the data cannot simply BE
#: the script: to change one account you would have to parse the generated code to keep the
#: others. The script is a projection, the keys are the truth.
#:
#: Why a generated script and not a lookup table, which is what this started out as: an
#: in-memory lookup store built from settings (``lookup.<naam>.<sleutel>``) is only ever
#: built ONCE. A ``POST /api/reload`` does not refresh it -- measured on 20 August 2026
#: against v0.11.8: the first account written got its value, every account added afterwards
#: read as empty until the relay was RESTARTED. A sieve script written through the same API
#: IS recompiled on every reload (measured in both directions: changing the value of an
#: existing account took effect on the very next message, without a restart), so this is
#: the one shape that stays current while the relay keeps running.
#:
#: Its NAME is a constant because the other end of the pair is written in sieve, in
#: ``mail/controller/base/configmap.yaml``, and drift between the two fails SILENTLY:
#: ``include :optional`` skips a script it cannot find without a word, so every project
#: would simply start sending without a display name and nothing would report it.
#: ``test_de_relayconfiguratie_knipt_hetzelfde_voorvoegsel`` pins the two together, the
#: same way it already pinned the ``project-`` prefix.
MAIL_SENDER_SCRIPT_NAME = "zad-afzenders"

#: The settings key that script is stored under, which is the only thing OPI writes.
MAIL_SENDER_TABLE_KEY = f"sieve.trusted.scripts.{MAIL_SENDER_SCRIPT_NAME}.contents"

#: What an account name may look like before it is written into that generated script.
#: Belt and braces around a value that is already computed by ``generate_mail_account_name``
#: -- it ends up inside a sieve string literal, and this is the layer that would have to
#: hold if it ever stopped being computed.
_ACCOUNT_PATROON = re.compile(r"^[a-z0-9][a-z0-9-]*$")

#: Same for a display name. This list and ``FROM_NAME_PATTERN`` (the rule the form and the
#: API validate against) hold exactly the same characters, and they have to: a character only
#: THIS one refuses turns a name the form just approved into an exception halfway through
#: processing a project, where nothing catches it. Refusing them here as well means the
#: generated script cannot be broken open by a value that reached this connector some other
#: way. Note ``$``: a sieve string interpolates ``${...}``, so a name containing it would
#: read a variable instead of being text -- which is why it belongs in both lists, and why
#: ``TestDeWeergavenaamWordtGetoetst`` runs its refused names past this layer too.
_NAAM_VERBODEN = re.compile(r'[@<>"\\$\x00-\x1F\x7F]')


#: Serialises the read-modify-write of the generated table.
#:
#: Writing one account's name means rendering the table from ALL of them, so two projects
#: being processed at the same time would both read the table as it was, and the one that
#: writes last would drop the other one's name -- silently, and until someone happens to
#: process that project again. OPI runs one replica per cluster, so a lock in the process is
#: the whole of the concurrency; a second replica would need the relay to offer a conditional
#: write, which v0.11.8 does not.
_TABEL_SLOT = asyncio.Lock()


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
    ) -> None:
        """Create the account. The relay refuses a duplicate; the manager checks first.

        The account carries a name and a secret and nothing else. It used to carry its
        sender and bounce address as ``emails``, because ``must-match-sender`` refused an
        account that did not own its own envelope address ("501 5.5.4 You are not allowed
        to send from this address", measured). That check is off now: an address only
        exists in a domain the relay holds as LOCAL, and the sender domain became
        ``rijksoverheid.nl`` -- holding that locally would swallow mail to colleagues
        there instead of relaying it. The sieve script pins sender and envelope anyway.

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
                # GEEN "emails". Een adres kan alleen bestaan in een domein dat de relay
                # als lokaal domein kent, en ons afzenderdomein is rijksoverheid.nl: dat
                # als lokaal domein registreren zou mail AAN collega's daar lokaal laten
                # bezorgen in plaats van doorsturen. Het account heeft de adressen ook niet
                # nodig -- must-match-sender staat uit en het sieve-script zet afzender en
                # envelope onvoorwaardelijk vast. Zie de configmap van de relay.
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

    # --- the display name of an account ------------------------------------------
    #
    # The relay works the sender ADDRESS out for itself, by cutting the ``project-`` prefix
    # off the authenticated account name (identity rule 1 in its configmap). It has to:
    # v0.11.8 offers nothing that can look a value up per account while a message is being
    # accepted -- there is no expression function that reads a principal (``principal_get``,
    # ``directory_query``, ``sql_query`` all do not exist), the lookup store that CAN be read
    # live cannot be written through the management API, and the one that can be written is
    # only built at startup. All measured on 20 August 2026.
    #
    # The display name cannot be derived from anything, so it is the one value that has to
    # travel: OPI keeps it per account in the relay's settings and renders those settings
    # into a small sieve script the identity rules include.

    async def get_sender_names(self) -> dict[str, str]:
        """Every account's display name, as the relay currently holds it.

        The whole table in one call, and that is on purpose: the generated script is
        rendered from ALL of them, so writing one name means knowing the others.
        """
        result = await self._request("GET", f"/api/settings/list?prefix={MAIL_SENDER_NAME_PREFIX}.")
        items = ((result or {}).get("data") or {}).get("items") or {}
        return {account: naam for account, naam in items.items() if naam}

    async def set_sender_name(self, account: str, display_name: str) -> bool:
        """Give this account the display name recipients see. Returns whether anything changed.

        Idempotent by comparison and not by luck: a project is processed again on every
        change it makes, and every write here drags a rebuild of the relay's whole
        configuration behind it.

        An empty name is a REMOVAL, not an empty value: no display name is a valid outcome
        (the message then leaves with the project's address and nothing in front of it), and
        a key that says nothing is one an administrator reading the settings has to
        interpret.
        """
        _controleer_naam(account, display_name)
        async with _TABEL_SLOT:
            namen = await self.get_sender_names()
            if namen.get(account, "") == display_name:
                return False
            if display_name:
                namen[account] = display_name
            else:
                namen.pop(account, None)
            await self._write_sender_names(account, display_name, namen)
            return True

    async def delete_sender_name(self, account: str) -> None:
        """Forget this account's display name. Replay-safe: a missing key is fine."""
        await self.set_sender_name(account, "")

    async def _write_sender_names(self, account: str, display_name: str, namen: dict[str, str]) -> None:
        """The key and the generated script in ONE request, then a reload.

        One request, because the key is the data and the script is what the relay actually
        reads: two requests could leave the relay reading a table that no longer matches
        what OPI thinks it wrote.
        """
        sleutel = f"{MAIL_SENDER_NAME_PREFIX}.{account}"
        wijzigingen: list[dict[str, Any]] = []
        if display_name:
            wijzigingen.append({"type": "insert", "values": [[sleutel, display_name]], "assert_empty": False})
        else:
            wijzigingen.append({"type": "delete", "keys": [sleutel]})
        wijzigingen.append(
            {"type": "insert", "values": [[MAIL_SENDER_TABLE_KEY, render_sender_table(namen)]], "assert_empty": False}
        )
        await self._request("POST", "/api/settings", payload=wijzigingen)
        await self.reload()
        logger.info(f"Weergavenaam van mailaccount {account} bijgewerkt op de relay: {display_name!r}")

    async def reload(self) -> None:
        """Rebuild the relay's configuration, so a written value takes effect.

        A build error on ANY key leaves the whole rebuild where it was -- measured, and it is
        the trap in this path: a value written correctly stays invisible because something
        entirely unrelated does not compile. The errors the relay reports are therefore
        logged rather than swallowed, even when none of them is ours.
        """
        result = await self._request("GET", "/api/reload")
        errors = ((result or {}).get("data") or {}).get("errors") or {}
        if errors:
            logger.warning(f"De mailrelay meldt configuratiefouten bij het herladen: {list(errors)}")


def _controleer_naam(account: str, display_name: str) -> None:
    """Refuse anything that would not be text inside the generated sieve script.

    Raises:
        MailSenderNameError: The account name or the display name carries a character that
            would end the string it is written into.
    """
    if not _ACCOUNT_PATROON.match(account):
        raise MailSenderNameError(
            f"Mailaccountnaam {account!r} hoort alleen kleine letters, cijfers en streepjes te bevatten"
        )
    if _NAAM_VERBODEN.search(display_name):
        raise MailSenderNameError(
            f"Weergavenaam {display_name!r} bevat een teken dat niet in een mailheader hoort "
            "(regeleinde, @, punthaak, aanhalingsteken, backslash of dollarteken)"
        )
    if len(display_name) > 64:
        raise MailSenderNameError(f"Weergavenaam {display_name!r} is langer dan 64 tekens")


def render_sender_table(namen: dict[str, str]) -> str:
    """The sieve script the identity rules include, rendered from the stored names.

    Deliberately dull: one ``if`` per account, no expressions, no data structures. It is
    generated code, so the only thing it may do is be obvious -- and the reader of a relay
    configuration should be able to see at a glance that this file cannot do anything except
    set one variable.

    ``global`` is what makes the value visible to the including script (RFC 6609: an included
    script shares only variables declared global), and it is declared in both.
    """
    regels = [
        "# Gegenereerd door ZAD (RC-145). Niet met de hand bewerken: elke wijziging van een",
        "# project schrijft dit script opnieuw, uit de sleutels onder " + MAIL_SENDER_NAME_PREFIX + ".",
        'require ["variables"];',
        'global "naam";',
    ]
    for account in sorted(namen):
        naam = namen[account]
        if not naam:
            # Geen naam is geen regel: een leeg item zou een regel opleveren die niets doet.
            continue
        _controleer_naam(account, naam)
        regels.append(f'if string :is "${{env.authenticated_as}}" "{account}" {{ set "naam" "{naam}"; }}')
    return "\n".join(regels) + "\n"


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
