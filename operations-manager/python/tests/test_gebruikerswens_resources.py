"""Een handmatig gezette resource-waarde wint van de auto-tuner (RC-141).

Waarom dit bestand bestaat
--------------------------
Op 19 augustus wijzigde iemand via de portal de CPU van een component. Die wijziging is
netjes gecommit en heeft nooit gewerkt: er waren twee schrijvers die naar twee niveaus
schreven. De portal schreef in ``components[]`` (de catalogus), de tuner in
``deployments[].components[].resources`` (de werkkopie), en manifestgeneratie legt de
werkkopie OVER de catalogus heen. Zodra de tuner een component ooit had aangeraakt was
elke bewerking daarna dus een stille no-op.

Wat hier bewaakt wordt is die keten, niet de losse functies:

* wat de gebruiker zet komt in BEIDE deployments terecht, terwijl de getunede waarde die
  hij NIET aanraakte gewoon blijft staan;
* een bewerking die niets wijzigt laat ook niets achter (de modal post altijd alle vier
  de velden -- zonder die diff legt elke bewerking de tuner volledig lam);
* er is nog steeds maar EEN schrijver, afgedwongen over de AST;
* de tuner respecteert een levende wens, pakt een vervallen wens weer op, en tilt bij een
  actieve OOM de geheugenlimiet toch omhoog.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.connectors.vpa import VpaContainerRecommendation
from opi.handlers.project_file_handler import ProjectFileHandler
from opi.services.resource_tuning_service import apply_resource_tuning

OPI_DIR = Path(__file__).resolve().parent.parent / "opi"


def _project(
    *,
    component_resources: dict[str, Any] | None = None,
    overrides: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Een component met twee deployments, elk met zijn eigen tuner-override."""
    overrides = overrides or {}
    return {
        "schema-version": 2,
        "name": "wens",
        "components": [
            {
                "name": "api",
                "type": "single",
                "resources": component_resources
                or {
                    "requests": {"memory": "64Mi", "cpu": "32m"},
                    "limits": {"memory": "128Mi", "cpu": "200m"},
                },
            }
        ],
        "deployments": [
            {
                "name": dep_name,
                "cluster": "odcn-production",
                "namespace": f"wens-{dep_name}",
                "components": [{"reference": "api", "image": "nginx:1.25", **({"resources": res} if res else {})}],
            }
            for dep_name, res in (
                ("productie", overrides.get("productie")),
                ("acceptatie", overrides.get("acceptatie")),
            )
        ],
    }


def _effectieve_resources(project_data: dict[str, Any], deployment: str, component: str) -> dict[str, str]:
    """De merge die manifestgeneratie doet: catalogus eerst, deployment-override erover.

    Dezelfde twee aanroepen in dezelfde volgorde als ``ProjectManager`` gebruikt bij het
    genereren van de deployment-manifesten; ``test_de_merge_van_manifestgeneratie_ziet_er_nog_zo_uit``
    hieronder bewaakt dat die vorm daar niet stilletjes verandert.
    """
    handler = ProjectFileHandler()
    resources = handler.extract_component_resources(project_data, component)
    overrides = handler.extract_deployment_component_resources(project_data, deployment, component)
    if overrides:
        resources.update(overrides)
    return resources


# ---------------------------------------------------------------------------
# 1. De bewerking komt aan in beide deployments, de tuning blijft staan
# ---------------------------------------------------------------------------


def test_een_cpu_bewerking_wint_van_de_tuner_in_beide_deployments() -> None:
    """Het scenario van 19 augustus, in beide deployments, met de tuning intact."""
    project = _project(
        overrides={
            "productie": {
                "requests": {"memory": "300Mi", "cpu": "32m"},
                "limits": {"memory": "600Mi", "cpu": "1000m"},
                "history": [{"timestamp": "2026-08-01T01:00:00+00:00", "source": "auto-tune"}],
            },
            "acceptatie": {
                "requests": {"memory": "100Mi", "cpu": "32m"},
                "limits": {"memory": "200Mi", "cpu": "1000m"},
            },
        }
    )

    gewijzigd = ProjectFileHandler().apply_user_resource_intent(
        project, "api", {"requests_cpu": "50m", "limits_cpu": "1"}, origin="portal"
    )

    assert gewijzigd == ["limits_cpu", "requests_cpu"]
    for deployment, geheugen in (("productie", ("300Mi", "600Mi")), ("acceptatie", ("100Mi", "200Mi"))):
        effectief = _effectieve_resources(project, deployment, "api")
        assert effectief["requests_cpu"] == "50m", deployment
        assert effectief["limits_cpu"] == "1", deployment
        # De getunede geheugenwaarden van DIT deployment blijven ongemoeid: een
        # CPU-bewerking mag de werkkopie van het geheugen niet meeslepen.
        assert (effectief["requests_memory"], effectief["limits_memory"]) == geheugen, deployment

    # De historie van de tuner blijft staan; alleen de CPU-velden zijn weg.
    productie = project["deployments"][0]["components"][0]
    assert productie["resources"]["history"][0]["source"] == "auto-tune"
    assert "cpu" not in productie["resources"]["requests"]
    assert "cpu" not in productie["resources"]["limits"]

    # En de wens zelf staat als 'manual' op de catalogus, zonder deployment-veld:
    # hij geldt voor elk deployment van dit component.
    entry = project["components"][0]["resources"]["history"][0]
    assert entry["source"] == "manual"
    assert "deployment" not in entry
    assert entry["limits"] == {"cpu": "1"}
    assert entry["requests"] == {"cpu": "50m"}


def test_een_override_die_helemaal_leegloopt_laat_geen_lege_blokken_achter() -> None:
    """Blijft er niets van een override over, dan gaat het hele blok weg."""
    project = _project(
        overrides={"productie": {"requests": {"cpu": "32m"}, "limits": {"cpu": "1000m"}}},
    )

    ProjectFileHandler().apply_user_resource_intent(
        project, "api", {"requests_cpu": "50m", "limits_cpu": "1"}, origin="portal"
    )

    assert "resources" not in project["deployments"][0]["components"][0]


def test_de_merge_van_manifestgeneratie_ziet_er_nog_zo_uit() -> None:
    """De test hierboven bootst de merge na; deze pint dat die merge nog zo werkt.

    Verdwijnt de ``update`` uit ``project_manager``, dan meet ``_effectieve_resources``
    iets dat de productiecode niet meer doet en bewijst de test hierboven niets.
    """
    bron = (OPI_DIR / "manager" / "project_manager.py").read_text(encoding="utf-8")
    assert "component_resources.update(deployment_resources)" in bron


# ---------------------------------------------------------------------------
# 2. Een bewerking die niets wijzigt laat niets achter
# ---------------------------------------------------------------------------


def test_dezelfde_waarden_opnieuw_opslaan_verandert_niets() -> None:
    """De modal post alle vier de velden bij elke opslag.

    Zonder de diff zou elke willekeurige componentbewerking een wens op alle vier de
    velden vastleggen en de tuner volledig lamleggen.
    """
    override = {"requests": {"memory": "300Mi"}, "limits": {"memory": "600Mi"}}
    project = _project(overrides={"productie": dict(override)})
    voor = ProjectFileHandler().extract_component_resources(project, "api")

    gewijzigd = ProjectFileHandler().apply_user_resource_intent(project, "api", voor, origin="portal")

    assert gewijzigd == []
    assert "history" not in project["components"][0]["resources"]
    assert project["deployments"][0]["components"][0]["resources"] == override


# ---------------------------------------------------------------------------
# 2b. Een tweede bewerking laat de eerste wens staan
# ---------------------------------------------------------------------------


def test_een_tweede_bewerking_op_een_ander_veld_laat_de_eerste_wens_staan() -> None:
    """De gewone flow, geen randgeval: twee bewerkingen achter elkaar.

    De lezer (``get_user_resource_intent``) neemt per niveau precies het NIEUWSTE
    ``manual``-item. Draagt dat item alleen de velden van die ene bewerking, dan telt de
    wens van de vorige bewerking niet meer mee en mag de tuner die waarde de eerstvolgende
    nacht weer verzetten -- precies de stille no-op die dit schrijfpad moet voorkomen.
    Dus draagt het nieuwste item altijd de VOLLEDIGE staande wens.
    """
    handler = ProjectFileHandler()
    project = _project()

    assert handler.apply_user_resource_intent(project, "api", {"limits_cpu": "1"}, origin="portal") == ["limits_cpu"]
    assert handler.apply_user_resource_intent(project, "api", {"limits_memory": "900Mi"}, origin="portal") == [
        "limits_memory"
    ]

    wens = handler.get_user_resource_intent(project, "productie", "api")
    assert wens is not None
    assert wens.fields == {"limits_cpu": "1", "limits_memory": "900Mi"}, (
        "de CPU-wens uit de eerste bewerking moet blijven meetellen"
    )

    # Het nieuwste item draagt beide velden; het oudere item blijft gewoon in de historie.
    historie = project["components"][0]["resources"]["history"]
    assert historie[0]["limits"] == {"cpu": "1", "memory": "900Mi"}
    assert historie[1]["limits"] == {"cpu": "1"}
    assert "still standing" in historie[0]["reason"]


def test_een_veld_dat_niet_meer_in_de_catalogus_staat_gaat_niet_mee() -> None:
    """Een wens staat alleen zolang de catalogus hem nog draagt.

    Is de waarde langs een andere weg gewijzigd, dan is het geen staande wens meer en
    mag hij niet stilzwijgend meeliften op het volgende item.
    """
    handler = ProjectFileHandler()
    project = _project()
    handler.apply_user_resource_intent(project, "api", {"limits_cpu": "1"}, origin="portal")

    # Langs een andere weg gewijzigd (de sectiestroom over de hele componentenlijst
    # legt geen wens vast, zie features/handmatig-gezette-resources.md).
    project["components"][0]["resources"]["limits"]["cpu"] = "400m"

    handler.apply_user_resource_intent(project, "api", {"limits_memory": "900Mi"}, origin="portal")

    wens = handler.get_user_resource_intent(project, "productie", "api")
    assert wens is not None
    assert wens.fields == {"limits_memory": "900Mi"}


def test_een_tweede_bewerking_ruimt_ook_de_override_van_de_staande_wens_op() -> None:
    """Een wens die het manifest niet haalt is geen wens.

    Heeft de tuner na het vervallen van een wens weer een override op dat veld gezet, dan
    haalt de volgende bewerking die override ook weg -- anders draagt het nieuwste item
    een wens die bij manifestgeneratie alsnog verliest.
    """
    handler = ProjectFileHandler()
    project = _project()
    handler.apply_user_resource_intent(project, "api", {"limits_cpu": "1"}, origin="portal")

    # De tuner pakte het veld weer op en zette een override terug.
    project["deployments"][0]["components"][0]["resources"] = {"limits": {"cpu": "250m", "memory": "600Mi"}}

    handler.apply_user_resource_intent(project, "api", {"limits_memory": "900Mi"}, origin="portal")

    effectief = _effectieve_resources(project, "productie", "api")
    assert effectief["limits_cpu"] == "1"
    assert effectief["limits_memory"] == "900Mi"


# ---------------------------------------------------------------------------
# 3. Er is maar EEN schrijver
# ---------------------------------------------------------------------------


def _aanroepers_van(namen: set[str]) -> dict[str, set[str]]:
    """Per gezochte functienaam de functies onder ``opi/`` die hem aanroepen.

    Over de AST en niet met een grep: een grep telt de naam in een docstring of in een
    commentaarregel mee, en dan bewaakt de grendel iets anders dan hij zegt.
    """
    gevonden: dict[str, set[str]] = {naam: set() for naam in namen}
    for pad in sorted(OPI_DIR.rglob("*.py")):
        boom = ast.parse(pad.read_text(encoding="utf-8"))
        for knoop in ast.walk(boom):
            if not isinstance(knoop, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for binnen in ast.walk(knoop):
                if not isinstance(binnen, ast.Call):
                    continue
                func = binnen.func
                naam = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
                if naam in namen:
                    gevonden[naam].add(knoop.name)
    return gevonden


def test_er_is_maar_een_schrijver_van_een_gebruikerswens() -> None:
    """``apply_resource_limits`` en ``set_component_resources`` zijn de lage schrijvers.

    Ze mogen alleen nog vanuit ``apply_user_resource_intent`` gedraaid worden. Komt er een
    tweede aanroeper bij, dan schrijft die weer naar de catalogus zonder de override op te
    ruimen -- en dan is de bewerking weer een stille no-op zodra de tuner iets heeft gezet.
    """
    aanroepers = _aanroepers_van({"apply_resource_limits", "set_component_resources"})

    for naam, plaatsen in aanroepers.items():
        assert plaatsen == {"apply_user_resource_intent"}, (
            f"{naam} wordt aangeroepen vanuit {sorted(plaatsen)}; laat de wijziging via "
            f"ProjectFileHandler.apply_user_resource_intent lopen, anders wint de "
            f"deployment-override van de gebruiker."
        )


# ---------------------------------------------------------------------------
# 4. Snoeien gooit de wens niet weg
# ---------------------------------------------------------------------------


def test_snoeien_houdt_zowel_de_handmatige_als_de_oom_entry() -> None:
    """Vijf verse auto-tune-items mogen de twee beschermde items niet wegdrukken."""
    handler = ProjectFileHandler()
    project = _project(
        component_resources={
            "requests": {"memory": "64Mi"},
            "limits": {"memory": "128Mi"},
            "history": [
                {"timestamp": "2026-08-02T01:00:00+00:00", "source": "manual", "limits": {"cpu": "1"}},
                {"timestamp": "2026-08-01T01:00:00+00:00", "source": "oom-watcher", "limits": {"memory": "512Mi"}},
            ],
        }
    )

    for i in range(5):
        handler.append_component_resource_history(
            project,
            "api",
            {"timestamp": f"2026-08-1{i}T01:00:00+00:00", "source": "auto-tune", "limits": {"memory": f"{128 + i}Mi"}},
        )

    historie = project["components"][0]["resources"]["history"]
    assert len(historie) == 5, "max_entries blijft hard"
    bronnen = [entry["source"] for entry in historie]
    assert bronnen.count("manual") == 1
    assert bronnen.count("oom-watcher") == 1
    # En ze zijn allebei nog vindbaar voor de lezers die erop rekenen.
    assert handler.get_user_resource_intent(project, "productie", "api") is not None
    assert handler.get_resource_history_floor(project, "productie", "api") is None  # entry zonder deployment-veld


def test_snoeien_redt_de_wens_ook_als_het_venster_vol_beschermde_items_staat() -> None:
    """Een OOM-storm mag de wens niet alsnog uit het venster duwen.

    Zat de redding in het VERVANGEN van een niet-beschermd item, dan vindt hij in een
    venster dat uitsluitend uit ``oom-watcher``-items bestaat geen vrij slot en valt de
    wens er toch uit. De slots worden daarom vooraf gereserveerd.
    """
    handler = ProjectFileHandler()
    project = _project(
        component_resources={
            "limits": {"memory": "128Mi"},
            "history": [{"timestamp": "2026-08-01T01:00:00+00:00", "source": "manual", "limits": {"cpu": "1"}}],
        }
    )

    for i in range(5):
        handler.append_component_resource_history(
            project,
            "api",
            {
                "timestamp": f"2026-08-1{i}T01:00:00+00:00",
                "source": "oom-watcher",
                "deployment": "productie",
                "limits": {"memory": f"{512 + i}Mi"},
            },
        )

    historie = project["components"][0]["resources"]["history"]
    assert len(historie) == 5, "max_entries blijft hard"
    assert [entry["source"] for entry in historie].count("manual") == 1
    assert handler.get_user_resource_intent(project, "productie", "api") is not None
    assert handler.get_resource_history_floor(project, "productie", "api") is not None


def test_comprimeren_vouwt_een_handmatige_entry_niet_weg() -> None:
    """``_compact_resource_history_list`` vouwt alleen runs van gelijke auto-tune-items."""
    handler = ProjectFileHandler()
    gelijk = {"limits": {"memory": "128Mi"}, "requests": {"memory": "64Mi"}}
    project = _project(
        component_resources={
            "history": [
                {"timestamp": "2026-08-05T01:00:00+00:00", "source": "auto-tune", **gelijk},
                {"timestamp": "2026-08-04T01:00:00+00:00", "source": "manual", **gelijk},
                {"timestamp": "2026-08-03T01:00:00+00:00", "source": "auto-tune", **gelijk},
            ]
        }
    )

    handler.compact_resource_history(project, max_entries=5)

    bronnen = [entry["source"] for entry in project["components"][0]["resources"]["history"]]
    assert bronnen == ["auto-tune", "manual", "auto-tune"]


# ---------------------------------------------------------------------------
# 5-7. De tuner tegenover een wens
# ---------------------------------------------------------------------------


def _tuner_project(intent_timestamp: str, *, intent_velden: dict[str, dict[str, str]]) -> dict[str, Any]:
    """Een component met een wens in de historie en verder niets vastgezet."""
    return {
        "schema-version": 2,
        "name": "wens",
        "components": [
            {
                "name": "api",
                "type": "single",
                "resources": {
                    "requests": {"memory": "64Mi", "cpu": "50m"},
                    "limits": {"memory": "128Mi", "cpu": "1"},
                    "history": [{"timestamp": intent_timestamp, "source": "manual", **intent_velden}],
                },
            }
        ],
        "deployments": [
            {
                "name": "productie",
                "cluster": "odcn-production",
                "namespace": "wens",
                "components": [{"reference": "api", "image": "nginx:1.25"}],
            }
        ],
    }


async def _draai_tuner(
    project: dict[str, Any],
    *,
    max_mb: float,
    oom: bool = False,
    vpa_cpu_m: float | None = None,
    vpa_memory_mi: float = 300.0,
) -> list[dict[str, str]]:
    """Draai de tuner over een project met gemodelleerde meetdata."""
    connector = AsyncMock()
    meting = [{"value": [0, str(int(max_mb * 1024 * 1024))]}]
    connector.custom_query.side_effect = [meting, meting, [{"value": [0, "1"]}] if oom else []]
    vpa = (
        VpaContainerRecommendation(
            container_name="app",
            target_cpu_m=vpa_cpu_m,
            target_memory_mi=vpa_memory_mi,
            lower_cpu_m=vpa_cpu_m,
            lower_memory_mi=vpa_memory_mi,
            upper_cpu_m=vpa_cpu_m,
            upper_memory_mi=vpa_memory_mi,
        )
        if vpa_cpu_m is not None
        else None
    )
    kubectl = MagicMock(
        get_deployment_conditions=AsyncMock(return_value=None),
        get_vpa_recommendation=AsyncMock(return_value=vpa),
    )

    with (
        patch("opi.services.resource_tuning_service.KubectlConnector", return_value=kubectl),
        patch("opi.services.resource_tuning_service.supports_vpa", return_value=vpa is not None),
        patch("opi.services.resource_tuning_service.get_prefixed_namespace", return_value="rig-prd-wens"),
        patch("opi.services.resource_tuning_service.get_metrics_connector", new=AsyncMock(return_value=connector)),
    ):
        changes, _unchanged = await apply_resource_tuning(project, ProjectFileHandler(), "productie")
    return changes


@pytest.mark.asyncio
async def test_de_tuner_laat_een_levende_cpu_wens_staan_en_tunet_het_geheugen_wel() -> None:
    """De wens geldt per VELD: de CPU blijft staan, het geheugen loopt gewoon mee."""
    vandaag = datetime.now(UTC).isoformat()
    project = _tuner_project(vandaag, intent_velden={"limits": {"cpu": "1"}, "requests": {"cpu": "50m"}})

    # 400Mi gemeten tegen een limiet van 128Mi: het geheugen moet duidelijk omhoog.
    # De VPA-target van 500m zou de CPU-request naar de plafondwaarde tillen.
    changes = await _draai_tuner(project, max_mb=400, vpa_cpu_m=500.0, vpa_memory_mi=400.0)

    assert len(changes) == 1
    verandering = changes[0]
    assert "new_limits_cpu" not in verandering, "de CPU van de gebruiker is aangeraakt"
    assert float(verandering["new_limits_memory"].removesuffix("Mi")) > 128
    override = project["deployments"][0]["components"][0]["resources"]
    assert "cpu" not in override.get("limits", {})
    assert "cpu" not in override.get("requests", {})


@pytest.mark.asyncio
async def test_zonder_wens_verzet_de_tuner_de_cpu_wel() -> None:
    """De negatieve controle: exact dezelfde meting, maar de entry is van de tuner zelf.

    Zonder deze controle bewijst de test hierboven alleen dat de CPU niet veranderde --
    niet dat de WENS daarvan de reden was.
    """
    vandaag = datetime.now(UTC).isoformat()
    project = _tuner_project(vandaag, intent_velden={"limits": {"cpu": "1"}, "requests": {"cpu": "50m"}})
    project["components"][0]["resources"]["history"][0]["source"] = "auto-tune"

    changes = await _draai_tuner(project, max_mb=400, vpa_cpu_m=500.0, vpa_memory_mi=400.0)

    assert len(changes) == 1
    assert changes[0]["new_requests_cpu"] == "250m", "zonder wens tilt de tuner de CPU-request op"


@pytest.mark.asyncio
async def test_de_tuner_pakt_een_vervallen_wens_weer_op() -> None:
    """Oud genoeg EN sindsdien ruim onder de gezette waarde: de wens vervalt.

    Zonder die vervalregel kan een veel te ruim gezette waarde nooit meer worden
    rechtgezet, en dat is precies waar de tuner voor is.
    """
    lang_geleden = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    project = _tuner_project(lang_geleden, intent_velden={"limits": {"cpu": "1"}, "requests": {"cpu": "50m"}})

    # VPA-target 10m tegen een gezette request van 50m: ruim onder de 50%-drempel.
    changes = await _draai_tuner(project, max_mb=400, vpa_cpu_m=10.0, vpa_memory_mi=400.0)

    assert len(changes) == 1
    assert changes[0]["new_requests_cpu"] == "25m", "de tuner pakt de CPU weer op"


@pytest.mark.asyncio
async def test_een_actieve_oom_tilt_de_geheugenlimiet_boven_een_levende_wens() -> None:
    """De enige plek waar de tuner de gebruiker overruled: een pod die NU omvalt."""
    vandaag = datetime.now(UTC).isoformat()
    project = _tuner_project(vandaag, intent_velden={"limits": {"memory": "128Mi"}, "requests": {"memory": "64Mi"}})

    changes = await _draai_tuner(project, max_mb=120, oom=True)

    assert len(changes) == 1
    nieuwe_limiet = float(changes[0]["new_limits_memory"].removesuffix("Mi"))
    assert nieuwe_limiet > 128, "een OOM moet de limiet omhoog kunnen tillen"
    # Het request is wel vastgezet: alleen de LIMIET is de noodroute.
    assert changes[0]["new_requests_memory"] == "64Mi"


@pytest.mark.asyncio
async def test_zonder_oom_blijft_de_geheugenwens_staan() -> None:
    """De negatieve controle bij de test hierboven: zonder OOM verandert het geheugen niet.

    Zonder deze controle bewijst die test alleen dat er iets gebeurde, niet dat de OOM de
    reden was.
    """
    vandaag = datetime.now(UTC).isoformat()
    project = _tuner_project(vandaag, intent_velden={"limits": {"memory": "128Mi"}, "requests": {"memory": "64Mi"}})

    changes = await _draai_tuner(project, max_mb=120, oom=False)

    assert changes == []
