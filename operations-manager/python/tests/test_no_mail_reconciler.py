"""De no-mail-reconciler voegt toe en gooit nooit iets weg.

Drie dingen moeten hier vastliggen. Eerst de normalisatie: TransIP mag quoting, witruimte
en de punt aan het eind van een MX-doel anders teruggeven dan wij hebben gestuurd, en een
naieve vergelijking POST't dan bij elke start een duplicaat -- precies de fout die
ongemerkt kan uitgroeien. Daarna de MX-poort: een naam die wel mail ontvangt mag nooit een
null MX krijgen. En als laatste dat een bestaand SPF- of DMARC-record blijft staan; een
tweede zo'n record maakt het beleid ongeldig in plaats van strenger.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from opi.connectors.transip import TransIPConnector
from opi.core.dns_config import NO_MAIL_NAMES, no_mail_names
from opi.core.no_mail_reconciler import reconcile_no_mail_records

ZONES = list(NO_MAIL_NAMES)
NAMES_PER_ZONE = {zone: no_mail_names(zone) for zone in ZONES}
TOTAL_NAMES = sum(len(names) for names in NAMES_PER_ZONE.values())


def _entry(name: str, record_type: str, content: str) -> dict[str, Any]:
    return {"name": name, "type": record_type, "expire": 3600, "content": content}


def _connector(domains: list[str], entries_per_zone: dict[str, list[dict[str, Any]]]) -> AsyncMock:
    connector = AsyncMock()
    connector.list_domains.return_value = domains
    connector.get_dns_entries.side_effect = lambda zone: entries_per_zone.get(zone, [])
    return connector


async def _reconcile(connector: AsyncMock) -> None:
    """Draai de reconciler met credentials en een gemockte connector."""
    with (
        patch("opi.core.no_mail_reconciler.settings") as mock_settings,
        patch("opi.core.no_mail_reconciler.TransIPConnector", return_value=connector),
    ):
        mock_settings.TRANSIP_ACCOUNT_NAME = "digigilde"
        mock_settings.TRANSIP_PRIVATE_KEY = "PEM"
        await reconcile_no_mail_records()


def _written(connector: AsyncMock) -> list[tuple[str, str, str, str, int]]:
    return [call.args for call in connector.add_dns_entry.await_args_list]


def _full_policy(zone: str) -> list[dict[str, Any]]:
    """De drie records op elke gedeclareerde naam van een zone, zoals wij ze schrijven."""
    entries: list[dict[str, Any]] = []
    for name in NAMES_PER_ZONE[zone]:
        entries.append(_entry(name, "TXT", "v=spf1 -all"))
        entries.append(_entry(name, "MX", "0 ."))
        entries.append(_entry(f"_dmarc.{name}", "TXT", "v=DMARC1; p=reject;"))
    return entries


@pytest.mark.asyncio
async def test_no_credentials_skips() -> None:
    """Zonder credentials wordt de TransIP-API niet aangeraakt."""
    connector = _connector([], {})
    with (
        patch("opi.core.no_mail_reconciler.settings") as mock_settings,
        patch("opi.core.no_mail_reconciler.TransIPConnector", return_value=connector) as factory,
    ):
        mock_settings.TRANSIP_ACCOUNT_NAME = None
        mock_settings.TRANSIP_PRIVATE_KEY = "PEM"
        await reconcile_no_mail_records()

    factory.assert_not_called()
    connector.list_domains.assert_not_called()
    connector.add_dns_entry.assert_not_called()


@pytest.mark.asyncio
async def test_zone_not_in_account_is_skipped(caplog: pytest.LogCaptureFixture) -> None:
    """Een zone die het account niet houdt geeft een waarschuwing en nul adds."""
    held = [zone for zone in ZONES if zone != "rijks.app"]
    entries = {zone: _full_policy(zone) for zone in held}
    connector = _connector(held, entries)

    with caplog.at_level("WARNING"):
        await _reconcile(connector)

    assert "rijks.app" not in [call.args[0] for call in connector.get_dns_entries.call_args_list]
    assert any("rijks.app" in record.message for record in caplog.records)
    connector.add_dns_entry.assert_not_called()


@pytest.mark.asyncio
async def test_empty_zone_gets_three_records_per_name() -> None:
    """Een naam zonder mailrecords krijgt SPF, null MX en DMARC, met de afgesproken TTL."""
    connector = _connector(ZONES, {})

    await _reconcile(connector)

    written = _written(connector)
    assert len(written) == 3 * TOTAL_NAMES
    for zone, names in NAMES_PER_ZONE.items():
        for name in names:
            assert (zone, name, "TXT", "v=spf1 -all", 3600) in written
            assert (zone, name, "MX", "0 .", 3600) in written
            assert (zone, f"_dmarc.{name}", "TXT", "v=DMARC1; p=reject;", 3600) in written


@pytest.mark.asyncio
async def test_second_run_writes_nothing() -> None:
    """Twee keer opstarten voegt niets dubbel toe: wat wij schreven telt als aanwezig."""
    entries = {zone: _full_policy(zone) for zone in ZONES}
    connector = _connector(ZONES, entries)

    await _reconcile(connector)

    connector.add_dns_entry.assert_not_called()


@pytest.mark.asyncio
async def test_transip_spelling_variants_count_as_present() -> None:
    """Andere quoting, witruimte, hoofdletters of een punt aan het eind is hetzelfde record."""
    entries = {
        zone: [
            entry
            for name in NAMES_PER_ZONE[zone]
            for entry in (
                _entry(name, "TXT", '"v=spf1  -all"'),
                _entry(name, "MX", "0 . "),
                _entry(f"_dmarc.{name}", "TXT", ' "V=DMARC1; p=REJECT;" '),
            )
        ]
        for zone in ZONES
    }
    connector = _connector(ZONES, entries)

    await _reconcile(connector)

    connector.add_dns_entry.assert_not_called()


@pytest.mark.asyncio
async def test_normalization_off_would_duplicate() -> None:
    """Negatieve controle: zonder normalisatie zou dezelfde invoer wel duplicaten geven."""
    entries = {
        zone: [
            entry
            for name in NAMES_PER_ZONE[zone]
            for entry in (
                _entry(name, "TXT", '"v=spf1  -all"'),
                _entry(name, "MX", "0 . "),
                _entry(f"_dmarc.{name}", "TXT", ' "V=DMARC1; p=REJECT;" '),
            )
        ]
        for zone in ZONES
    }
    connector = _connector(ZONES, entries)

    with patch("opi.core.no_mail_reconciler._normalize_txt", side_effect=lambda content: content):
        await _reconcile(connector)

    # Zonder de TXT-normalisatie herkent hij zijn eigen SPF en DMARC niet terug.
    assert connector.add_dns_entry.await_count == 2 * TOTAL_NAMES


@pytest.mark.asyncio
async def test_name_with_mail_keeps_its_mx(caplog: pytest.LogCaptureFixture) -> None:
    """Een naam die wel mail ontvangt wordt overgeslagen, met een logregel."""
    mailing = NAMES_PER_ZONE[ZONES[0]][0]
    entries = {ZONES[0]: [_entry(mailing, "MX", "10 mail.example.org.")]}
    connector = _connector(ZONES, entries)

    with caplog.at_level("WARNING"):
        await _reconcile(connector)

    assert not [args for args in _written(connector) if args[2] == "MX" and args[:2] == (ZONES[0], mailing)]
    assert any(f"{mailing}.{ZONES[0]}" in record.message and "MX" in record.message for record in caplog.records)
    # De andere namen krijgen hun null MX gewoon.
    assert len([args for args in _written(connector) if args[2] == "MX"]) == TOTAL_NAMES - 1


@pytest.mark.asyncio
async def test_own_spf_and_dmarc_are_left_alone(caplog: pytest.LogCaptureFixture) -> None:
    """Een eigen SPF- of DMARC-record blijft staan: een tweede maakt het beleid ongeldig."""
    name = NAMES_PER_ZONE[ZONES[0]][0]
    entries = {
        ZONES[0]: [
            _entry(name, "TXT", "v=spf1 ip4:147.181.48.71 -all"),
            _entry(f"_dmarc.{name}", "TXT", "v=DMARC1; p=none; rua=mailto:dmarc@example.org"),
        ]
    }
    connector = _connector(ZONES, entries)

    with caplog.at_level("WARNING"):
        await _reconcile(connector)

    written_here = [args for args in _written(connector) if args[0] == ZONES[0]]
    assert [args for args in written_here if args[2] == "TXT"] == []
    assert any("ip4:147.181.48.71" in record.message for record in caplog.records)
    assert any("p=none" in record.message for record in caplog.records)
    # Wegpoetsen kan niet: de connector heeft geen verwijdermethode.
    assert not [attr for attr in dir(TransIPConnector) if "delete" in attr or "remove" in attr]


@pytest.mark.asyncio
async def test_apex_records_are_never_touched() -> None:
    """De apex heeft SPF en DMARC al; de reconciler schrijft daar niets."""
    entries = {
        zone: [
            _entry("@", "TXT", "v=spf1 -all"),
            _entry("_dmarc", "TXT", "v=DMARC1; p=reject; sp=reject"),
        ]
        for zone in ZONES
    }
    connector = _connector(ZONES, entries)

    await _reconcile(connector)

    apex_writes = [args for args in _written(connector) if args[1] in ("@", "", "_dmarc")]
    assert apex_writes == []
    # En de apexrecords tellen niet mee als het beleid van een gedeclareerde naam.
    assert len(_written(connector)) == 3 * TOTAL_NAMES


@pytest.mark.asyncio
async def test_cname_name_is_skipped(caplog: pytest.LogCaptureFixture) -> None:
    """Naast een CNAME weigert TransIP elk record, dus die naam wordt overgeslagen."""
    name = NAMES_PER_ZONE[ZONES[0]][0]
    entries = {ZONES[0]: [_entry(name, "CNAME", "elders.example.org.")]}
    connector = _connector(ZONES, entries)

    with caplog.at_level("WARNING"):
        await _reconcile(connector)

    assert [args for args in _written(connector) if args[0] == ZONES[0]] == []
    assert any("CNAME" in record.message for record in caplog.records)
