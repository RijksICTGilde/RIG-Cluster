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
