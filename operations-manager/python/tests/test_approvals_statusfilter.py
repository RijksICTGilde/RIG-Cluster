"""Het statusfilter op /admin/approvals.

Server-side, net als het zoeken en sorteren op /projects: dan werkt het zonder JavaScript,
is een gefilterde lijst deelbaar als URL, en staat de gekozen waarde na een swap nog in de
lijst omdat de server hem meerendert.

Dat laatste is waar deze tests vooral op staan. Een filter dat filtert maar zijn eigen keuze
niet toont, ziet er na de swap uit alsof hij op "alles" staat terwijl je een deelverzameling
bekijkt -- en dat is erger dan geen filter.
"""

from types import SimpleNamespace
from typing import Any

import pytest
from opi.core.templates_lotc import templates_lotc
from opi.web.router_approvals import APPROVAL_STATUSSEN, filter_op_status


def _project(naam: str, *statussen: str) -> dict[str, Any]:
    return {
        "project_name": naam,
        "approval_items": [
            {"current_status": status, "type": "domain", "name": f"{naam}-{i}", "history": []}
            for i, status in enumerate(statussen)
        ],
    }


PROJECTEN = [
    _project("een", "requested", "approved"),
    _project("twee", "approved"),
    _project("drie", "requested", "requested", "denied"),
]


class TestHetFilteren:
    def test_alleen_de_gevraagde_status_blijft_over(self) -> None:
        resultaat = filter_op_status(PROJECTEN, "requested")

        assert [p["project_name"] for p in resultaat] == ["een", "drie"]
        assert [len(p["approval_items"]) for p in resultaat] == [1, 2]

    def test_een_project_zonder_treffers_valt_weg(self) -> None:
        """Een projectpaneel met een lege tabel leest als 'dit project heeft niets'."""
        resultaat = filter_op_status(PROJECTEN, "denied")

        assert [p["project_name"] for p in resultaat] == ["drie"]

    @pytest.mark.parametrize("status", ["", "onzin", "APPROVED"])
    def test_leeg_of_onbekend_filtert_niet(self, status: str) -> None:
        """Onbekend is bewust hetzelfde als leeg: ?status=onzin in een gedeelde link
        hoort de lijst te tonen, niet een lege pagina die als een storing leest."""
        assert filter_op_status(PROJECTEN, status) == PROJECTEN

    def test_de_bron_blijft_ongemoeid(self) -> None:
        """Er wordt een nieuwe lijst gemaakt; het origineel draagt de telling van de
        ongefilterde aanvragen, en die mag niet meekrimpen."""
        filter_op_status(PROJECTEN, "requested")

        assert [len(p["approval_items"]) for p in PROJECTEN] == [2, 1, 3]


class TestDeKeuzelijst:
    """Gerenderd, want een waarde in de context zegt niets over wat er op het scherm staat."""

    def _render(self, status: str) -> str:
        return templates_lotc.env.get_template("bg/admin-approvals.html.j2").render(
            request=SimpleNamespace(cookies={}, url=SimpleNamespace(path="/admin/approvals"), state=SimpleNamespace()),
            projects_data=[],
            approvals_totaal=6,
            approvals_getoond=0,
            approval_status=status,
            approval_statussen=APPROVAL_STATUSSEN,
            navigation={},
            menu_items=[],
        )

    def test_elke_status_staat_in_de_lijst(self) -> None:
        html = self._render("")

        for sleutel, label in APPROVAL_STATUSSEN:
            assert label in html, label
            if sleutel:
                assert f'value="{sleutel}"' in html, sleutel

    def test_de_gekozen_status_staat_geselecteerd(self) -> None:
        """Zonder dit staat de lijst na de swap op 'alles' terwijl je filtert."""
        html = self._render("approved")

        gekozen = html.split('value="approved"', 1)[1].split(">", 1)[0]
        assert "selected" in gekozen, "de gekozen status is niet geselecteerd"

    def test_zonder_keuze_is_er_niets_geselecteerd_behalve_alles(self) -> None:
        html = self._render("")

        for sleutel in ("requested", "approved", "denied"):
            achter = html.split(f'value="{sleutel}"', 1)[1].split(">", 1)[0]
            assert "selected" not in achter, sleutel

    def test_het_filter_staat_binnen_het_geswapte_gebied(self) -> None:
        """Stond het erbuiten, dan hertekent de lijst wel en de keuzelijst niet, en raakt
        de getoonde waarde uit de pas met wat je ziet."""
        html = self._render("approved")

        gebied = html.split('id="approvals-gebied"', 1)[1]
        assert 'name="status"' in gebied, "de keuzelijst valt buiten het geswapte gebied"

    def test_de_telling_zegt_hoeveel_er_verborgen_zijn(self) -> None:
        """Een gefilterde lege pagina is alleen bruikbaar als hij het verschil toont
        tussen 'er is niets' en 'er is niets met deze status'."""
        html = self._render("denied")

        assert "0 van 6" in html
