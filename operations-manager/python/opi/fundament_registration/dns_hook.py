"""The TXT record of a DNS-01 challenge, set and removed by lego's ``exec`` provider.

lego runs this as its own process with three arguments, ``present`` or ``cleanup``, the
FQDN of the challenge and the value of the record. The TransIP token comes from the
environment it inherits, never from an argument: an argument is readable in ``ps``.

That environment is :func:`hook_environment` and nothing wider, so this process asks for
the settings in there and not for the seven an attempt needs.

Run as ``python -m opi.fundament_registration.dns_hook present <fqdn> <value>``.
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import TYPE_CHECKING

from opi.connectors.transip import TRANSIP_API_BASE, TransIPConnector, TransIPError
from opi.fundament_registration.configuration import (
    API_BASE_VARIABLE,
    RECORD_TTL,
    hook_configuration,
    missing_hook_settings,
)

if TYPE_CHECKING:
    from opi.fundament_registration.configuration import HookConfiguration

RECORD_TYPE = "TXT"

PRESENT = "present"
CLEANUP = "cleanup"


def record_name(fqdn: str, zone: str) -> str:
    """The challenge name as TransIP wants it: relative to the zone, without a trailing dot.

    Raises:
        ValueError: when the FQDN is not inside this zone. Writing outside it would touch
            a zone this token was not handed over for.
    """
    name = fqdn.rstrip(".")
    if name == zone:
        return "@"
    if not name.endswith(f".{zone}"):
        raise ValueError(f"{fqdn} ligt niet in zone {zone}")
    return name.removesuffix(f".{zone}")


def _connector(config: HookConfiguration) -> TransIPConnector:
    """The client for one hook call, pointed at the API the environment names."""
    return TransIPConnector.with_access_token(
        config.transip_token, base_url=os.environ.get(API_BASE_VARIABLE) or TRANSIP_API_BASE
    )


async def present(fqdn: str, value: str) -> None:
    """Add the challenge record."""
    config = hook_configuration()
    connector = _connector(config)
    await connector.add_dns_entry(config.zone, record_name(fqdn, config.zone), RECORD_TYPE, value, RECORD_TTL)


async def cleanup(fqdn: str, value: str) -> None:
    """Remove the challenge record this run added."""
    config = hook_configuration()
    connector = _connector(config)
    await connector.delete_dns_entry(config.zone, record_name(fqdn, config.zone), RECORD_TYPE, value, RECORD_TTL)


async def run(arguments: list[str]) -> int:
    """Carry out one lego hook call. Returns the exit code lego reads."""
    if len(arguments) != 3 or arguments[0] not in (PRESENT, CLEANUP):
        print(f"gebruik: dns_hook [{PRESENT}|{CLEANUP}] <fqdn> <waarde>", file=sys.stderr)
        return 2
    missing = missing_hook_settings()
    if missing:
        print(f"instellingen ontbreken: {', '.join(missing)}", file=sys.stderr)
        return 3

    action, fqdn, value = arguments
    try:
        if action == PRESENT:
            await present(fqdn, value)
        else:
            await cleanup(fqdn, value)
    except (TransIPError, ValueError) as error:
        print(f"{action} van {fqdn} mislukte: {error}", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    return asyncio.run(run(sys.argv[1:]))


if __name__ == "__main__":
    sys.exit(main())
