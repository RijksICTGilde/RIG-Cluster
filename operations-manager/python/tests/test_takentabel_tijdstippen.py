"""De takentabel toont onze eigen tijd, niet het UTC-getal uit de database.

WAT ER MISGING

De cellen Gestart en Beeindigd stonden op ``(item.gestart or "")[:16] | replace("T", " ")``:
de eerste zestien tekens van de ruwe tijdstempel. Twee dingen tegelijk mis.

* De ZONE. De takendienst bewaart zijn tijdstippen in een ``timestamptz``-kolom die door
  de database wordt gevuld, en die staat op ``Etc/UTC``; ``to_dict()`` maakt er
  ``2026-09-17T21:18:55.951682+00:00`` van. Die ``+00:00`` valt buiten de eerste zestien
  tekens, dus wat de gebruiker zag was het UTC-getal zonder dat erbij stond. Wij kijken in
  Europe/Amsterdam, dus in de zomer stond alles twee uur te vroeg -- en de LOG van
  diezelfde taak schrijft wel in onze tijd, dus de tabel en de log spraken elkaar tegen.
* De MANIER. Er was al een filter dat dit doet, ``dutch_date``, gebruikt op de
  goedkeuringspagina en op de deploymentkaart. De takentabel deed het zelf, en anders.

Wat hieronder wordt vastgelegd is de UITKOMST op het scherm, gerenderd. Dat het filter
zelf klopt staat in tests/test_template_helpers.py; dat geen sjabloon meer zelf afkapt in
tests/test_dates_go_through_one_filter.py.
"""

from __future__ import annotations

from opi.core.templates_lotc import templates_lotc
from opi.web.router_tasks import _normalize_run, _normalize_task


def _render(items: list[dict]) -> str:
    return templates_lotc.env.get_template("bg/_tasks.html.j2").render(request=None, project_name="va-48w", items=items)


class TestDeGetoondeTijd:
    def test_een_taak_wordt_in_onze_eigen_tijd_getoond(self) -> None:
        """21:18 UTC in september is 23:18 hier; het oude sjabloon toonde 21:18."""
        html = _render(
            [
                _normalize_task(
                    {
                        "task_id": "abc-123",
                        "task_type": "backup",
                        "status": "completed",
                        "created_at": "2026-09-17T21:18:55.951682+00:00",
                        "completed_at": "2026-09-17T23:40:01.123456+00:00",
                    }
                )
            ]
        )

        assert "17 sep 2026 23:18" in html
        assert "18 sep 2026 01:40" in html
        assert "2026-09-17 21:18" not in html

    def test_de_winterstand_verschuift_een_uur(self) -> None:
        """Niet twee uur hardgecodeerd: in januari is het CET en dus UTC+1."""
        html = _render(
            [_normalize_task({"task_id": "a", "task_type": "backup", "created_at": "2026-01-14T17:14:00+00:00"})]
        )

        assert "14 jan 2026 18:14" in html

    def test_een_run_loopt_langs_dezelfde_weg(self) -> None:
        """De tabel toont twee bronnen door elkaar; een run mag niet anders uitpakken."""
        html = _render(
            [
                _normalize_run(
                    {
                        "kind": "db-console",
                        "status": "stopped",
                        "started_at": "2026-09-17T21:18:55+00:00",
                        "ended_at": "2026-09-17T23:40:01+00:00",
                    }
                )
            ]
        )

        assert "17 sep 2026 23:18" in html
        assert "18 sep 2026 01:40" in html

    def test_een_taak_die_nog_loopt_heeft_een_streepje_bij_beeindigd(self) -> None:
        """Het lege veld werd eerder in het sjabloon afgevangen; nu doet het filter dat."""
        html = _render(
            [
                _normalize_task(
                    {
                        "task_id": "a",
                        "task_type": "backup",
                        "status": "running",
                        "created_at": "2026-09-17T21:18:55+00:00",
                        "completed_at": None,
                    }
                )
            ]
        )

        assert "17 sep 2026 23:18" in html
        assert ">-<" in html.replace("\n", "").replace(" ", "")


class TestDeKolomverdeling:
    """De verdeling zelf wordt in de browser gemeten (tests/e2e/), maar dat de datum
    NIET meer de smalste kolom van de zes is, is uit het sjabloon te lezen."""

    def test_de_datumkolommen_zijn_niet_langer_de_smalste(self) -> None:
        html = _render([_normalize_task({"task_id": "a", "task_type": "backup"})])

        kolommen = html.split('columns="')[1].split('"')[0].split()
        breedtes = [float(kolom.removesuffix("fr")) for kolom in kolommen]

        assert len(breedtes) == 6, kolommen
        gestart, beeindigd = breedtes[4], breedtes[5]
        assert min(gestart, beeindigd) > min(breedtes[:4]), (
            f"Gestart en Beeindigd horen niet de smalste te zijn: {kolommen}"
        )
