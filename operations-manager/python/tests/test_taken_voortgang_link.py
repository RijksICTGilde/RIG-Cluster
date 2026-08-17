"""Vanuit de Taken-tab door naar de voortgang van die ene taak.

De voortgangspagina op ``/projects/progress/<task_id>`` bestond al, met de balk, de
huidige stap, de subtaken en de fouten per component, maar was alleen bereikbaar direct
na het aanmaken van een project. De takenlijst had het id wel in handen en gooide het weg
bij het normaliseren; deze test legt vast dat het meekomt en dat de tabel er een link van
maakt.

De tabel toont twee bronnen door elkaar: achtergrondtaken en runs (databaseconsole, job).
Alleen de eerste soort heeft een voortgangspagina, dus alleen die rij hoort een link te
zijn. Zonder dat onderscheid zou een run naar ``/projects/progress/None`` wijzen.
"""

from __future__ import annotations

from opi.core.templates_lotc import templates_lotc
from opi.web.router_tasks import _normalize_run, _normalize_task


class TestHetIdKomtMee:
    def test_een_taak_draagt_zijn_id(self) -> None:
        rij = _normalize_task({"task_id": "abc-123", "task_type": "backup", "status": "running"})
        assert rij["task_id"] == "abc-123"

    def test_ook_een_afgeronde_taak(self) -> None:
        """Juist bij een mislukking wil je die pagina in: daar staat wat er misging."""
        rij = _normalize_task({"task_id": "abc-123", "task_type": "refresh_project", "status": "failed"})
        assert rij["task_id"] == "abc-123"

    def test_een_run_heeft_er_geen(self) -> None:
        rij = _normalize_run({"kind": "db-console", "status": "running"})
        assert rij["task_id"] is None


class TestDeTabel:
    """De cel Soort is de ingang. Gerenderd, want een sleutel in de context zegt nog
    niets over wat er op het scherm staat."""

    def _render(self, items: list[dict]) -> str:
        return templates_lotc.env.get_template("bg/_tasks.html.j2").render(
            request=None, project_name="va-48w", items=items
        )

    def test_een_taak_wordt_een_link(self) -> None:
        html = self._render([_normalize_task({"task_id": "abc-123", "task_type": "backup", "status": "running"})])
        assert "/projects/progress/abc-123" in html

    def test_een_run_blijft_tekst(self) -> None:
        html = self._render([_normalize_run({"kind": "db-console", "status": "running"})])
        assert "/projects/progress/" not in html
        assert "Databaseconsole" in html


class TestDeBestemmingIsGeenWizardpagina:
    """Waar de link heen wijst moet ELKE taaksoort aankunnen, niet alleen het aanmaken.

    De voortgangspagina is gebouwd voor de wizard en heette zo ook ("project creation
    progress page"). Nu de takenlijst er backups, refreshes en verwijderacties heen
    stuurt, is dat geen aanname meer die stil mag blijven staan: een pagina die alleen
    een create_project begrijpt, zou vanuit deze tabel op de helft van de rijen scheef
    lopen.
    """

    def test_een_gewone_taak_krijgt_een_eigen_afrondtekst(self) -> None:
        from opi.web.router import _progress_page_context

        context = _progress_page_context(
            {"task_id": "abc-123", "task_type": "backup", "status": "completed", "project_name": "va-48w"},
            "abc-123",
        )

        assert "Project succesvol aangemaakt" not in context["success_message"]
        assert context["progress_url"] == "/projects/progress/abc-123/fragment"

    def test_het_aanmaken_houdt_zijn_eigen_tekst(self) -> None:
        from opi.web.router import _progress_page_context

        context = _progress_page_context(
            {"task_id": "abc-123", "task_type": "create_project", "status": "completed", "project_name": "va-48w"},
            "abc-123",
        )

        assert "Project succesvol aangemaakt" in context["success_message"]

    def test_de_weg_terug_gaat_naar_het_project_en_niet_naar_de_wizard(self) -> None:
        """Ook bij een mislukking: daar is de projectpagina waar je verder repareert."""
        from opi.web.router import _progress_page_context

        context = _progress_page_context(
            {"task_id": "abc-123", "task_type": "refresh_project", "status": "failed", "project_name": "va-48w"},
            "abc-123",
        )

        assert "/projects/va-48w/details" in context["on_complete"]
