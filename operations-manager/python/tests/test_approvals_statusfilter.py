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


class TestHetFiltermenu:
    """Gerenderd, want een waarde in de context zegt niets over wat er op het scherm staat.

    De vorm is die van het sorteren op /projects: een toolbar-knop met een uitklapmenu, als
    kale nldd-markup. Hier stond eerst een kaal <c-select> in een <c-cluster>, en dat stond
    naast de projectenpagina als iets uit een andere applicatie. De les die deze tests
    vasthouden is niet "gebruik nldd-menu-item" maar: het patroon stond er al, twintig regels
    verderop.
    """

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

    def test_elke_status_staat_in_het_menu(self) -> None:
        html = self._render("")

        for sleutel, label in APPROVAL_STATUSSEN:
            assert f'text="{label}"' in html, label
            if sleutel:
                assert f'href="/admin/approvals?status={sleutel}"' in html, sleutel

    def test_de_items_dragen_hun_bestemming_twee_keer(self) -> None:
        """href zodat een gefilterde lijst een deelbare URL is, en hx-get omdat een
        nldd-menu-item niet uit zichzelf op zijn href navigeert."""
        html = self._render("")

        item = html.split('text="Goedgekeurd"', 1)[1].split("</nldd-menu-item>", 1)[0]
        assert 'href="/admin/approvals?status=approved"' in item
        assert 'hx-get="/admin/approvals?status=approved"' in item
        assert 'hx-target="#approvals-gebied"' in item

    def test_de_gekozen_status_staat_in_het_KNOPLABEL(self) -> None:
        """Dat is wat je ziet zonder het menu te openen; zonder dit lijkt het filter uit."""
        assert 'text="Status: Goedgekeurd"' in self._render("approved")
        assert 'text="Status: Alle statussen"' in self._render("")

    def test_de_gekozen_status_staat_aangevinkt_in_het_menu(self) -> None:
        """Zonder dit staat het menu na de swap op 'alles' terwijl je filtert."""
        html = self._render("approved")

        item = html.split('text="Goedgekeurd"', 1)[1].split(">", 1)[0]
        assert "selected" in item, "de gekozen status is niet aangevinkt"

    def test_zonder_keuze_is_er_niets_aangevinkt_behalve_alles(self) -> None:
        html = self._render("")

        for label in ("Aangevraagd", "Goedgekeurd", "Afgewezen"):
            item = html.split(f'text="{label}"', 1)[1].split(">", 1)[0]
            assert "selected" not in item, label

    def test_elke_status_staat_er_maar_EEN_keer(self) -> None:
        """Er stond een overloopmenu naast de knop in plaats van ervoor in de plaats, en
        dan staat het hele filter dubbel op het scherm: als knop en als hamburgermenu."""
        html = self._render("")

        for label in ("Aangevraagd", "Goedgekeurd", "Afgewezen"):
            assert html.count(f'text="{label}"') == 1, f"{label} staat er dubbel"

    def test_het_filter_staat_binnen_het_geswapte_gebied(self) -> None:
        """Stond het erbuiten, dan hertekent de lijst wel en de keuzelijst niet, en raakt
        de getoonde waarde uit de pas met wat je ziet."""
        html = self._render("approved")

        gebied = html.split('id="approvals-gebied"', 1)[1]
        assert "nldd-menu-item" in gebied, "het filtermenu valt buiten het geswapte gebied"

    def test_de_telling_zegt_hoeveel_er_verborgen_zijn(self) -> None:
        """Een gefilterde lege pagina is alleen bruikbaar als hij het verschil toont
        tussen 'er is niets' en 'er is niets met deze status'."""
        html = self._render("denied")

        assert "0 van 6" in html
