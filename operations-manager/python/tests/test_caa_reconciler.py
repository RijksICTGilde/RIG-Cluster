"""De CAA-reconciler voegt toe en gooit nooit iets weg.

Twee dingen moeten hier vastliggen. Eerst: hij vergelijkt genormaliseerd, want TransIP mag
quoting en witruimte anders teruggeven dan wij hebben gestuurd, en een naïeve vergelijking
POST't dan bij elke start een duplicaat. Daarna: een CAA-record dat wij niet verwachten
blijft staan -- dat kan een bewuste uitzondering zijn tijdens een CA-migratie, en dat
automatisch wegpoetsen breekt iemands uitgifte zonder dat het opvalt.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from opi.connectors.transip import TransIPConnector
from opi.core.caa_reconciler import reconcile_caa_records
from opi.core.dns_config import MANAGED_DNS_ZONES


def _entry(content: str, name: str = "@", record_type: str = "CAA") -> dict[str, Any]:
    return {"name": name, "type": record_type, "expire": 3600, "content": content}


def _connector(domains: list[str], entries_per_zone: dict[str, list[dict[str, Any]]]) -> AsyncMock:
    connector = AsyncMock()
    connector.list_domains.return_value = domains
    connector.get_dns_entries.side_effect = lambda zone: entries_per_zone.get(zone, [])
    return connector


@pytest.mark.asyncio
async def test_no_credentials_skips() -> None:
    """Zonder credentials wordt de TransIP-API niet aangeraakt."""
    connector = _connector([], {})
    with (
        patch("opi.core.caa_reconciler.settings") as mock_settings,
        patch("opi.core.caa_reconciler.TransIPConnector", return_value=connector) as factory,
    ):
        mock_settings.TRANSIP_ACCOUNT_NAME = None
        mock_settings.TRANSIP_PRIVATE_KEY = "PEM"
        await reconcile_caa_records()

    factory.assert_not_called()
    connector.list_domains.assert_not_called()
    connector.add_dns_entry.assert_not_called()


@pytest.mark.asyncio
async def test_zone_not_in_account_is_skipped(caplog: pytest.LogCaptureFixture) -> None:
    """Een zone die het account niet houdt geeft een waarschuwing en nul adds."""
    held = [zone for zone in MANAGED_DNS_ZONES if zone != "rijks.app"]
    entries = {zone: [_entry('0 issue "letsencrypt.org"'), _entry('0 issuewild "letsencrypt.org"')] for zone in held}
    connector = _connector(held, entries)

    with caplog.at_level("WARNING"):
        await _reconcile(connector)

    connector.get_dns_entries.assert_any_call("rijksapp.nl")
    assert "rijks.app" not in [call.args[0] for call in connector.get_dns_entries.call_args_list]
    assert any("rijks.app" in record.message for record in caplog.records)
    connector.add_dns_entry.assert_not_called()


@pytest.mark.asyncio
async def test_correct_zone_does_nothing() -> None:
    """Zones die de records al hebben leveren geen enkele schrijfactie op."""
    zones = list(MANAGED_DNS_ZONES)
    entries = {zone: [_entry('0 issue "letsencrypt.org"'), _entry('0 issuewild "letsencrypt.org"')] for zone in zones}
    connector = _connector(zones, entries)

    await _reconcile(connector)

    connector.add_dns_entry.assert_not_called()


@pytest.mark.asyncio
async def test_quoting_variants_count_as_present() -> None:
    """Andere hoofdletters of extra witruimte is hetzelfde record, geen duplicaat."""
    zones = list(MANAGED_DNS_ZONES)
    entries = {
        zone: [_entry('0  issue  "LETSENCRYPT.ORG"'), _entry(' 0 ISSUEWILD "letsencrypt.org" ')] for zone in zones
    }
    connector = _connector(zones, entries)

    await _reconcile(connector)

    connector.add_dns_entry.assert_not_called()


@pytest.mark.asyncio
async def test_empty_zone_adds_two() -> None:
    """Een lege zone krijgt issue en issuewild op de apex, met de afgesproken TTL."""
    zones = list(MANAGED_DNS_ZONES)
    connector = _connector(zones, {})

    await _reconcile(connector)

    assert connector.add_dns_entry.await_count == 2 * len(zones)
    for call in connector.add_dns_entry.await_args_list:
        zone, name, record_type, content, ttl = call.args
        assert name == "@"
        assert record_type == "CAA"
        assert ttl == 3600
        assert content in ('0 issue "letsencrypt.org"', '0 issuewild "letsencrypt.org"')


@pytest.mark.asyncio
async def test_non_apex_caa_is_ignored() -> None:
    """Een CAA op een diepere naam telt niet mee: de apex is wat wij beheren."""
    zones = list(MANAGED_DNS_ZONES)
    entries = {
        zone: [
            _entry('0 issue "letsencrypt.org"', name="www"),
            _entry('0 issuewild "letsencrypt.org"', name="www"),
        ]
        for zone in zones
    }
    connector = _connector(zones, entries)

    await _reconcile(connector)

    assert connector.add_dns_entry.await_count == 2 * len(zones)


@pytest.mark.asyncio
async def test_unexpected_caa_is_warned_not_deleted(caplog: pytest.LogCaptureFixture) -> None:
    """Een vreemde CA blijft staan, wordt gemeld, en houdt onze eigen records niet tegen."""
    zones = list(MANAGED_DNS_ZONES)
    entries = {zone: [_entry('0 issue "digicert.com"')] for zone in zones}
    connector = _connector(zones, entries)

    with caplog.at_level("WARNING"):
        await _reconcile(connector)

    assert any("digicert.com" in record.message for record in caplog.records)
    assert connector.add_dns_entry.await_count == 2 * len(zones)
    # Wegpoetsen kan niet: de connector heeft geen verwijdermethode.
    assert not [name for name in dir(TransIPConnector) if "delete" in name or "remove" in name]


async def _reconcile(connector: AsyncMock) -> None:
    """Draai de reconciler met credentials en een gemockte connector."""
    with (
        patch("opi.core.caa_reconciler.settings") as mock_settings,
        patch("opi.core.caa_reconciler.TransIPConnector", return_value=connector),
    ):
        mock_settings.TRANSIP_ACCOUNT_NAME = "digigilde"
        mock_settings.TRANSIP_PRIVATE_KEY = "PEM"
        await reconcile_caa_records()
