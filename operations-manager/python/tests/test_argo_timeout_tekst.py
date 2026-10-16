"""Een timeout naar ArgoCD moet in de foutmelding te zien zijn.

In productie stond op 6 oktober 2026 deze regel, twee keer, en hij liet een hele taak
sneuvelen (toets-hn7, "Diensten en manifesten bijwerken")::

    opi.connectors.argo - ERROR - Error getting application status:

Daar stond niets achter. De oorzaak is dat ``asyncio.TimeoutError`` een lege ``str()``
heeft, en de brede vangst in ``get_application_status`` de uitzondering met ``{e}``
interpoleerde. Juist de meest voorkomende fout op dit pad, een read timeout, logde dus
precies niets, en de log-watcher stuurde die lege regel door als alert.

Daarom ``_exc_text``: die geeft de tekst als die er is, en anders de klasse. Niet de repr,
want die maakt de uitzonderingen die WEL een tekst hebben juist slechter leesbaar.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from opi.connectors.argo import ArgoConnector, _exc_text


def _connector() -> ArgoConnector:
    """Een connector met een token in de hand, zodat er geen login plaatsvindt."""
    connector = ArgoConnector()
    connector.auth_token = "token-1"
    return connector


@pytest.mark.asyncio
async def test_timeout_laat_een_leesbare_melding_achter() -> None:
    """De melding noemt de soort fout, ook als de uitzondering zelf geen tekst heeft."""
    connector = _connector()

    with (
        patch.object(connector, "_make_authenticated_request", AsyncMock(side_effect=TimeoutError())),
        pytest.raises(RuntimeError) as excinfo,
    ):
        await connector.get_application_status("some-app")

    melding = str(excinfo.value)
    assert melding.rstrip().endswith("TimeoutError"), f"de melding noemt de fout niet: {melding!r}"
    assert melding != "Error getting application status: ", "de melding is weer leeg"


def test_waarom_de_kale_interpolatie_niet_volstaat() -> None:
    """Vastleggen waarom de helper bestaat: deze uitzondering heeft geen tekst."""
    assert f"{TimeoutError()}" == "", "TimeoutError heeft blijkbaar wel een tekst, dan mag de helper weg"
    assert _exc_text(TimeoutError()) == "TimeoutError"


def test_een_uitzondering_met_tekst_houdt_zijn_eigen_tekst() -> None:
    """De helper mag de leesbare meldingen niet vervangen door een repr."""
    assert _exc_text(RuntimeError("argocd-server weigert de verbinding")) == "argocd-server weigert de verbinding"
