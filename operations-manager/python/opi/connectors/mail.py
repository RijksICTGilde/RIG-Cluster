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
from opi.utils.connect_retry import retry_on_connect_error
from opi.utils.naming import mail_sender_address_error

logger = logging.getLogger(__name__)

#: Hoelang een management-aanroep mag blijven proberen te verbinden. Een podwissel van de
#: relay laat de Service even zonder endpoint, en zonder deze marge kost dat een hele
#: uitrol. Zelfde budget en dezelfde helper als de ArgoCD-connector.
RELAY_CONNECT_RETRY_SECONDS = 30.0

#: Een readiness-meting mag niet in een trage relay blijven hangen: de uitkomst van een
#: meting die langer duurt dan de tik zelf is toch niet meer actueel.
RELAY_READY_TIMEOUT_SECONDS = 5.0


class MailRelayError(RuntimeError):
    """The relay refused or could not answer a management call."""


class MailRelayUnavailableError(MailRelayError):
    """De relay was niet bereikbaar. Dit is een storing van het platform, niet van het project.
    Taakhandlers die een uitrol met ``processing_failed`` rapporteren zetten ``error_category: InternalError``,
    zodat de gebruiker weet dat hij niets kan repareren.
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
#: account that has a different one (Keycloak's, and since RC-399 a project whose
#: requested address a platform administrator approved). A second series (not a second field
#: on the same key) because relay settings are flat: one key, one value. Both are rendered
#: into the same script.
MAIL_SENDER_ADDRESS_PREFIX = "zad.afzender.adres"

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
#: Belt and braces around a value that is already computed by ``generate_mail_account_name``
#: -- it ends up inside a sieve string literal, and this is the layer that would have to
#: hold if it ever stopped being computed.
#:
#: ``\A``/``\Z`` and not ``^``/``$``: ``$`` also matches just BEFORE a closing newline, so
#: ``"zad-keycloak\n"`` passes an anchored ``$`` pattern -- and a newline is exactly the
#: character that ends the sieve string literal this value goes into. ``\Z`` is the end of
#: the string and nothing else.
_ACCOUNT_PATROON = re.compile(r"\A[a-z0-9][a-z0-9-]*\Z")

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
        """One management call. ``allow_missing`` turns a 404 into ``None``.

        Een relay die niet antwoordt wordt hier een ``MailRelayUnavailableError``. Zonder
        die omzetting ontsnapt de transportfout van aiohttp aan elke aanroeper die op
        ``MailRelayError`` rekent, en komt hij bij de gebruiker uit als een onverklaarde
        mislukking op zijn eigen project.
        """

        async def _eenmaal() -> dict[str, Any] | None:
            return await self._eenmalig_verzoek(method, path, payload, allow_missing)

        try:
            return await retry_on_connect_error(_eenmaal, RELAY_CONNECT_RETRY_SECONDS, f"{method} {path}")
        except (aiohttp.ClientError, TimeoutError, OSError) as error:
            raise MailRelayUnavailableError(
                "De mailservice van het platform is niet bereikbaar, dit ligt niet aan je project "
                f"({method} {path}: {error or type(error).__name__})"
            ) from error

    async def _eenmalig_verzoek(
        self,
        method: str,
        path: str,
        payload: Any | None,
        allow_missing: bool,
    ) -> dict[str, Any] | None:
        """Een management-aanroep zonder herhaling en zonder omzetting van transportfouten."""
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

    async def is_ready(self) -> bool:
        """Of de relay zijn eigen ``/healthz/ready`` met 200 antwoordt.

        Dezelfde ingang als de readiness-probe van de pod: geen inloggegevens, geen
        mailsessie, en dus ook geen tik in ``queue.limiter.inbound.platform``. Buiten
        ``_request`` gehouden omdat het antwoordlichaam geen JSON is en een herhaling
        hier juist niet hoort: wie meet of de relay er is, wil het antwoord van nu.
        """
        connector = aiohttp.TCPConnector(ssl=self._verify_tls)
        timeout = aiohttp.ClientTimeout(total=RELAY_READY_TIMEOUT_SECONDS)
        try:
            async with (
                aiohttp.ClientSession(connector=connector, timeout=timeout) as session,
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

    # The sender ADDRESS is derived from the account name; the DISPLAY NAME is the one
    # value OPI keeps per account in relay settings, rendered into a sieve script.

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

    async def _get_sender_keys(self, prefix: str) -> dict[str, str]:
        """One settings series, keyed by account name with the prefix stripped."""
        result = await self._request("GET", f"/api/settings/list?prefix={prefix}.")
        items = ((result or {}).get("data") or {}).get("items") or {}
        return {account: waarde for account, waarde in items.items() if waarde}

    async def set_sender(self, account: str, display_name: str, from_address: str = "") -> bool:
        """Give this account the sender recipients see. Returns whether anything changed.

        Both halves in ONE call, and one lock around them, because both are rendered into
        the same generated script: writing them separately would render that script twice,
        reload twice, and let two concurrent writers drop each other's value.

        Idempotent by comparison and not by luck: a project is processed again on every
        change it makes, and every write here drags a rebuild of the relay's whole
        configuration behind it.

        An empty value is a REMOVAL, not an empty value stored: no display name is a valid
        outcome (the message then leaves with the account's address and nothing in front of
        it) and no address means the including script derives one, which is what nearly
        every account does. A key that says nothing is one an administrator reading the
        settings has to interpret. That removal is also how a withdrawn approval lands: the
        next run passes an empty address and the account falls back to the derived one.
        """
        _controleer_naam(account, display_name)
        _controleer_adres(account, from_address)
        async with _TABEL_SLOT:
            namen = await self.get_sender_names()
            adressen = await self.get_sender_addresses()
            if namen.get(account, "") == display_name and adressen.get(account, "") == from_address:
                return False
            self._zet_of_verwijder(namen, account, display_name)
            self._zet_of_verwijder(adressen, account, from_address)
            await self._write_sender_keys(account, display_name, from_address, namen, adressen)
            return True

    async def set_sender_name(self, account: str, display_name: str) -> bool:
        """Give this account the display name recipients see, keeping its address derived.

        The short form for a caller that has no address to set; it WIPES any address the
        account had, which is what makes "derive it again" a single call.
        """
        return await self.set_sender(account, display_name, "")

    async def delete_sender_name(self, account: str) -> None:
        """Forget this account's display name AND address. Replay-safe: missing keys are fine."""
        await self.set_sender(account, "", "")

    @staticmethod
    def _zet_of_verwijder(waarden: dict[str, str], account: str, waarde: str) -> None:
        """Store the value, or drop the key when it is empty."""
        if waarde:
            waarden[account] = waarde
        else:
            waarden.pop(account, None)

    async def _write_sender_keys(
        self,
        account: str,
        display_name: str,
        from_address: str,
        namen: dict[str, str],
        adressen: dict[str, str],
    ) -> None:
        """The keys and the generated script in ONE request, then a reload.

        One request, because the keys are the data and the script is what the relay actually
        reads: two requests could leave the relay reading a table that no longer matches
        what OPI thinks it wrote.
        """
        wijzigingen: list[dict[str, Any]] = []
        for prefix, waarde in ((MAIL_SENDER_NAME_PREFIX, display_name), (MAIL_SENDER_ADDRESS_PREFIX, from_address)):
            sleutel = f"{prefix}.{account}"
            if waarde:
                wijzigingen.append({"type": "insert", "values": [[sleutel, waarde]], "assert_empty": False})
            else:
                wijzigingen.append({"type": "delete", "keys": [sleutel]})
        wijzigingen.append(
            {
                "type": "insert",
                "values": [[MAIL_SENDER_TABLE_KEY, render_sender_table(namen, adressen)]],
                "assert_empty": False,
            }
        )
        await self._request("POST", "/api/settings", payload=wijzigingen)
        await self.reload()
        logger.info(
            f"Afzender van mailaccount {account} bijgewerkt op de relay: naam {display_name!r}, adres {from_address!r}"
        )

    # Elk account krijgt zijn eigen limiter, zodat het getal uit het projectbestand ook
    # echt wordt afgedwongen in plaats van alleen vastgelegd.

    async def set_rate_limit(self, account: str, messages_per_day: int) -> bool:
        """Give this account its own daily limit. Returns whether anything changed.

        Idempotent by comparison, like ``set_sender`` and for the same reason: a project is
        processed again on every change it makes, and every write drags a rebuild of the
        relay's whole configuration behind it.

        The account name ends up inside the limiter's ``match`` EXPRESSION, between single
        quotes, so it is checked against the same pattern the sender script uses -- a name
        with a quote or a space in it would break that expression open.
        """
        _controleer_account(account)
        prefix = f"{MAIL_LIMITER_PREFIX}-{account}"
        rate = f"{messages_per_day}/1d"
        # Alle vier vergelijken: een limiter zonder ``match`` telt per verbinding, niet per bericht.
        gewenst = {
            "enable": "true",
            "key": "authenticated_as",
            "match": f"authenticated_as == '{account}'",
            "rate": rate,
        }
        bestaand = await self._get_limiter_keys(prefix)
        if all(bestaand.get(deel) == waarde for deel, waarde in gewenst.items()):
            return False
        await self._request(
            "POST",
            "/api/settings",
            payload=[
                {
                    "type": "insert",
                    "assert_empty": False,
                    "values": [[f"{prefix}.{deel}", waarde] for deel, waarde in gewenst.items()],
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
        _controleer_account(account)
        prefix = f"{MAIL_LIMITER_PREFIX}-{account}"
        if not await self._get_limiter_keys(prefix):
            return
        await self._request(
            "POST",
            "/api/settings",
            payload=[{"type": "delete", "keys": [f"{prefix}.{deel}" for deel in ("enable", "key", "match", "rate")]}],
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
        return {sleutel.removeprefix(f"{prefix}."): waarde for sleutel, waarde in items.items()}

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


def _controleer_account(account: str) -> None:
    """Refuse an account name that would not be text inside a generated expression.

    Belt and braces around a value ``generate_mail_account_name`` computes, and the layer
    that has to hold if it ever stops being computed: the name is written into a sieve
    string literal (the sender script) and into a limiter ``match`` expression, both between
    quotes.

    Raises:
        MailSenderNameError: The name carries a character that would end the string it is
            written into.
    """
    if not _ACCOUNT_PATROON.match(account):
        raise MailSenderNameError(
            f"Mailaccountnaam {account!r} hoort alleen kleine letters, cijfers en streepjes te bevatten"
        )


def _controleer_naam(account: str, display_name: str) -> None:
    """Refuse anything that would not be text inside the generated sieve script.

    Raises:
        MailSenderNameError: The account name or the display name carries a character that
            would end the string it is written into.
    """
    _controleer_account(account)
    if _NAAM_VERBODEN.search(display_name):
        raise MailSenderNameError(
            f"Weergavenaam {display_name!r} bevat een teken dat niet in een mailheader hoort "
            "(regeleinde, @, punthaak, aanhalingsteken, backslash of dollarteken)"
        )
    if len(display_name) > 64:
        raise MailSenderNameError(f"Weergavenaam {display_name!r} is langer dan 64 tekens")


def _controleer_adres(account: str, adres: str) -> None:
    """Refuse a sender address that may not be written into the generated sieve script.

    HET TWEEDE SLOT. Sinds RC-399 kan een adres uit een projectbestand komen, langs het
    formulier, de API en een goedkeuring. Deze laag toetst het nog een keer, met dezelfde
    regel (``mail_sender_address_error``), zodat een waarde die langs de goedkeuring heen
    hier arriveert alsnog strandt voordat ze in de sieve-stringliteraal belandt.

    Een leeg adres is geldig en betekent "leid het af uit de accountnaam".

    Raises:
        MailSenderNameError: The address is malformed or lives outside
            ``MAIL_SENDER_DOMAIN_ALLOWLIST`` (``opi.utils.naming``).
    """
    fout = mail_sender_address_error(adres)
    if fout:
        raise MailSenderNameError(f"Afzenderadres {adres!r} voor {account!r}: {fout}")


def render_sender_table(namen: dict[str, str], adressen: dict[str, str] | None = None) -> str:
    """The sieve script the identity rules include, rendered from the stored names and addresses.

    Deliberately dull: one ``if`` per account, no expressions, no data structures. It is
    generated code, so the only thing it may do is be obvious -- and the reader of a relay
    configuration should be able to see at a glance that this file cannot do anything except
    set two variables.

    ``global`` is what makes a value visible to the including script (RFC 6609: an included
    script shares only variables declared global), and both are declared in both scripts.

    ``adressen`` is almost always empty. An account's address is DERIVED from its name by
    the including script, and a key here exists only for an account that has to send under
    a different one; setting ``afzender`` is what makes the including script skip its own
    derivation. An account with an address but no name is a normal outcome and gets a rule
    of its own, which is why the two maps are walked together instead of nested.
    """
    adressen = adressen or {}
    regels = [
        "# Gegenereerd door ZAD (RC-145/RC-159). Niet met de hand bewerken: elke wijziging van",
        f"# een project schrijft dit script opnieuw, uit de sleutels onder {MAIL_SENDER_NAME_PREFIX}.",
        f"# en {MAIL_SENDER_ADDRESS_PREFIX}.",
        'require ["variables"];',
        'global "naam";',
        'global "afzender";',
    ]
    for account in sorted(set(namen) | set(adressen)):
        naam = namen.get(account, "")
        adres = adressen.get(account, "")
        if not naam and not adres:
            # Geen naam en geen adres is geen regel: een leeg item zou een regel opleveren
            # die niets doet.
            continue
        _controleer_naam(account, naam)
        _controleer_adres(account, adres)
        toekenningen = []
        if naam:
            toekenningen.append(f'set "naam" "{naam}";')
        if adres:
            toekenningen.append(f'set "afzender" "{adres}";')
        regels.append(f'if string :is "${{env.authenticated_as}}" "{account}" {{ {" ".join(toekenningen)} }}')
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
