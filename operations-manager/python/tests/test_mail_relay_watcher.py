"""RC-261: een mailrelay die niet ready is moet iemand bereiken.

Op 13 oktober 2026 lag de relay twee uur plat zonder dat iemand het zag. Elke uitrol met
send-email faalde, en de foutmelding stuurde de gebruiker naar zijn eigen project. Er was
geen enkel signaal dat het platform had gefaald.

Wat hier wordt vastgehouden, en het zijn precies de drie dingen die een melding bruikbaar
of onbruikbaar maken:

1. ÉÉN melding per storing, niet één per tik. Een lus die elke minuut meet en elke minuut
   meldt maakt het kanaal na een uur onbruikbaar, en dan ziet niemand de VOLGENDE storing.
2. De drempel is echt een drempel: een relay die even niet antwoordt (een podwissel, een
   rollout) mag niemand wakker maken.
3. Herstel wordt ook gemeld, één keer. Zonder dat moet degene die de melding kreeg zelf
   gaan kijken of het over is.
"""

from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi import server
from opi.connectors.mail import MailConnector, MailRelayNotConfiguredError
from opi.core.config import settings
from opi.core.mail_relay_watcher import MailRelayWatcher

DREMPEL = 300


@pytest.fixture
def _relay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "MAIL_RELAY_API_URL", "http://rig-mail-relay:8080")
    monkeypatch.setattr(settings, "MAIL_RELAY_DOWN_ALERT_SECONDS", DREMPEL)
    monkeypatch.setattr(settings, "LOGWATCHER_NTFY_TOPIC", "geheim-topic")


def _watcher(monkeypatch: pytest.MonkeyPatch, *, ready: bool | list[bool]) -> tuple[MailRelayWatcher, MagicMock]:
    """Een watcher met een relay die ``ready`` antwoordt, en de verzamelde meldingen."""
    antwoorden = ready if isinstance(ready, list) else None
    connector = MagicMock()
    if antwoorden is None:
        connector.is_ready = AsyncMock(return_value=ready)
    else:
        connector.is_ready = AsyncMock(side_effect=antwoorden)
    monkeypatch.setattr(
        "opi.core.mail_relay_watcher.create_mail_connector", AsyncMock(return_value=connector), raising=True
    )
    meldingen = MagicMock()
    monkeypatch.setattr(MailRelayWatcher, "_post", staticmethod(meldingen))
    return MailRelayWatcher(), meldingen


def _klok(monkeypatch: pytest.MonkeyPatch, seconden: list[float]) -> None:
    monkeypatch.setattr("opi.core.mail_relay_watcher.monotonic", MagicMock(side_effect=seconden))


@pytest.mark.usefixtures("_relay")
class TestDeMeldingBijEenStoring:
    @pytest.mark.asyncio
    async def test_na_vijf_minuten_niet_ready_komt_er_precies_een_melding(self, monkeypatch) -> None:
        watcher, meldingen = _watcher(monkeypatch, ready=False)
        # Tikken van een minuut: de eerste zet de teller, bij de zesde is de drempel om. De
        # twee erna moeten zwijgen.
        _klok(monkeypatch, [0, 60, 120, 180, 240, 300, 360, 420])

        for _ in range(8):
            await watcher.tik()

        assert meldingen.call_count == 1, "een melding per storing, niet per tik"
        titel = meldingen.call_args.args[1]
        tekst = meldingen.call_args.args[2]
        assert "niet bereikbaar" in titel
        assert "send-email" in tekst

    @pytest.mark.asyncio
    async def test_een_korte_onderbreking_meldt_niets(self, monkeypatch) -> None:
        """Een podwissel of een rollout van de relay mag niemand wakker maken."""
        watcher, meldingen = _watcher(monkeypatch, ready=[False, False, True])
        _klok(monkeypatch, [0, 60, 120])

        for _ in range(3):
            await watcher.tik()

        meldingen.assert_not_called()

    @pytest.mark.asyncio
    async def test_herstel_meldt_precies_een_keer(self, monkeypatch) -> None:
        watcher, meldingen = _watcher(monkeypatch, ready=[False, False, True, True, True])
        _klok(monkeypatch, [0, 400])

        for _ in range(5):
            await watcher.tik()

        titels = [aanroep.args[1] for aanroep in meldingen.call_args_list]
        assert titels == ["Mailrelay is niet bereikbaar", "Mailrelay is weer bereikbaar"]

    @pytest.mark.asyncio
    async def test_een_tweede_storing_na_herstel_meldt_weer(self, monkeypatch) -> None:
        """Anders is de eerste storing de enige die ooit gemeld wordt."""
        watcher, meldingen = _watcher(monkeypatch, ready=[False, False, True, False, False])
        _klok(monkeypatch, [0, 400, 1000, 1400])

        for _ in range(5):
            await watcher.tik()

        titels = [aanroep.args[1] for aanroep in meldingen.call_args_list]
        assert titels == [
            "Mailrelay is niet bereikbaar",
            "Mailrelay is weer bereikbaar",
            "Mailrelay is niet bereikbaar",
        ]


class TestWatErNietGemetenIsWordtNietGemeld:
    @pytest.mark.asyncio
    async def test_zonder_relay_doet_de_check_niets(self, monkeypatch) -> None:
        monkeypatch.setattr(settings, "LOGWATCHER_NTFY_TOPIC", "geheim-topic")
        meldingen = MagicMock()
        monkeypatch.setattr(MailRelayWatcher, "_post", staticmethod(meldingen))
        monkeypatch.setattr(
            "opi.core.mail_relay_watcher.create_mail_connector",
            AsyncMock(side_effect=MailRelayNotConfiguredError("geen relay")),
        )

        watcher = MailRelayWatcher()
        for _ in range(10):
            await watcher.tik()

        meldingen.assert_not_called()

    @pytest.mark.asyncio
    async def test_een_onontsleutelbaar_wachtwoord_meldt_geen_storing(self, monkeypatch) -> None:
        """De meting is dan niet gedaan. "Relay onbereikbaar" melden zou naar de verkeerde
        hoek wijzen: het relaywachtwoord is van ons, niet van de relay."""
        monkeypatch.setattr(settings, "LOGWATCHER_NTFY_TOPIC", "geheim-topic")
        meldingen = MagicMock()
        monkeypatch.setattr(MailRelayWatcher, "_post", staticmethod(meldingen))
        monkeypatch.setattr(
            "opi.core.mail_relay_watcher.create_mail_connector",
            AsyncMock(side_effect=ValueError("Failed to decrypt password: no matching AGE key")),
        )

        watcher = MailRelayWatcher()
        for _ in range(10):
            await watcher.tik()

        meldingen.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("_relay")
    async def test_zonder_ntfy_topic_wordt_er_niets_gepost(self, monkeypatch) -> None:
        monkeypatch.setattr(settings, "LOGWATCHER_NTFY_TOPIC", None)
        watcher, meldingen = _watcher(monkeypatch, ready=False)
        _klok(monkeypatch, [0, 400])

        await watcher.tik()
        await watcher.tik()

        meldingen.assert_not_called()


class TestDeWiring:
    def test_de_lifespan_start_de_watcher(self) -> None:
        """Zonder deze schakel is alle dekking hierboven dood: er zou niets meten."""
        bron = inspect.getsource(server)
        assert "MailRelayWatcher()" in bron
        assert "app.state.mail_relay_watcher" in bron

    @pytest.mark.asyncio
    async def test_de_meting_gaat_langs_de_readiness_ingang_van_de_relay(self) -> None:
        """Dezelfde ingang als de probe van de pod: geen inloggegevens en geen mailsessie,
        dus ook geen tik in queue.limiter.inbound.platform."""
        connector = MailConnector("http://rig-mail-relay:8080", "admin", "geheim")
        gevraagd: list[str] = []

        class _Antwoord:
            status = 200

            async def __aenter__(self) -> _Antwoord:
                return self

            async def __aexit__(self, *_: object) -> None:
                return None

        class _Sessie:
            async def __aenter__(self) -> _Sessie:
                return self

            async def __aexit__(self, *_: object) -> None:
                return None

            def get(self, url: str) -> _Antwoord:
                gevraagd.append(url)
                return _Antwoord()

        with patch("aiohttp.ClientSession", return_value=_Sessie()):
            assert await connector.is_ready() is True

        assert gevraagd == ["http://rig-mail-relay:8080/healthz/ready"]
