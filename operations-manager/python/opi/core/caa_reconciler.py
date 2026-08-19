"""Keep CAA records in place on the DNS zones we administer.

Without CAA, every publicly trusted CA in the world may issue a certificate for
any name under our zones. With CAA, DNS says who may. We only use Let's Encrypt,
so the bolt can go on.

Add-only by design: a CAA record we did not expect is logged and left alone. Such
a record can be a deliberate exception during a CA migration, and quietly wiping
it would break someone's issuance without anyone noticing. Cleaning up is a
human's job.
"""

import logging

from opi.connectors.transip import TransIPConnector
from opi.core.config import settings
from opi.core.dns_config import CAA_TTL, MANAGED_DNS_ZONES, desired_caa_contents

logger = logging.getLogger(__name__)

# TransIP writes an apex record as "@"; older exports use the empty name.
APEX_NAMES = ("@", "")


def _normalize_caa(content: str) -> tuple[str, str, str] | None:
    """Normalize a CAA RDATA string to (flags, tag, value) for comparison.

    TransIP may hand back different quoting or spacing than we sent, and a naive
    string comparison would then POST a duplicate on every boot.
    """
    parts = content.strip().split(None, 2)
    if len(parts) != 3:
        return None
    flags, tag, value = parts
    return flags.strip(), tag.lower(), value.strip().strip('"').lower()


async def reconcile_caa_records() -> None:
    """Ensure CAA records on every managed zone this TransIP account actually holds.

    Add-only: an unexpected CAA record is logged and left alone.
    """
    if not settings.TRANSIP_ACCOUNT_NAME or not settings.TRANSIP_PRIVATE_KEY:
        logger.info("No TransIP credentials configured, skipping CAA reconciliation")
        return

    connector = TransIPConnector(settings.TRANSIP_ACCOUNT_NAME, settings.TRANSIP_PRIVATE_KEY)
    account_domains = set(await connector.list_domains())

    for zone in MANAGED_DNS_ZONES:
        # A zone we declare but the account does not hold is the bolt on the bolt:
        # a typo in MANAGED_DNS_ZONES can never touch somebody else's zone.
        if zone not in account_domains:
            logger.warning(f"Managed zone {zone} is not held by this TransIP account, skipping")
            continue
        await _reconcile_zone(connector, zone)


async def _reconcile_zone(connector: TransIPConnector, zone: str) -> None:
    """Add the missing CAA records on one zone's apex."""
    entries = await connector.get_dns_entries(zone)
    existing = [entry for entry in entries if entry.get("type") == "CAA" and entry.get("name", "") in APEX_NAMES]
    present = {normalized for entry in existing if (normalized := _normalize_caa(entry.get("content", "")))}

    wanted = desired_caa_contents(zone)
    wanted_normalized = {normalized for content in wanted if (normalized := _normalize_caa(content))}

    for unexpected in present - wanted_normalized:
        logger.warning(f"Unexpected CAA record on {zone}: {unexpected} (left in place, remove it by hand if wrong)")

    for content in wanted:
        if _normalize_caa(content) in present:
            continue
        # Only the apex, never a deeper name: TransIP enforces RFC 1035 strictly and
        # refuses a record next to a CNAME. Not needed either, since a CA walks up to
        # the first CAA set it finds.
        await connector.add_dns_entry(zone, "@", "CAA", content, CAA_TTL)
