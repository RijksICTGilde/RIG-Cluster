"""De beheerpagina /admin/approvals toont een aanvraag die GEEN domein is.

De pagina was helemaal op domeinen gebouwd: de kolommen Type / Domein / Naam en een tag
die hardgecodeerd "Domein" of "Subdomein" zei. De goedkeuringsaanvraag van "E-mail
versturen" heeft geen domein, dus die stond er als een lege regel op -- er ontbrak geen
mechanisme, alleen de weergave.

Wat hier wordt vastgelegd is dus precies dat: een dienstaanvraag is herkenbaar, en er valt
nergens meer een hardgecodeerd "Domein" op iets wat er geen is. Plus de twee dingen die
daarvoor moesten kloppen: elk item draagt een ``subject`` van de dienst die het weet, en de
groepering per dienst laat de teller en het statusfilter met rust (die tellen ITEMS).
"""

from types import SimpleNamespace
from typing import Any

import pytest
from opi.core.templates_lotc import templates_lotc
from opi.services.approvals import collect_approval_items
from opi.web.lotc_fixtures import page_data
from opi.web.router_approvals import APPROVAL_STATUSSEN, filter_op_status, groepeer_per_dienst

#: Een project met alle drie de soorten aanvraag die het platform vandaag kent: een domein
#: en een subdomein (publish-on-web) en het gebruik van de dienst zelf (send-email).
PROJECT: dict[str, Any] = {
    "name": "ai1-uit",
    "services": [
        {
            "name": "send-email",
            "config": {
                "from-name": "Robbert Uittenbroek",
                "messages-per-day": 100,
                "approval": {"status": "requested", "history": []},
            },
        }
    ],
    "domains": {
        "allowed-domains": [
            {
                "domain": "voorbeeld.nl",
                "status": "requested",
                "history": [{"date": "2026-08-16T10:00:00+00:00", "status": "requested"}],
            }
        ],
        "allowed-subdomains": [
            {
                "domain": "sandbox.rijksapp.dev",
                "subdomains": [{"name": "mijnapp", "status": "approved", "history": []}],
            }
        ],
    },
}


def _items() -> list[dict[str, Any]]:
    return collect_approval_items(PROJECT)


class TestElkItemZegtWaarHetOverGaat:
    """``subject`` is het gat waar de lege cel doorheen viel: WAT er gevraagd wordt."""

    def test_elk_item_heeft_een_gevulde_label_en_subject(self) -> None:
        for item in _items():
            assert item["label"], item
            assert item["subject"], item

    def test_een_domein_is_zijn_eigen_domeinnaam(self) -> None:
        domein = next(item for item in _items() if item["type"] == "domain")

        assert domein["subject"] == "voorbeeld.nl"

    def test_een_subdomein_is_samengesteld_en_niet_de_twee_losse_velden(self) -> None:
        """Wat wordt aangevraagd is het hele adres; de losse velden blijven voor het
        terugrouteren van het oordeel."""
        sub = next(item for item in _items() if item["type"] == "subdomain")

        assert sub["subject"] == "mijnapp.sandbox.rijksapp.dev"
        assert (sub["domain"], sub["name"]) == ("sandbox.rijksapp.dev", "mijnapp")

    def test_de_mailaanvraag_gaat_over_het_gebruik_van_de_dienst(self) -> None:
        mail = next(item for item in _items() if item["service"] == "send-email")

        assert mail["subject"] == "Gebruik van de dienst"
        assert mail["label"] == "E-mail versturen"

    def test_een_spec_zonder_subject_valt_terug_op_de_bestaande_velden(self) -> None:
        """Zodat een modalsessie die nog van voor deze wijziging loopt niet breekt.
        Hetzelfde is eerder met ``label`` gedaan."""
        from opi.services.catalog.approval import ApprovalSpec, ApproverScope

        gemeten: list[dict[str, Any]] = []

        class NepDienst:
            service_type = SimpleNamespace(value="nep")

            def approval_specs(self) -> list[ApprovalSpec]:
                return [
                    ApprovalSpec(
                        key="nep",
                        label="Nep",
                        approver=ApproverScope.PLATFORM_ADMIN,
                        status_of=lambda data, waarde: None,  # type: ignore[arg-type,return-value]
                        list_items=lambda data: [{"domain": "", "name": "alleen-een-naam"}],
                    )
                ]

        import opi.services.approvals as module

        origineel = module.approval_services
        module.approval_services = lambda: [NepDienst()]  # type: ignore[assignment]
        try:
            gemeten = collect_approval_items({})
        finally:
            module.approval_services = origineel

        assert gemeten[0]["subject"] == "alleen-een-naam"


class TestDeGroepering:
    def test_er_is_een_groep_per_dienst_met_naam_en_icoon_uit_de_registry(self) -> None:
        groepen = groepeer_per_dienst(_items())

        assert [(g["service"], g["naam"], g["icoon"]) for g in groepen] == [
            ("publish-on-web", "Publiceren op het web", "wereldbol"),
            ("send-email", "E-mail versturen", "envelop"),
        ]

    def test_de_soort_tag_staat_er_alleen_als_hij_iets_toevoegt(self) -> None:
        """Bij publiceren op het web scheidt hij domein van subdomein; bij een dienst met
        een enkele soort herhaalt hij alleen de groepskop."""
        groepen = {g["service"]: g for g in groepeer_per_dienst(_items())}

        assert groepen["publish-on-web"]["toon_soort"] is True
        assert groepen["send-email"]["toon_soort"] is False

    def test_geen_aanvraag_raakt_zoek_in_de_groepering(self) -> None:
        items = _items()
        groepen = groepeer_per_dienst(items)

        assert sum(len(g["aanvragen"]) for g in groepen) == len(items)

    def test_een_dienst_die_de_registry_niet_kent_verliest_zijn_rij_niet(self) -> None:
        """Het opschrift van de spec is dan het beste dat er is."""
        groepen = groepeer_per_dienst([{"service": "verdwenen", "type": "x", "label": "Iets", "subject": "s"}])

        assert [(g["naam"], g["icoon"]) for g in groepen] == [("Iets", "")]


class TestDeTellingBlijftOverAANVRAGENGaan:
    """De valkuil van deze taak: met een groepering erbij is het verleidelijk om groepen te
    tellen, en dan klopt "x van y aanvragen" niet meer met de lijst."""

    def test_het_statusfilter_werkt_nog_op_items(self) -> None:
        projecten = [{"project_name": "ai1-uit", "approval_items": _items()}]

        gefilterd = filter_op_status(projecten, "requested")

        onderwerpen = [item["subject"] for item in gefilterd[0]["approval_items"]]
        assert onderwerpen == ["voorbeeld.nl", "Gebruik van de dienst"]

    def test_de_mailaanvraag_valt_weg_bij_een_andere_status(self) -> None:
        projecten = [{"project_name": "ai1-uit", "approval_items": _items()}]

        gefilterd = filter_op_status(projecten, "approved")

        assert [item["subject"] for item in gefilterd[0]["approval_items"]] == ["mijnapp.sandbox.rijksapp.dev"]

    def test_groeperen_gebeurt_NA_filteren(self) -> None:
        """Anders staat er een groepskop boven een tabel zonder rijen."""
        projecten = [{"project_name": "ai1-uit", "approval_items": _items()}]

        gefilterd = filter_op_status(projecten, "approved")
        groepen = groepeer_per_dienst(gefilterd[0]["approval_items"])

        assert [g["service"] for g in groepen] == ["publish-on-web"]


def _render(items: list[dict[str, Any]], status: str = "") -> str:
    projecten = [
        {
            "project_name": "ai1-uit",
            "approval_items": items,
            "approval_groups": groepeer_per_dienst(items),
        }
    ]
    return templates_lotc.env.get_template("bg/admin-approvals.html.j2").render(
        request=SimpleNamespace(cookies={}, url=SimpleNamespace(path="/admin/approvals"), state=SimpleNamespace()),
        projects_data=projecten,
        approvals_totaal=len(items),
        approvals_getoond=len(items),
        approval_status=status,
        approval_statussen=APPROVAL_STATUSSEN,
        navigation={},
        menu_items=[],
    )


class TestWatErOpHetSchermStaat:
    """Gerenderd, want een waarde in de context zegt niets over wat er op de pagina staat."""

    def test_de_mailaanvraag_is_herkenbaar(self) -> None:
        html = _render(_items())

        assert "E-mail versturen" in html
        assert 'name="envelope"' in html, "het icoon van de dienst ontbreekt"
        assert "Gebruik van de dienst" in html
        assert 'text="Aangevraagd"' in html

    def test_er_valt_nergens_een_hardgecodeerd_Domein_op_een_dienstaanvraag(self) -> None:
        """DE fout van deze taak. Alleen de mailaanvraag renderen: dan hoort het woord
        Domein er in geen enkele vorm te staan."""
        mail = [item for item in _items() if item["service"] == "send-email"]

        html = _render(mail)

        assert "Domein" not in html, "een dienstaanvraag wordt nog steeds als domein aangekondigd"
        assert "Subdomein" not in html

    def test_een_domeinaanvraag_houdt_zijn_soort_tag(self) -> None:
        web = [item for item in _items() if item["service"] == "publish-on-web"]

        html = _render(web)

        assert 'text="Domein"' in html
        assert 'text="Subdomein"' in html
        assert "voorbeeld.nl" in html

    def test_de_pagina_noemt_zichzelf_geen_domeinbeheer_meer(self) -> None:
        html = _render(_items())

        assert "Domeinbeheer" not in html
        assert "Aanvragen" in html

    def test_er_staat_geen_lege_cel_meer_onder_de_mailaanvraag(self) -> None:
        """De vorige vorm zette item.domain in een eigen kolom, en die is bij een
        dienstaanvraag leeg. Meten op de KOPPEN: er is geen kolom Domein meer."""
        html = _render(_items())

        assert ">Domein<" not in html, "de kolom Domein staat er nog"

    def test_de_teller_telt_aanvragen_en_geen_groepen(self) -> None:
        html = _render(_items())

        assert "3 van 3" in html


@pytest.mark.parametrize("titel", ["domeinbeheer", "domein- en subdomeingoedkeuring"])
def test_de_domeinnamen_zijn_uit_de_broncode_verdwenen(titel: str) -> None:
    """Een naam die niet meer klopt blijft anders in een hoek staan die niemand rendert.

    ZONDER HOOFDLETTER vergeleken, en ook over de stijlbladen: de eerste vorm van deze
    poort keek alleen naar "Domeinbeheer" met een hoofdletter in .py en .j2, en liet
    daarmee twee achterblijvers staan die allebei in een commentaar zonder hoofdletter
    stonden (opi/web/lotc_switch.py en static/css/admin-approvals.css).
    """
    from pathlib import Path

    wortel = Path(__file__).resolve().parent.parent
    # Per MAP en niet met rglob("opi/**"): dat laatste zoekt vanaf elke tussenmap en loopt
    # dus ook door .venv, met de kans op een treffer in een pakket dat wij niet schrijven.
    bestanden = [
        *(wortel / "opi").rglob("*.py"),
        *(wortel / "opi").rglob("*.j2"),
        *(wortel / "static").rglob("*.css"),
    ]
    treffers = [pad for pad in bestanden if titel in pad.read_text(encoding="utf-8").lower()]

    assert treffers == [], f"{titel} staat nog in: {treffers}"


class TestDeProefopstelling:
    """``/lotc/bg/admin-approvals`` toont dezelfde pagina met verzonnen gegevens.

    Dat is de TWEEDE schrijver van deze context, en die liep uiteen: het sjabloon ging van
    ``approval_items`` naar ``approval_groups`` en de proefopstelling schreef die sleutel
    niet, dus draaide de for-lus nul keer en bleef er een lege tabel over. De
    screenshottest merkte daar niets van (die eist alleen een antwoord), dus staat de toets
    hier: de context van de proefopstelling levert RIJEN op.
    """

    def test_de_context_levert_groepen_uit_dezelfde_functie(self) -> None:
        project = page_data("admin-approvals")["projects_data"][0]

        assert [groep["service"] for groep in project["approval_groups"]] == ["publish-on-web", "send-email"]
        assert sum(len(groep["aanvragen"]) for groep in project["approval_groups"]) == len(project["approval_items"])

    def test_de_proefopstelling_rendert_een_dienstaanvraag_en_geen_lege_tabel(self) -> None:
        html = templates_lotc.env.get_template("bg/admin-approvals.html.j2").render(
            request=SimpleNamespace(cookies={}, url=SimpleNamespace(path="/admin/approvals"), state=SimpleNamespace()),
            navigation={},
            menu_items=[],
            **page_data("admin-approvals"),
        )

        assert html.count("nldd-table-row") > 0, "de proefopstelling toont geen enkele regel"
        assert "E-mail versturen" in html
        assert "Gebruik van de dienst" in html
        assert "voorbeeld.nl" in html
