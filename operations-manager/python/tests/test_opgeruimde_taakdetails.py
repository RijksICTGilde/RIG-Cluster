"""Wat het portaal toont van een taak waarvan de werkgegevens zijn leeggemaakt (RC-282).

De kop van een afgeronde taak blijft 90 dagen staan, zijn werkgegevens (``payload``,
``logs``, ``subtasks``, ``events``, ``result``, ``error_message``, ``current_step``) gaan na
een uur leeg. De lezers van die rij moeten daar een nette weergave van maken en niet
omvallen.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from opi.core.persistent_task_progress import PersistentTaskProgressManager
from opi.core.templates_lotc import templates_lotc
from opi.web.router import _progress_page_context, _v2_task_to_template_context
from opi.web.router_tasks import _normalize_task


def _opgeruimde_taak() -> dict:
    """Een rij zoals ``cleanup_old_tasks`` hem achterlaat."""
    return {
        "task_id": "abc-123",
        "task_type": "update_image",
        "project_name": "va-48w",
        "deployment_name": "d1",
        "status": "completed",
        "current_step": None,
        "progress_percent": 100,
        "created_by": "iemand@rijksoverheid.nl",
        "created_at": "2026-03-01T10:00:00+00:00",
        "completed_at": "2026-03-01T10:05:00+00:00",
        "error_message": None,
        "payload": {},
        "logs": None,
        "subtasks": [],
        "events": None,
        "result": None,
        "details_purged_at": "2026-03-01T11:05:00+00:00",
    }


class TestDeTakenlijst:
    def test_de_rij_houdt_wie_wat_en_wanneer(self) -> None:
        rij = _normalize_task(_opgeruimde_taak())

        assert rij["soort"] == "Image bijwerken"
        assert rij["deployment"] == "d1"
        assert rij["status"] == "Voltooid"
        assert rij["door"] == "iemand@rijksoverheid.nl"
        assert rij["gestart"] == "2026-03-01T10:00:00+00:00"
        assert rij["beeindigd"] == "2026-03-01T10:05:00+00:00"

    def test_de_tabel_toont_hem(self) -> None:
        html = templates_lotc.env.get_template("bg/_tasks.html.j2").render(
            request=None, project_name="va-48w", items=[_normalize_task(_opgeruimde_taak())]
        )

        assert "Image bijwerken" in html
        assert "iemand@rijksoverheid.nl" in html


class TestDeVoortgangspagina:
    def test_de_context_meldt_dat_de_details_weg_zijn(self) -> None:
        context = _v2_task_to_template_context(_opgeruimde_taak(), "va-48w")

        assert context["details_purged"] is True
        assert context["status"] == "completed"
        assert context["tasks"] == []
        assert context["error"] is None

    def test_de_stapregel_doet_niet_alsof_de_taak_nog_loopt(self) -> None:
        """Zonder stapregel viel de pagina terug op "Verwerking gestart...", bij een taak
        die al afgerond was."""
        context = _v2_task_to_template_context(_opgeruimde_taak(), "va-48w")

        assert context["current_step"] == "Details niet meer beschikbaar"

    def test_een_lopende_taak_zonder_stapregel_houdt_de_oude_tekst(self) -> None:
        taak = _opgeruimde_taak()
        taak["details_purged_at"] = None
        taak["status"] = "running"

        assert _v2_task_to_template_context(taak, "va-48w")["current_step"] == "Verwerking gestart..."

    def test_een_taak_met_zijn_details_meldt_niets(self) -> None:
        taak = _opgeruimde_taak()
        taak["details_purged_at"] = None
        taak["current_step"] = "Done"
        taak["subtasks"] = [{"id": "a", "name": "Manifesten schrijven", "status": "completed"}]

        context = _v2_task_to_template_context(taak, "va-48w")

        assert context["details_purged"] is False
        assert [t["name"] for t in context["tasks"]] == ["Manifesten schrijven"]

    def test_de_pagina_zegt_het_ook_op_het_scherm(self) -> None:
        """Anders staat er een geslaagde taak zonder enige stap, zonder uitleg waarom."""
        html = templates_lotc.env.get_template("bg/_task-progress.html.j2").render(
            _progress_page_context(_opgeruimde_taak(), "abc-123")
        )

        assert "niet meer beschikbaar" in html

    def test_een_taak_met_zijn_details_krijgt_die_melding_niet(self) -> None:
        taak = _opgeruimde_taak()
        taak["details_purged_at"] = None

        html = templates_lotc.env.get_template("bg/_task-progress.html.j2").render(
            _progress_page_context(taak, "abc-123")
        )

        assert "niet meer beschikbaar" not in html


class TestDeSchrijfkant:
    def test_een_mislukking_zet_de_fouttekst_niet_in_de_stapregel(self) -> None:
        """``current_step`` hoort bij de kop, en die blijft 90 dagen staan.

        De tekst van de exceptie kan de invoer van de handler dragen, bijvoorbeeld de
        doelgegevens van een restore, en hoort dus alleen in de werkgegevens.
        """
        with patch(
            "opi.core.persistent_task_progress.asyncio.get_running_loop",
            side_effect=RuntimeError,
        ):
            progress = PersistentTaskProgressManager(task_id="abc-123", project_name="va-48w", task_service=MagicMock())
            progress.fail_project("restore naar postgres://u:geheim@db.extern/zad mislukt")

        assert progress._current_step == "Failed"
        assert progress.project_failure == "restore naar postgres://u:geheim@db.extern/zad mislukt"
