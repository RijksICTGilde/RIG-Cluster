"""Een verlopen poll breekt een ArgoCD-wacht-lus niet meer af.

WAT ER MISGING

Productie, 15 oktober 2026. De event loop stond 14 s stil, waardoor een GET naar ArgoCD
van een andere taak op zijn timeout van 10 s afliep. ``get_application_status`` maakte van
die ``TimeoutError`` een ``RuntimeError``, en de wacht-lus gooide elke ``RuntimeError``
door. Daarmee brak EEN verlopen poll de hele wacht van 240 s af: de taak faalde, sloeg de
ArgoCD-sync over, en de gebruiker kreeg een fout te zien terwijl de wijziging wel was
doorgevoerd.

Alleen een terminale toestand die ArgoCD zelf meldt, of het verstrijken van het budget,
hoort de lus te beeindigen. Een transportfout zegt niets over de applicatie.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from opi.connectors.argo import ArgoConnector, ArgoTransportError
from opi.manager.argo_manager import ArgoManager


def _status(sync: str = "Synced", health: str = "Healthy", **extra: Any) -> dict[str, Any]:
    status: dict[str, Any] = {"sync": {"status": sync}, "health": {"status": health}}
    status.update(extra)
    return {"status": status}


def _connector(antwoorden: list[Any]) -> AsyncMock:
    """Een connector die ``antwoorden`` afwerkt; een uitzondering wordt geworpen.

    De laatste waarde wordt herhaald zodra de lijst op is, zodat een lus die blijft
    pollen niet op een StopIteration strandt. Een uitzondering wordt elke keer vers
    gemaakt: dezelfde instantie duizend keer werpen bouwt een ``__context__``-keten op.
    """
    resterend = list(antwoorden)

    async def status(_app_name: str | None = None) -> dict[str, Any] | None:
        waarde = resterend.pop(0) if len(resterend) > 1 else resterend[0]
        if isinstance(waarde, BaseException):
            raise type(waarde)(*waarde.args) from None
        return waarde

    connector = AsyncMock()
    connector.login = AsyncMock(return_value=True)
    connector.get_application_status = status
    connector.refresh_application = AsyncMock(return_value=None)
    return connector


#: De lus pollt elke 2 s en de budgetten zijn minuten; dat is niet wat getoetst wordt.
#: De pauze wordt ingekort, niet weggehaald: zonder pauze wordt de wacht een tight loop.
_KORTE_PAUZE = 0.005
_KORT_BUDGET = 0.3


@pytest.fixture(autouse=True)
def _korte_pauzes(monkeypatch: pytest.MonkeyPatch) -> None:
    # De echte slaap eerst vastleggen: het pad patcht het attribuut op de asyncio-module
    # zelf, dus een aanroep van asyncio.sleep hierbinnen zou naar deze functie terugwijzen.
    echte_slaap = asyncio.sleep

    async def pauze(_seconden: float) -> None:
        await echte_slaap(_KORTE_PAUZE)

    monkeypatch.setattr("opi.manager.argo_manager.asyncio.sleep", pauze)


async def _wacht(antwoorden: list[Any], timeout: float = _KORT_BUDGET) -> bool:
    manager = ArgoManager.__new__(ArgoManager)
    with patch("opi.connectors.argo.create_argo_connector", return_value=_connector(antwoorden)):
        return await manager.wait_for_application_synced("app", timeout=timeout)  # type: ignore[arg-type]


async def test_een_verlopen_poll_laat_de_lus_doorgaan() -> None:
    """Eerst een timeout, daarna Synced/Healthy: de wacht slaagt gewoon."""
    assert await _wacht([ArgoTransportError("Error getting application status: TimeoutError"), _status()]) is True


async def test_alleen_timeouts_eindigt_op_het_budget() -> None:
    """Blijft ArgoCD onbereikbaar, dan stopt de lus pas op de time-out."""
    with pytest.raises(TimeoutError, match=r"after 0\.3s"):
        await _wacht([ArgoTransportError("TimeoutError")])


async def test_een_terminale_toestand_stopt_de_lus_direct() -> None:
    """Een sync-failure die ArgoCD zelf meldt blijft terminaal."""
    terminaal = _status("OutOfSync", "Progressing", operationState={"phase": "Failed", "message": "boem"})
    with pytest.raises(RuntimeError, match="sync failed: Failed"):
        await _wacht([terminaal])


async def test_een_echte_runtimefout_blijft_terminaal() -> None:
    """Een 4xx is geen transportfout en hoort de lus wel af te breken."""
    with pytest.raises(RuntimeError, match="HTTP 400"):
        await _wacht([RuntimeError("Failed to get application status: HTTP 400")])


async def _wacht_infra(antwoorden: list[Any], timeout: float = _KORT_BUDGET) -> bool:
    manager = ArgoManager.__new__(ArgoManager)
    with patch("opi.connectors.argo.ArgoConnector", return_value=_connector(antwoorden)):
        return await manager.wait_for_infrastructure_ready("project", "cluster", timeout=timeout)  # type: ignore[arg-type]


async def test_infra_een_verlopen_poll_laat_de_lus_doorgaan() -> None:
    """Dezelfde lus als ``wait_for_application_synced``, voor de infrastructuur-applicatie."""
    assert await _wacht_infra([ArgoTransportError("Error getting application status: TimeoutError"), _status()]) is True


async def test_infra_alleen_timeouts_eindigt_op_het_budget() -> None:
    with pytest.raises(TimeoutError, match=r"after 0\.3s"):
        await _wacht_infra([ArgoTransportError("TimeoutError")])


async def test_infra_een_terminale_toestand_stopt_de_lus_direct() -> None:
    terminaal = _status("OutOfSync", "Progressing", operationState={"phase": "Failed", "message": "boem"})
    with pytest.raises(RuntimeError, match="sync failed: Failed"):
        await _wacht_infra([terminaal])


async def test_de_verstreken_tijd_komt_van_de_wandklok() -> None:
    """Een poll die langer duurt dan het poll-interval telt ook echt langer mee.

    Met ``elapsed_time += poll_interval`` zou de lus hier eindeloos doorgaan: elke ronde
    kost 1 s echte tijd maar telt er 2 bij, dus de teller loopt harder dan de klok en het
    gemelde budget klopt niet met de werkelijkheid.
    """
    trage_pollen = 0

    async def traag(_app_name: str | None = None) -> dict[str, Any]:
        nonlocal trage_pollen
        trage_pollen += 1
        await asyncio.sleep(0.05)
        return _status("OutOfSync", "Progressing")

    connector = _connector([_status("OutOfSync", "Progressing")])
    connector.get_application_status = traag
    manager = ArgoManager.__new__(ArgoManager)

    with patch("opi.connectors.argo.create_argo_connector", return_value=connector), pytest.raises(TimeoutError):
        await manager.wait_for_application_synced("app", timeout=0.2, poll_interval=2)  # type: ignore[arg-type]

    assert trage_pollen >= 4, (
        f"de lus stopte na {trage_pollen} pollen; telt hij de tijd weer met poll_interval "
        "in plaats van met de wandklok?"
    )


# --- de connector maakt er een transportfout van, geen kale RuntimeError ---


def _verbinding() -> Any:
    """Een connector zonder constructor: die zou inloggen."""
    verbinding = ArgoConnector.__new__(ArgoConnector)
    verbinding.default_app_name = "user-applications"
    verbinding._actual_base_url = "http://argocd-server"
    return verbinding


@pytest.mark.parametrize(
    "fout",
    [TimeoutError(), ConnectionError("Cannot connect to host argocd-server:80")],
    ids=["timeout", "connect"],
)
async def test_een_mislukt_verzoek_wordt_een_transportfout(fout: BaseException) -> None:
    verbinding = _verbinding()
    verbinding._make_authenticated_request = AsyncMock(side_effect=fout)

    with pytest.raises(ArgoTransportError):
        await verbinding.get_application_status("app")


async def test_een_5xx_is_een_transportfout() -> None:
    verbinding = _verbinding()
    verbinding._make_authenticated_request = AsyncMock(return_value=(503, "upstream connect error"))

    with pytest.raises(ArgoTransportError, match="HTTP 503"):
        await verbinding.get_application_status("app")


async def test_een_4xx_blijft_een_gewone_runtimefout() -> None:
    """Een 400 is geen transportstoring; die hoort een wacht-lus wel te beeindigen."""
    verbinding = _verbinding()
    verbinding._make_authenticated_request = AsyncMock(return_value=(400, "bad request"))

    with pytest.raises(RuntimeError, match="HTTP 400") as fout:
        await verbinding.get_application_status("app")
    assert not isinstance(fout.value, ArgoTransportError)
