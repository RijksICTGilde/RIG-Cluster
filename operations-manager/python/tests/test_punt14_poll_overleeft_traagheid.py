"""Een trage statuspoll mag een punt14-test niet doden.

De punt14-tests zetten OPI bewust onder gelijktijdige druk, en met ``rollout=true`` wacht
een componentpatch op een volledige ArgoCD-uitrol -- veruit het traagste deel van een actie
(RC-117). Onder die druk haalt een enkele poll de 30 seconden van de httpx-client soms niet.

Gemeten in de doorloop van RC-118: ``test_deployment_overleeft_een_gelijktijdige_uitrol``
viel twee keer om op een ``httpx.ReadTimeout``, niet op een assertion. De poll zat in
``_await_task``, dat een EIGEN deadline van uren heeft -- er was dus alle ruimte om het
gewoon opnieuw te vragen, maar de fout liep door en nam de test mee.

Deze test loopt zonder cluster mee in de gewone ronde, want dat is precies wat er ontbrak:
het gedrag zat alleen in een suite die apart draait, en daar zag niemand het.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

_MODULE = Path(__file__).resolve().parent / "e2e" / "test_sandbox_punt14.py"


def _await_task():
    """Laad ``_await_task`` los, zonder de e2e-conftest of een browser op te tuigen."""
    spec = importlib.util.spec_from_file_location("punt14_onder_test", _MODULE)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module._await_task


def _antwoord(status_code: int, payload: dict) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.content = b"{}"
    response.json.return_value = payload
    return response


def _client_die(*antwoorden):
    """Een httpx.Client-stub die achtereenvolgens deze dingen doet (Exception = opgooien)."""
    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = False
    client.get.side_effect = list(antwoorden)
    return client


class TestEenTragePollIsGeenUitkomst:
    def test_een_readtimeout_wordt_opnieuw_geprobeerd(self):
        client = _client_die(
            httpx.ReadTimeout("The read operation timed out"),
            _antwoord(200, {"status": "completed"}),
        )
        with patch("httpx.Client", return_value=client), patch("time.sleep"):
            uitkomst = _await_task()("https://zad.test", "taak-1", "sleutel")

        assert uitkomst == {"status": "completed"}
        assert client.get.call_count == 2, "na een timeout is niet opnieuw gevraagd"

    def test_meerdere_timeouts_achter_elkaar_overleven(self):
        client = _client_die(
            httpx.ReadTimeout("traag"),
            httpx.ConnectTimeout("ook traag"),
            _antwoord(200, {"status": "completed", "result": {"ok": True}}),
        )
        with patch("httpx.Client", return_value=client), patch("time.sleep"):
            uitkomst = _await_task()("https://zad.test", "taak-2", "sleutel")

        assert uitkomst["status"] == "completed"

    def test_een_nog_lopende_taak_wordt_gewoon_verder_gepold(self):
        """202 betekent 'nog bezig'; dat gedrag mag de vangst niet verstoren."""
        client = _client_die(
            _antwoord(202, {"status": "running"}),
            _antwoord(200, {"status": "completed"}),
        )
        with patch("httpx.Client", return_value=client), patch("time.sleep"):
            uitkomst = _await_task()("https://zad.test", "taak-3", "sleutel")

        assert uitkomst == {"status": "completed"}


class TestWatEenUitkomstBLIJFT:
    """De vangst mag alleen traagheid opvangen, geen antwoorden wegpoetsen."""

    def test_een_foutstatus_blijft_een_foutstatus(self):
        client = _client_die(_antwoord(500, {"detail": "stuk"}))
        with patch("httpx.Client", return_value=client), patch("time.sleep"):
            uitkomst = _await_task()("https://zad.test", "taak-4", "sleutel")

        assert uitkomst["status"] == "poll_error"
        assert uitkomst["http_status"] == 500

    def test_blijft_de_taak_hangen_dan_valt_de_lus_uit_op_de_eigen_deadline(self):
        """Oneindig timeouts: geen eeuwige lus, maar de bestaande 'running'-uitkomst."""
        client = MagicMock()
        client.__enter__.return_value = client
        client.__exit__.return_value = False
        client.get.side_effect = httpx.ReadTimeout("altijd traag")

        tijden = iter([0.0, 1.0, 2.0, 10_000.0])
        with (
            patch("httpx.Client", return_value=client),
            patch("time.sleep"),
            patch("time.monotonic", side_effect=lambda: next(tijden)),
        ):
            uitkomst = _await_task()("https://zad.test", "taak-5", "sleutel")

        assert uitkomst["status"] == "running"


@pytest.mark.parametrize(
    "fout",
    [httpx.ReadTimeout("t"), httpx.ConnectTimeout("t"), httpx.ReadError("t"), httpx.ConnectError("t")],
)
def test_elke_transportfout_telt_als_traagheid(fout):
    client = _client_die(fout, _antwoord(200, {"status": "completed"}))
    with patch("httpx.Client", return_value=client), patch("time.sleep"):
        assert _await_task()("https://zad.test", "taak", "sleutel")["status"] == "completed"
