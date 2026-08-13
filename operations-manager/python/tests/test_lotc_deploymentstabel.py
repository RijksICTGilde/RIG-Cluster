"""De deploymenttabel op het tabblad Overzicht (RC-76).

Het blok "Deployment Status" was een kaart per deployment, elk met zijn eigen
ArgoCD-bevraging. Bij veel deployments is dat onleesbaar en duur. Het is een tabel
geworden, met de status erin.

Deze toetsen meten waar dat op staat of valt:

1. het is een TABEL en geen lijst. NLDD maakt van een tabel een CSS-grid, en zonder het
   attribuut ``columns`` valt alles in EEN kolom: de koppen en de cellen stapelen onder
   elkaar. Dat is de vorige ronde precies zo misgegaan, met een groene browsertest erbij;
   daarom staat er hier een toets op de markup en in tests/e2e/ een op het BEELD.
2. zoeken en sorteren gebeuren op de SERVER (``?q=``, ``?dsort=``), zodat het zonder
   JavaScript werkt en een gefilterde lijst deelbaar is als URL;
3. de statuskolom voegt twee bronnen samen volgens de regel van RC-31/RC-35: een
   dienstbadge neemt de plaats in van precies EEN verdict, de groene "Healthy".
"""

from __future__ import annotations

from typing import Any

from opi.core.templates_lotc import templates_lotc as templates
from opi.web.lotc_switch import (
    DEPLOYMENT_SORTERINGEN,
    build_deployment_status_column,
    deployment_pagina_adres,
    deployment_status_tags,
    filter_lotc_deployments,
    kies_deployment,
)
from starlette.datastructures import QueryParams

TABEL = "bg/_deployments-lijst.html.j2"


class _Verzoek:
    """Genoeg Request om ``filter_lotc_deployments`` te voeden: alleen query_params.

    Een echte Request bouwen vraagt een scope met headers en een client; die functie leest
    er precies een ding uit, en dat expliciet neerzetten leest beter dan een halve
    ASGI-scope die suggereert dat de rest meedoet.
    """

    def __init__(self, query: str = "") -> None:
        self.query_params = QueryParams(query)
        self.url = _Url("/projects/demo/details", query)


class _Url:
    """Het stukje URL dat deployment_pagina_adres leest: het pad en de querystring."""

    def __init__(self, path: str, query: str = "") -> None:
        self.path = path
        self.query = query


def _deployments(*namen: str) -> list[dict[str, Any]]:
    return [{"name": naam, "cluster": "odcn-production", "components": []} for naam in namen]


def _filter(query: str, deployments: list[dict[str, Any]], deployment_open: str = "") -> dict[str, Any]:
    return filter_lotc_deployments(_Verzoek(query), deployments, deployment_open)


# ---------------------------------------------------------------- zoeken en sorteren


def test_zoeken_laat_de_rijen_over_die_de_naam_dragen() -> None:
    resultaat = _filter("q=pr-12", _deployments("pr-12", "pr-120", "productie"))

    assert [d["name"] for d in resultaat["deployments_zichtbaar"]] == ["pr-12", "pr-120"]


def test_zoeken_kijkt_ook_naar_het_cluster() -> None:
    """Het cluster staat in de tabel omdat het anders identieke namen onderscheidt; dan
    hoort het ook doorzoekbaar te zijn."""
    deployments = _deployments("een", "twee")
    deployments[1]["cluster"] = "sandboxed-local"

    resultaat = _filter("q=sandboxed", deployments)

    assert [d["name"] for d in resultaat["deployments_zichtbaar"]] == ["twee"]


def test_de_telling_kent_het_totaal_naast_wat_er_staat() -> None:
    """ "Totaal: 1 van 3" - zonder het totaal zegt een gefilterde lijst niet hoeveel er is."""
    resultaat = _filter("q=productie", _deployments("pr-1", "pr-2", "productie"))

    assert len(resultaat["deployments_zichtbaar"]) == 1
    assert len(resultaat["deployments_alle"]) == 3


def test_sorteren_op_naam_aflopend() -> None:
    resultaat = _filter("dsort=naam-af", _deployments("a", "c", "b"))

    assert [d["name"] for d in resultaat["deployments_zichtbaar"]] == ["c", "b", "a"]


def test_sorteren_op_meeste_componenten() -> None:
    deployments = _deployments("weinig", "veel")
    deployments[1]["components"] = [{"reference": "een"}, {"reference": "twee"}]

    resultaat = _filter("dsort=componenten", deployments)

    assert [d["name"] for d in resultaat["deployments_zichtbaar"]] == ["veel", "weinig"]


def test_een_onbekende_sortering_valt_terug_op_de_eerste() -> None:
    """Een sleutel uit een oude of geknutselde URL mag geen 500 opleveren."""
    resultaat = _filter("dsort=bestaat-niet", _deployments("b", "a"))

    assert [d["name"] for d in resultaat["deployments_zichtbaar"]] == ["a", "b"]
    # En de onbekende sleutel gaat NIET terug de pagina in: hij zou in het verborgen veld
    # en in hx-push-url een sortering beloven die niet gebruikt wordt.
    assert resultaat["deployment_sort"] == "naam"


def test_zoeken_en_sorteren_werken_samen() -> None:
    """Zoeken mag de sortering niet wegvagen: beide staan in dezelfde URL."""
    resultaat = _filter("q=pr-&dsort=naam-af", _deployments("pr-1", "pr-2", "productie"))

    assert [d["name"] for d in resultaat["deployments_zichtbaar"]] == ["pr-2", "pr-1"]


def test_de_sorteersleutel_van_de_projectenlijst_wordt_niet_geleend() -> None:
    """``?sort=`` is van de projectenlijst; deze lijst luistert naar ``?dsort=``.

    Beide namen kunnen in een URL staan zodra iemand een link deelt, en dan mag de ene
    lijst de sortering van de andere niet oppakken.
    """
    resultaat = _filter("sort=naam-af", _deployments("a", "b"))

    assert [d["name"] for d in resultaat["deployments_zichtbaar"]] == ["a", "b"]


def test_elke_sortering_uit_het_menu_werkt() -> None:
    """Een sleutel in het menu die niets sorteert is een dode knop."""
    for sleutel, _label, _fn in DEPLOYMENT_SORTERINGEN:
        resultaat = _filter(f"dsort={sleutel}", _deployments("b", "a"))
        assert len(resultaat["deployments_zichtbaar"]) == 2, f"sortering {sleutel} verliest rijen"


# ------------------------------------------------------------ welke deployment open staat
#
# Sinds RC-92 staat de naam in het PAD (/projects/<project>/deployments/<naam>) en niet in
# ``?deployment=``. De keuze wordt daarom door de ROUTE gemaakt, met kies_deployment(), en
# komt hier binnen; filter_lotc_deployments zoekt er alleen de deployment bij.


def test_de_keuze_uit_het_pad_wordt_de_geopende_deployment() -> None:
    resultaat = _filter("", _deployments("eerste", "tweede"), "tweede")

    assert resultaat["deployment_open"] == "tweede"
    assert resultaat["deployment_geopend"]["name"] == "tweede"


def test_een_naam_die_niet_bestaat_levert_geen_paneel_op() -> None:
    """De route verwijst zo'n adres door; komt hij hier toch binnen, dan is er niets open
    in plaats van een halve pagina over een deployment die er niet is."""
    resultaat = _filter("", _deployments("eerste"), "weg")

    assert resultaat["deployment_geopend"] is None


def test_een_project_zonder_deployments_heeft_niets_open() -> None:
    resultaat = _filter("", [], "")

    assert resultaat["deployment_open"] == ""
    assert resultaat["deployment_geopend"] is None
    assert resultaat["deployments_zichtbaar"] == []


def test_de_eerste_voorkeur_die_bestaat_wint() -> None:
    """Het pad gaat voor de oude ``?deployment=``-vorm; die volgorde geeft de route mee."""
    assert kies_deployment(["eerste", "tweede"], "tweede", "eerste") == "tweede"


def test_een_verwijderde_deployment_valt_terug_op_de_eerste_op_naam() -> None:
    """Een gedeelde link kan een deployment noemen die niet meer bestaat; dan hoort er een
    pagina te staan en geen lege of kapotte."""
    assert kies_deployment(["tweede", "eerste"], "weg") == "eerste"


def test_zonder_voorkeur_opent_de_eerste_op_naam() -> None:
    assert kies_deployment(["tweede", "eerste"]) == "eerste"


def test_zonder_deployments_valt_er_niets_te_openen() -> None:
    assert kies_deployment([], "weg") == ""


def test_het_adres_zet_de_naam_in_het_pad() -> None:
    verzoek = _Verzoek("")
    verzoek.url = _Url("/projects/demo/deployments")

    assert deployment_pagina_adres(verzoek, "demo", "tweede") == "/projects/demo/deployments/tweede"


def test_het_adres_houdt_het_tabblad_vast() -> None:
    verzoek = _Verzoek("")
    verzoek.url = _Url("/projects/demo/metrics")

    assert deployment_pagina_adres(verzoek, "demo", "tweede") == "/projects/demo/metrics/tweede"


def test_de_oude_parameter_verdwijnt_uit_het_adres() -> None:
    """``?deployment=<naam>`` was de vorige vorm. Hij mag niet naast het pad blijven
    bestaan, anders zijn er twee adressen voor dezelfde pagina."""
    verzoek = _Verzoek("deployment=tweede&q=pr")
    verzoek.url = _Url("/projects/demo/deployments", "deployment=tweede&q=pr")

    assert deployment_pagina_adres(verzoek, "demo", "tweede") == "/projects/demo/deployments/tweede?q=pr"


def test_een_project_zonder_deployments_krijgt_het_kale_adres() -> None:
    verzoek = _Verzoek("")
    verzoek.url = _Url("/projects/demo/deployments/weg")

    assert deployment_pagina_adres(verzoek, "demo", "") == "/projects/demo/deployments"


# --------------------------------------------------------------------- de statuskolom


class _Stand:
    """Wat ``DeploymentState`` aan de statuskolom levert, meer niet."""

    def __init__(self, vervangend: list[str] | None = None, begeleidend: list[str] | None = None) -> None:
        self.replacing_badges = vervangend or []
        self.accompanying_badges = begeleidend or []


def test_de_status_toont_gezondheid_en_sync_uit_argocd() -> None:
    tags = deployment_status_tags(
        replacing_badges=[], accompanying_badges=[], argocd={"available": True, "health": "Healthy", "sync": "Synced"}
    )

    assert [tag["label"] for tag in tags] == ["Healthy", "Synced"]
    assert [tag["type"] for tag in tags] == ["success", "success"]


def test_een_dienstbadge_neemt_de_plaats_in_van_een_groene_healthy() -> None:
    """RC-31/RC-35: nul replicas noemt ArgoCD Healthy, en die groene badge is dan onwaar."""
    tags = deployment_status_tags(
        replacing_badges=["slaapstand"],
        accompanying_badges=[],
        argocd={"available": True, "health": "Healthy", "sync": "Synced"},
    )

    assert [tag["label"] for tag in tags] == ["slaapstand", "Synced"]


def test_een_dienstbadge_verbergt_geen_echte_storing() -> None:
    """Degraded heeft ArgoCD echt waargenomen; die badge blijft naast de dienstbadge."""
    tags = deployment_status_tags(
        replacing_badges=["uitgeschakeld"],
        accompanying_badges=[],
        argocd={"available": True, "health": "Degraded", "sync": "Synced"},
    )

    assert [tag["label"] for tag in tags] == ["uitgeschakeld", "Degraded", "Synced"]


def test_sync_unknown_is_rood_en_niet_neutraal() -> None:
    """Unknown betekent dat ArgoCD niet kan vergelijken - vaak een renderfout. Grijs zou
    lezen als "nog mee bezig"."""
    tags = deployment_status_tags(
        replacing_badges=[], accompanying_badges=[], argocd={"available": True, "health": "Healthy", "sync": "Unknown"}
    )

    assert next(tag for tag in tags if tag["label"] == "Unknown")["type"] == "error"


def test_een_deployment_die_argocd_niet_kent_zegt_dat() -> None:
    tags = deployment_status_tags(replacing_badges=[], accompanying_badges=[], argocd={"available": False})

    assert [tag["label"] for tag in tags] == ["Niet in ArgoCD"]


def test_zonder_argocd_staan_alleen_de_dienstbadges_er() -> None:
    """Is ArgoCD niet verbonden, dan zegt de pagina dat in gewone taal. Een verzonnen
    "Unknown" per rij zou daar overheen schreeuwen."""
    tags = deployment_status_tags(replacing_badges=["slaapstand"], accompanying_badges=[], argocd=None)

    assert [tag["label"] for tag in tags] == ["slaapstand"]


def test_de_kolom_wordt_voor_elke_deployment_gebouwd() -> None:
    kolom = build_deployment_status_column(
        _deployments("een", "twee"),
        {"een": _Stand(vervangend=["slaapstand"])},
        {"een": {"available": True, "health": "Healthy", "sync": "Synced"}},
    )

    assert [tag["label"] for tag in kolom["een"]] == ["slaapstand", "Synced"]
    # "twee" kent geen stand en geen ArgoCD-antwoord; dan hoort er een lege lijst te
    # staan en geen KeyError bij het renderen.
    assert kolom["twee"] == []


# ------------------------------------------------------------------- de tabel zelf


def _render(query: str, deployments: list[dict[str, Any]], **extra: Any) -> str:
    context = _filter(query, deployments)
    return templates.env.get_template(TABEL).render(
        {
            "project": {"name": "demo"},
            "deployment_status_tags": {},
            "deployment_argocd": {},
            **context,
            **extra,
        }
    )


def test_de_tabel_is_een_tabel_en_geen_stapel() -> None:
    """DE TOETS OP DE VORIGE FOUT.

    NLDD maakt van een tabel een CSS-grid met ``grid-template-columns: var(--_columns)``,
    en dat wordt ``none`` als het attribuut ``columns`` ontbreekt: alles valt in EEN kolom.
    De vorige ronde leverde daardoor een tabel op die als lijst rendeerde, met een groene
    browsertest ernaast. Deze toets meet het attribuut; tests/e2e/ meet het beeld.
    """
    html = _render("", _deployments("eerste", "tweede"))

    assert "<nldd-table" in html, "een lijst van panelen is geen tabel"
    tabel = html.split("<nldd-table", 1)[1].split(">", 1)[0]
    assert "columns=" in tabel, "zonder columns stapelt NLDD alle cellen in een kolom"
    # Evenveel breedtes als koppen, anders schuift de laatste kolom eruit.
    breedtes = tabel.split('columns="', 1)[1].split('"', 1)[0].split()
    assert len(breedtes) == html.count('data-lotc-component="th"')


def test_de_koprij_staat_er_een_keer() -> None:
    html = _render("", _deployments("eerste", "tweede"))

    assert html.count('slot="header"') == 1


def test_de_tabel_toont_elke_deployment_met_naam_en_cluster() -> None:
    html = _render("", _deployments("eerste", "tweede"))

    assert "eerste" in html
    assert "tweede" in html
    assert "odcn-production" in html


def test_de_rij_wijst_naar_het_tabblad_deployments() -> None:
    """De tabel is de ingang; het detail staat op het tabblad Deployments."""
    html = _render("", _deployments("eerste", "tweede"))

    assert "/projects/demo/deployments/tweede" in html


def test_de_statuskolom_staat_in_de_rij() -> None:
    html = _render(
        "",
        _deployments("productie"),
        deployment_status_tags={"productie": [{"label": "slaapstand", "type": "info"}]},
    )

    assert "slaapstand" in html


def test_de_laatste_sync_staat_in_de_rij() -> None:
    html = _render(
        "",
        _deployments("productie"),
        deployment_argocd={"productie": {"available": True, "last_sync": "2026-08-12T10:00:00+00:00"}},
    )

    assert "2026" in html


def test_de_telling_staat_boven_de_tabel() -> None:
    html = _render("q=pr-", _deployments("pr-1", "pr-2", "productie"))

    assert "2 deployments" in html
    assert "van 3" in html


def test_een_zoekterm_zonder_treffers_zegt_dat() -> None:
    html = _render("q=bestaat-niet", _deployments("eerste"))

    assert "Geen deployments gevonden" in html
    assert "bestaat-niet" in html


def test_een_leeg_project_krijgt_geen_telling_en_geen_nul_melding() -> None:
    """ "Totaal: 0 deployments" boven een leeg project is ruis; de melding eronder zegt
    wat je eraan kunt doen. Twee keer hetzelfde melden is de fout die dit voorkomt."""
    html = _render("", [])

    assert "Totaal" not in html
    assert "Geen deployments gevonden" not in html
