"""Keep a no-mail policy on the DNS names we publish ourselves.

``router.rijksapp.nl`` and its two siblings are plain A/AAAA names in front of the
cluster. Nothing ever sends mail from them and nothing should ever accept mail for
them, but without records saying so a receiver has nothing to check, and the mail
tests from Pas toe of leg uit fail on exactly that.

SPF does not inherit: a receiver testing ``router.rijksapp.nl`` reads the TXT record
of that name, not the one on the apex. So the policy has to stand on each name:
``v=spf1 -all``, the null MX of RFC 7505, and a DMARC record on ``_dmarc.<name>``.

A separate reconciler from the CAA one, on purpose: it uses the same connector and
the same two gates, but the records, the comparison and the "does this name receive
mail" question have nothing to do with certificate issuance. Merging them would make
one function carry two unrelated policies.

Add-only, like CAA. A record we did not expect is logged and left alone -- and here
that matters twice over, because a second SPF or a second DMARC record on one name
makes the policy invalid rather than stricter.
"""

import logging

from opi.connectors.transip import TransIPConnector
from opi.core.config import settings
from opi.core.dns_config import (
    DMARC_CONTENT,
    DMARC_PREFIX,
    MANAGED_DNS_ZONES,
    NO_MAIL_NAMES,
    NO_MAIL_TTL,
    NULL_MX_CONTENT,
    SPF_CONTENT,
    no_mail_names,
)

logger = logging.getLogger(__name__)

# TransIP writes an apex record as "@"; older exports use the empty name.
APEX_NAMES = ("@", "")


def _normalize_txt(content: str) -> str:
    """Normalize a TXT RDATA string for comparison.

    TransIP may hand back different quoting or spacing than we sent, and a naive
    comparison would then POST a duplicate on every boot. SPF and DMARC tags are
    case-insensitive, so case folds too.
    """
    unquoted = content.strip().strip('"')
    return " ".join(unquoted.split()).lower()


def _normalize_mx(content: str) -> tuple[str, str] | None:
    """Normalize an MX RDATA string to (preference, target), or None if unparsable.

    The root target is written as "." and a normal target may or may not carry the
    trailing dot; both spellings are the same record.
    """
    parts = content.strip().split()
    if len(parts) != 2:
        return None
    preference, target = parts
    return preference.lstrip("0") or "0", target.rstrip(".").lower()


def _entries_for(entries: list[dict[str, str]], name: str, record_type: str) -> list[str]:
    """The RDATA of every entry of one type on one relative name."""
    wanted = APEX_NAMES if name in APEX_NAMES else (name,)
    return [
        str(entry.get("content", ""))
        for entry in entries
        if entry.get("type") == record_type and entry.get("name", "") in wanted
    ]


def _dmarc_name(name: str) -> str:
    """The relative name DMARC lives on for a given name."""
    return DMARC_PREFIX if name in APEX_NAMES else f"{DMARC_PREFIX}.{name}"


async def reconcile_no_mail_records() -> None:
    """Ensure the no-mail records on every declared name this TransIP account can reach.

    Add-only: an existing SPF, MX or DMARC record is never replaced.
    """
    if not settings.TRANSIP_ACCOUNT_NAME or not settings.TRANSIP_PRIVATE_KEY:
        logger.info("No TransIP credentials configured, skipping no-mail reconciliation")
        return

    connector = TransIPConnector(settings.TRANSIP_ACCOUNT_NAME, settings.TRANSIP_PRIVATE_KEY)
    account_domains = set(await connector.list_domains())

    for zone in NO_MAIL_NAMES:
        # The same bolt on the bolt as CAA: a zone we declare but the account does not
        # hold is never touched, so a typo can not reach somebody else's zone.
        if zone not in MANAGED_DNS_ZONES:
            logger.warning(f"No-mail zone {zone} is not a managed DNS zone, skipping")
            continue
        if zone not in account_domains:
            logger.warning(f"Managed zone {zone} is not held by this TransIP account, skipping")
            continue

        entries = await connector.get_dns_entries(zone)
        for name in no_mail_names(zone):
            await _reconcile_name(connector, zone, name, entries)


async def _reconcile_name(connector: TransIPConnector, zone: str, name: str, entries: list[dict[str, str]]) -> None:
    """Add the missing no-mail records on one name in one zone."""
    if _entries_for(entries, name, "CNAME"):
        # TransIP enforces RFC 1035 strictly and refuses any record next to a CNAME.
        logger.warning(f"Name {name}.{zone} is a CNAME, skipping no-mail records")
        return

    await _reconcile_spf(connector, zone, name, entries)
    await _reconcile_mx(connector, zone, name, entries)
    await _reconcile_dmarc(connector, zone, name, entries)


async def _reconcile_spf(connector: TransIPConnector, zone: str, name: str, entries: list[dict[str, str]]) -> None:
    """Add "v=spf1 -all" unless the name already states an SPF policy."""
    existing = [_normalize_txt(content) for content in _entries_for(entries, name, "TXT")]
    present = [content for content in existing if content.startswith("v=spf1")]
    if present:
        # Never a second one: two SPF records on a name make the policy permerror.
        if _normalize_txt(SPF_CONTENT) not in present:
            logger.warning(f"Name {name}.{zone} has its own SPF record {present} (left in place)")
        return

    await connector.add_dns_entry(zone, name, "TXT", SPF_CONTENT, NO_MAIL_TTL)


async def _reconcile_mx(connector: TransIPConnector, zone: str, name: str, entries: list[dict[str, str]]) -> None:
    """Add the null MX unless the name already has mail exchangers."""
    existing = [_normalize_mx(content) for content in _entries_for(entries, name, "MX")]
    if existing:
        wanted = _normalize_mx(NULL_MX_CONTENT)
        if any(mx != wanted for mx in existing):
            # A null MX next to a real one would deny mail this name does receive.
            logger.warning(f"Name {name}.{zone} has MX records {existing}, skipping null MX")
        return

    await connector.add_dns_entry(zone, name, "MX", NULL_MX_CONTENT, NO_MAIL_TTL)


async def _reconcile_dmarc(connector: TransIPConnector, zone: str, name: str, entries: list[dict[str, str]]) -> None:
    """Add the DMARC policy on _dmarc.<name> unless the name already has one."""
    dmarc_name = _dmarc_name(name)
    existing = [_normalize_txt(content) for content in _entries_for(entries, dmarc_name, "TXT")]
    present = [content for content in existing if content.startswith("v=dmarc1")]
    if present:
        # Never a second one: DMARC ignores a name that publishes more than one record.
        if _normalize_txt(DMARC_CONTENT) not in present:
            logger.warning(f"Name {dmarc_name}.{zone} has its own DMARC record {present} (left in place)")
        return

    await connector.add_dns_entry(zone, dmarc_name, "TXT", DMARC_CONTENT, NO_MAIL_TTL)
