"""Een waarschuwing op de LAATSTE wizardstap verdwijnt niet.

Vervolg op ``test_wizard_waarschuwing_blokkeert_niet.py``. Daar is vastgelegd dat een
waarschuwing de stap niet meer tegenhoudt en meereist naar de volgende stap. Maar de
laatste stap heeft geen volgende stap: die tak gaat naar ``_render_modal_review``, en
daar viel de waarschuwing op de grond -- precies op het scherm waar de gebruiker
besluit te bevestigen.

De reviewpagina heeft al een ``warnings``-kanaal (verwijderde services, het herstellen
van een backup). De waarschuwingen van de laatste stap gaan daar nu bij in.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

WAARSCHUWING = "Het cluster kan geen certificaat aanvragen voor een eigen domein."


def _render(field_warnings: dict[str, list[str]] | None) -> list[str]:
    """Render de reviewpagina en geef terug welke waarschuwingen erin gingen."""
    from opi.web import router_detail_edit

    state = MagicMock()
    state.get_merged_data.return_value = {}
    state.get_steps.return_value = []
    state.locked_services = None

    with patch.object(router_detail_edit, "render_fragment", return_value="<div></div>") as render:
        router_detail_edit._render_modal_review(
            MagicMock(),
            "token",
            "demo",
            "modal-domain",
            [],
            state,
            field_warnings=field_warnings,
        )

    return render.call_args.kwargs["context"]["warnings"]


class TestDeLaatsteStap:
    def test_de_waarschuwing_staat_op_de_reviewpagina(self) -> None:
        warnings = _render({"deployments/0/base-domain": [WAARSCHUWING]})

        assert WAARSCHUWING in warnings

    def test_meerdere_velden_leveren_allemaal_hun_waarschuwing(self) -> None:
        warnings = _render(
            {
                "deployments/0/base-domain": [WAARSCHUWING],
                "deployments/0/subdomain": ["Tweede waarschuwing."],
            }
        )

        assert WAARSCHUWING in warnings
        assert "Tweede waarschuwing." in warnings

    def test_zonder_waarschuwing_blijft_de_lijst_leeg(self) -> None:
        """De bestaande waarschuwingen van de reviewpagina mogen er niet door groeien."""
        assert _render(None) == []


class TestDeLaatsteStapGeeftZeDoor:
    """De aanroeper: de tak zonder volgende stap moet ze daadwerkelijk meegeven."""

    def test_de_reviewtak_geeft_section_warnings_mee(self) -> None:
        from pathlib import Path

        bron = Path(__file__).resolve().parents[1] / "opi" / "web" / "router_detail_edit.py"
        na_de_laatste_stap = bron.read_text().split("# All steps completed", 1)[1]
        assert "field_warnings=section_warnings or None" in na_de_laatste_stap
