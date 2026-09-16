"""Een dienst declareert zijn speelruimte (RC-168).

De connectielimiet moet instelbaar worden per project en per deployment, en een project
met een eigen databasecluster moet zijn geheugen en volumegrootte kunnen zetten. Twee
taken, dezelfde vorm: de dienst bepaalt wat verantwoord is, het project kiest daarbinnen,
en iets moet controleren dat die keuze binnen de perken blijft. Dat mechanisme bestond
niet -- een grens stond als los getal in een pydantic-model, hardgecodeerd in een
connector, of nergens.

Hier wordt het mechanisme gemeten, en niet een dienst die het gebruikt: er is er nog
geen. De eerste test is dan ook de belangrijkste: de hele catalogus declareert niets, dus
er verandert vandaag niets aan het gedrag van welke dienst dan ook. De rest meet de vier
beloften -- een grens op precies een plek, specifieker wint, buiten de speelruimte is een
leesbare fout bij het INLEZEN, en wat een dienst niet declareert is niet instelbaar --
met declaraties die alleen in deze tests bestaan.
"""

from __future__ import annotations

import asyncio
import copy
from dataclasses import dataclass
from typing import Any

import pytest
from opi.core.project_schema import ProjectIntegrityError
from opi.forms.editables.editable import WidgetType
from opi.forms.editables.validators import ConfigSettingValidator
from opi.forms.visualizers.config_setting_fields import setting_field
from opi.manager.project_validation import (
    iter_service_config_blocks,
    validate_project_structure,
    validate_service_configs,
)
from opi.services.catalog.base import ConfigLayer, config_path
from opi.services.catalog.config_settings import (
    MISSING,
    ChoiceSetting,
    IntegerSetting,
    QuantityKind,
    QuantitySetting,
    SettingError,
    check_setting_changes,
    check_settings,
    read_setting_value,
    resolve_setting,
)
from opi.services.project_service import get_project_service
from opi.services.project_store import GitProjectStore
from opi.services.registry import SERVICES, get_service
from opi.services.services_enums import ServiceType
from opi.utils.yaml_util import dump_yaml_to_string

from test_project_store import RELATIVE_PATH, FakeGitConnector, FakeRemote  # type: ignore[import-not-found]

_DATABASE = ServiceType.POSTGRESQL_DATABASE
_REDIS = ServiceType.NAMESPACE_REDIS


# --- declaraties die alleen hier bestaan ------------------------------------------

CONNECTIONS = IntegerSetting(
    path="connection-limit",
    layers=(ConfigLayer.PROJECT, ConfigLayer.DEPLOYMENT),
    default=20,
    minimum=1,
    maximum=100,
    label="Connectielimiet",
)

MEMORY = QuantitySetting(
    path="resources.limits.memory",
    layers=(ConfigLayer.PROJECT,),
    default="512Mi",
    minimum="256Mi",
    maximum="2Gi",
    kind=QuantityKind.MEMORY,
    label="Geheugenlimiet",
)

CPU = QuantitySetting(
    path="resources.limits.cpu",
    layers=(ConfigLayer.PROJECT,),
    default="500m",
    minimum="100m",
    maximum="2",
    kind=QuantityKind.CPU,
    label="CPU-limiet",
)

VOLUME = QuantitySetting(
    path="storage",
    layers=(ConfigLayer.PROJECT,),
    default="1Gi",
    minimum="1Gi",
    maximum="10Gi",
    kind=QuantityKind.MEMORY,
    grow_only=True,
    label="Opslag",
)

#: Hetzelfde volume, maar met een standaard BOVEN de ondergrens. Dat verschil is nodig om
#: een grendel te kunnen meten die op de standaard sluit: staat de standaard op de
#: ondergrens, dan is er geen enkele geldige waarde onder de standaard en kan een gat in
#: "een ontbrekend blok telt als de standaard" zich niet laten zien.
RUIM_VOLUME = QuantitySetting(
    path="storage",
    layers=(ConfigLayer.PROJECT,),
    default="5Gi",
    minimum="1Gi",
    maximum="10Gi",
    kind=QuantityKind.MEMORY,
    grow_only=True,
    label="Opslag",
)

#: Een grens op een dienst waarvan de config een component-EIGENSCHAP is (``aliases``,
#: ``user-env-vars``) in plaats van een blok in een ``services:``-lijst.
EIGENSCHAP_LIMIET = IntegerSetting(
    path="LIMIET",
    layers=(ConfigLayer.COMPONENT,),
    default=10,
    minimum=1,
    maximum=100,
    label="Limiet",
)

#: Hetzelfde grow_only-volume, maar op de DEPLOYMENTlaag. Een veld dat een wijziging
#: beoordeelt mag op precies een laag staan; dat die ene laag ook een andere dan het
#: project mag zijn, is wat ``namespace-redis`` hieronder meet.
DEPLOYMENT_VOLUME = QuantitySetting(
    path="storage",
    layers=(ConfigLayer.DEPLOYMENT,),
    default="5Gi",
    minimum="1Gi",
    maximum="10Gi",
    kind=QuantityKind.MEMORY,
    grow_only=True,
    label="Opslag",
)

#: Hetzelfde grow_only-volume, maar op de laag waar een dienst meer dan een blok per plek
#: kan hebben: de deployment-component, waar de opslagdiensten een record per MOUNT
#: bijhouden. Een laag, dus de declaratiegrendel laat hem toe.
MOUNT_VOLUME = QuantitySetting(
    path="storage",
    layers=(ConfigLayer.DEPLOYMENT_COMPONENT,),
    default="5Gi",
    minimum="1Gi",
    maximum="10Gi",
    kind=QuantityKind.MEMORY,
    grow_only=True,
    label="Opslag",
)

IMAGE = ChoiceSetting(
    path="image",
    layers=(ConfigLayer.PROJECT,),
    default="ghcr.io/cloudnative-pg/postgresql:17",
    allowed=("ghcr.io/cloudnative-pg/postgresql:17", "ghcr.io/cloudnative-pg/postgresql:16"),
    label="Image",
)


def _project(config: dict[str, Any], **extra: Any) -> dict[str, Any]:
    """Een project dat de databasedienst met een eigen cluster gebruikt."""
    data: dict[str, Any] = {
        "schema-version": 2,
        "name": "demo",
        "display-name": "Demo",
        "description": "test project",
        "users": [{"email": "admin@example.com", "role": "admin"}],
        "clusters": ["local"],
        "services": [{"name": _DATABASE.value, "config": {"scope": "project", **config}}],
        "components": [{"name": "backend", "type": "single", "services": [_DATABASE.value]}],
        "deployments": [
            {
                "name": "deployment-1",
                "cluster": "local",
                "namespace": "demo",
                "components": [{"reference": "backend"}],
            }
        ],
        "config": {"api-key": "base64+age:dGVzdC1hcGkta2V5"},
    }
    data.update(extra)
    return data


@pytest.fixture
def declaring(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Laat de databasedienst een speelruimte declareren, alleen voor deze test."""

    def declare(*settings: Any) -> Any:
        provider = get_service(_DATABASE)
        monkeypatch.setattr(provider, "config_settings", lambda: tuple(settings))
        return provider

    return declare


# --- 1. er verandert vandaag niets ------------------------------------------------


def test_geen_enkele_dienst_in_de_catalogus_declareert_iets() -> None:
    """De regressietoets over de hele catalogus.

    De waarde van dit mechanisme zit erin dat twee volgende taken erop kunnen bouwen,
    niet in wat het zelf oplevert. Declareert een dienst hier wel iets, dan is er gedrag
    veranderd dat niemand heeft gevraagd -- en dan hoort daar een test bij die dat gedrag
    meet, niet deze.
    """
    declarerend = {name: provider.config_settings() for name, provider in SERVICES.items()}
    assert {name: settings for name, settings in declarerend.items() if settings} == {}


def test_een_dienst_die_niets_declareert_krijgt_er_geen_laag_bij() -> None:
    """``config_layers()`` telt een laag mee die een setting openzet; nul settings, nul lagen."""
    for provider in SERVICES.values():
        lagen = provider.config_layers()
        assert all(not setting.allows(layer) for setting in provider.config_settings() for layer in lagen)


def test_een_projectbestand_zonder_declaraties_valideert_ongewijzigd() -> None:
    validate_service_configs(_project({"storage": "1Gi", "instances": 1}))


# --- 2. specifieker wint ----------------------------------------------------------


def test_de_vier_combinaties_van_twee_lagen() -> None:
    """Geen, alleen project, alleen deployment, allebei -- de specifieke wint."""
    assert resolve_setting(CONNECTIONS, {}) == 20
    assert resolve_setting(CONNECTIONS, {ConfigLayer.PROJECT: 60}) == 60
    assert resolve_setting(CONNECTIONS, {ConfigLayer.DEPLOYMENT: 80}) == 80
    assert resolve_setting(CONNECTIONS, {ConfigLayer.PROJECT: 60, ConfigLayer.DEPLOYMENT: 80}) == 80


def test_een_laag_die_de_dienst_niet_openzet_telt_niet_mee_bij_het_samenvoegen() -> None:
    """Een waarde die langs de validatie zou glippen kan alsnog niets doen."""
    assert resolve_setting(MEMORY, {ConfigLayer.DEPLOYMENT: "2Gi"}) == "512Mi"


def test_een_lege_laag_zegt_niets_en_valt_terug() -> None:
    assert resolve_setting(CONNECTIONS, {ConfigLayer.DEPLOYMENT: None, ConfigLayer.PROJECT: 60}) == 60


def test_een_waarde_binnen_de_speelruimte_komt_ongewijzigd_terug() -> None:
    """Wat de connector krijgt is wat het project opgaf, niet iets afgeknepen."""
    assert resolve_setting(MEMORY, {ConfigLayer.PROJECT: "1Gi"}) == "1Gi"
    assert MEMORY.check("1Gi") == "1Gi"
    assert CONNECTIONS.check(60) == 60


# --- 3. de drie soorten grens -----------------------------------------------------


def test_een_geheel_getal_buiten_zijn_grenzen() -> None:
    with pytest.raises(SettingError, match="tussen 1 en 100"):
        CONNECTIONS.check(200)
    with pytest.raises(SettingError, match="tussen 1 en 100"):
        CONNECTIONS.check(0)


def test_een_geheel_getal_dat_geen_geheel_getal_is() -> None:
    for waarde in ("veel", 1.5, True, None):
        with pytest.raises(SettingError, match="geheel getal"):
            CONNECTIONS.check(waarde)


def test_hoeveelheden_worden_ontleed_en_niet_als_tekst_vergeleken() -> None:
    """De twee paren waar tekstvergelijking op omvalt.

    Alfabetisch komt ``1Gi`` voor ``512Mi`` en ``2`` voor ``100m``, terwijl beide vier
    respectievelijk twintig keer zo groot zijn. Beide parsers staan in
    ``opi/services/resource_analyzer.py``, zodat dat op een plek wordt beantwoord.
    """
    ruim = QuantitySetting(
        path="geheugen",
        layers=(ConfigLayer.PROJECT,),
        default="512Mi",
        minimum="512Mi",
        maximum="1Gi",
        kind=QuantityKind.MEMORY,
        label="Geheugen",
    )
    assert ruim.check("1Gi") == "1Gi"
    with pytest.raises(SettingError, match="tussen 512Mi en 1Gi"):
        ruim.check("256Mi")

    kern = QuantitySetting(
        path="cpu",
        layers=(ConfigLayer.PROJECT,),
        default="100m",
        minimum="100m",
        maximum="2",
        kind=QuantityKind.CPU,
        label="CPU",
    )
    assert kern.check("2") == "2"
    with pytest.raises(SettingError, match="tussen 100m en 2"):
        kern.check("50m")


def test_een_hoeveelheid_die_geen_hoeveelheid_is() -> None:
    with pytest.raises(SettingError, match="geen geldige Kubernetes-hoeveelheid"):
        MEMORY.check("heel veel")
    with pytest.raises(SettingError, match="Kubernetes-hoeveelheid"):
        MEMORY.check(512)


def test_een_waarde_buiten_de_toegestane_verzameling() -> None:
    assert IMAGE.check("ghcr.io/cloudnative-pg/postgresql:16") == "ghcr.io/cloudnative-pg/postgresql:16"
    with pytest.raises(SettingError, match="moet een van deze waarden zijn"):
        IMAGE.check("docker.io/library/postgres:latest")


# --- 4. een declaratie die zichzelf tegenspreekt ----------------------------------


def test_een_standaardwaarde_buiten_zijn_eigen_speelruimte_is_een_fout_in_de_declaratie() -> None:
    with pytest.raises(ValueError, match="valt buiten zijn eigen speelruimte"):
        IntegerSetting(path="x", layers=(ConfigLayer.PROJECT,), default=500, minimum=1, maximum=100, label="X")


def test_een_ondergrens_boven_de_bovengrens() -> None:
    with pytest.raises(ValueError, match="ligt boven maximum"):
        IntegerSetting(path="x", layers=(ConfigLayer.PROJECT,), default=1, minimum=10, maximum=1, label="X")
    with pytest.raises(ValueError, match="ligt boven maximum"):
        QuantitySetting(
            path="x",
            layers=(ConfigLayer.PROJECT,),
            default="1Gi",
            minimum="1Gi",
            maximum="512Mi",
            kind=QuantityKind.MEMORY,
            label="X",
        )


def test_een_veld_dat_een_wijziging_beoordeelt_staat_op_precies_een_laag() -> None:
    """Anders is de wijzigingsregel te omzeilen door hem een laag lager op te schrijven.

    De regel vergelijkt per configBLOK, terwijl de effectieve waarde uit de MEEST
    SPECIFIEKE laag komt die iets zegt. Staat hetzelfde grow_only-veld op twee lagen, dan
    is 8Gi op projectniveau met een nieuwe deployment-override van 2Gi effectief een
    verkleining, zonder dat er ook maar een blok kleiner wordt -- en dan ziet de regel
    niets. Zolang de opgeloste waarde niet per plek wordt vergeleken, is de declaratie
    zelf de plek om dat af te snijden: hardop, bij het inladen.
    """
    with pytest.raises(ValueError, match="beoordeelt een WIJZIGING"):
        QuantitySetting(
            path="storage",
            layers=(ConfigLayer.PROJECT, ConfigLayer.DEPLOYMENT),
            default="5Gi",
            minimum="1Gi",
            maximum="10Gi",
            kind=QuantityKind.MEMORY,
            grow_only=True,
            label="Opslag",
        )

    # Zonder die regel mag hetzelfde veld wel op twee lagen staan: dan is er geen
    # wijziging te beoordelen en doet 'specifieker wint' gewoon zijn werk.
    QuantitySetting(
        path="storage",
        layers=(ConfigLayer.PROJECT, ConfigLayer.DEPLOYMENT),
        default="5Gi",
        minimum="1Gi",
        maximum="10Gi",
        kind=QuantityKind.MEMORY,
        label="Opslag",
    )
    # En met de regel op een laag -- welke laag dat is, maakt niet uit.
    assert VOLUME.judges_changes is True
    assert DEPLOYMENT_VOLUME.judges_changes is True
    assert MEMORY.judges_changes is False
    assert CONNECTIONS.judges_changes is False


def test_een_volgende_soort_grens_kan_het_vlaggetje_niet_vergeten() -> None:
    """``judges_changes`` vraagt het aan de KLASSE, niet aan een los bij te houden vlaggetje.

    Een volgende soort grens die ``check_change`` een body geeft en het vlaggetje vergat,
    erfde precies de omzeiling die de grendel hierboven sluit. Nu is de body zelf het
    antwoord: hetzelfde veld op twee lagen wordt geweigerd zonder dat de nieuwe soort er
    iets voor hoeft te zeggen.
    """

    @dataclass(frozen=True, kw_only=True)
    class AlleenLangerSetting(IntegerSetting):
        def check_change(self, previous: Any, new: Any) -> None:
            if new < previous:
                raise SettingError(f"'{self.path}' kan alleen omhoog.")

    assert AlleenLangerSetting(path="x", layers=(ConfigLayer.PROJECT,), default=5, minimum=1, maximum=10, label="X")
    with pytest.raises(ValueError, match="beoordeelt een WIJZIGING"):
        AlleenLangerSetting(
            path="x",
            layers=(ConfigLayer.PROJECT, ConfigLayer.DEPLOYMENT),
            default=5,
            minimum=1,
            maximum=10,
            label="X",
        )


def test_met_een_laag_is_het_blok_de_plek_waar_de_waarde_wordt_opgelost() -> None:
    """Waarom vergelijken per BLOK dan hetzelfde is als vergelijken per effectieve waarde.

    ``resolve_setting`` raadpleegt alleen lagen die de dienst openzet. Staat het veld op
    een laag, dan is het blok op die laag het enige dat de uitkomst kan veranderen -- dus
    een blok dat niet krimpt is een waarde die niet krimpt.
    """
    assert resolve_setting(VOLUME, {ConfigLayer.PROJECT: "8Gi", ConfigLayer.DEPLOYMENT: "2Gi"}) == "8Gi"
    assert resolve_setting(DEPLOYMENT_VOLUME, {ConfigLayer.PROJECT: "2Gi", ConfigLayer.DEPLOYMENT: "8Gi"}) == "8Gi"
    assert resolve_setting(DEPLOYMENT_VOLUME, {ConfigLayer.PROJECT: "2Gi"}) == "5Gi"


def test_een_veld_zonder_laag_kan_nergens_gezet_worden_en_wordt_geweigerd() -> None:
    with pytest.raises(ValueError, match="geen enkele laag"):
        IntegerSetting(path="x", layers=(), default=1, minimum=1, maximum=10, label="X")


def test_een_veld_zonder_pad_wijst_nergens_heen() -> None:
    with pytest.raises(ValueError, match="heeft een pad nodig"):
        IntegerSetting(path="", layers=(ConfigLayer.PROJECT,), default=1, minimum=1, maximum=10, label="X")


def test_een_verzameling_zonder_waarden_laat_niets_toe() -> None:
    with pytest.raises(ValueError, match="geen enkele toegestane waarde"):
        ChoiceSetting(path="image", layers=(ConfigLayer.PROJECT,), default="x", allowed=(), label="Image")


# --- 5. wat een dienst niet declareert, is niet instelbaar ------------------------


def test_een_veld_dat_de_dienst_niet_declareert_heeft_geen_speelruimte(declaring: Any) -> None:
    provider = declaring(CONNECTIONS)
    assert provider.config_setting("connection-limit") is CONNECTIONS
    with pytest.raises(SettingError, match="geen instelbaar veld 'storage'"):
        provider.config_setting("storage")


def test_een_veld_op_een_laag_die_de_dienst_niet_openzet_wordt_geweigerd() -> None:
    with pytest.raises(SettingError, match="alleen op projectniveau"):
        check_settings([MEMORY], {"resources": {"limits": {"memory": "1Gi"}}}, ConfigLayer.DEPLOYMENT)
    with pytest.raises(SettingError, match="alleen op projectniveau"):
        MEMORY.yaml_path(_DATABASE, ConfigLayer.DEPLOYMENT)


def test_een_ongenoemd_veld_wordt_overgeslagen() -> None:
    assert read_setting_value({"iets-anders": 3}, CONNECTIONS) is MISSING
    assert repr(MISSING) == "MISSING"  # zodat "ontbreekt" in een foutmelding leesbaar is
    check_settings([CONNECTIONS, MEMORY], {"iets-anders": 3}, ConfigLayer.PROJECT)


def test_een_configblok_dat_geen_dict_is_levert_niets_op() -> None:
    """Een dienst waarvan de config op een laag een LIJST is (opslagmounts)."""
    assert read_setting_value([{"name": "data"}], CONNECTIONS) is MISSING


# --- 6. de fout valt bij het inlezen, niet bij het aanmaken van de resource -------


def test_buiten_de_speelruimte_is_een_leesbare_fout_bij_het_inlezen(declaring: Any) -> None:
    declaring(VOLUME)
    with pytest.raises(ProjectIntegrityError, match="'storage' moet tussen 1Gi en 10Gi liggen; je gaf 50Gi"):
        validate_service_configs(_project({"storage": "50Gi"}))


def test_een_image_buiten_de_verzameling_wordt_geweigerd_bij_het_inlezen(declaring: Any) -> None:
    declaring(IMAGE)
    with pytest.raises(ProjectIntegrityError, match="moet een van deze waarden zijn"):
        validate_service_configs(_project({"image": "docker.io/library/postgres:latest"}))


def test_een_waarde_binnen_de_speelruimte_komt_door_de_validatie_heen(declaring: Any) -> None:
    declaring(VOLUME, IMAGE)
    validate_service_configs(_project({"storage": "5Gi", "image": "ghcr.io/cloudnative-pg/postgresql:16"}))


def test_een_veld_op_de_verkeerde_laag_wordt_bij_het_inlezen_geweigerd(declaring: Any) -> None:
    """Hetzelfde veld, maar de dienst zet het alleen per deployment open."""
    alleen_deployment = QuantitySetting(
        path="storage",
        layers=(ConfigLayer.DEPLOYMENT,),
        default="1Gi",
        minimum="1Gi",
        maximum="10Gi",
        kind=QuantityKind.MEMORY,
        label="Opslag",
    )
    declaring(alleen_deployment)
    with pytest.raises(ProjectIntegrityError, match="kun je niet op projectniveau zetten"):
        validate_service_configs(_project({"storage": "5Gi"}))


# --- 7. alleen omhoog -------------------------------------------------------------


def test_een_volume_verkleinen_wordt_geweigerd_ook_binnen_de_grenzen() -> None:
    VOLUME.check("2Gi")  # binnen de grenzen, dus de grenzen zeggen er niets over
    with pytest.raises(SettingError, match="kan alleen omhoog"):
        VOLUME.check_change("5Gi", "2Gi")
    VOLUME.check_change("5Gi", "5Gi")
    VOLUME.check_change("5Gi", "10Gi")


def test_een_veld_zonder_die_regel_mag_beide_kanten_op() -> None:
    MEMORY.check_change("1Gi", "512Mi")


def test_een_wijziging_wordt_alleen_beoordeeld_op_een_laag_die_de_dienst_openzet() -> None:
    """VOLUME staat alleen op projectniveau open, dus per deployment valt er niets te zeggen."""
    check_setting_changes([VOLUME], {"storage": "5Gi"}, {"storage": "2Gi"}, ConfigLayer.DEPLOYMENT)
    check_setting_changes([VOLUME], {"storage": "5Gi"}, {"iets-anders": 1}, ConfigLayer.DEPLOYMENT)


def test_het_veld_weglaten_is_dezelfde_verlaging_als_het_expliciet_verlagen() -> None:
    """Beide kanten worden hetzelfde gelezen: de waarde, of anders de standaard.

    Alleen de velden beoordelen die de NIEUWE versie noemt maakt de regel omzeilbaar
    door het veld weg te laten -- de effectieve waarde zakt dan naar de standaard, en
    dat is precies de verlaging die de regel weigert.
    """
    with pytest.raises(SettingError, match="kan alleen omhoog"):
        check_setting_changes([VOLUME], {"storage": "5Gi"}, {"storage": "1Gi"}, ConfigLayer.PROJECT)
    with pytest.raises(SettingError, match="kan alleen omhoog"):
        check_setting_changes([VOLUME], {"storage": "5Gi"}, {"iets-anders": 1}, ConfigLayer.PROJECT)
    with pytest.raises(SettingError, match="kan alleen omhoog"):
        check_setting_changes([VOLUME], {"storage": "5Gi"}, {}, ConfigLayer.PROJECT)
    with pytest.raises(SettingError, match="kan alleen omhoog"):
        check_setting_changes([VOLUME], {"storage": "5Gi"}, None, ConfigLayer.PROJECT)
    # Noemt geen van beide versies het veld, dan staan ze allebei op de standaard.
    check_setting_changes([VOLUME], {}, {}, ConfigLayer.PROJECT)


def test_verlagen_ten_opzichte_van_de_standaard_telt_ook_als_verlagen() -> None:
    """Stond het veld er niet, dan stond het op de standaard van de dienst."""
    with pytest.raises(SettingError, match="kan alleen omhoog"):
        check_setting_changes([RUIM_VOLUME], {}, {"storage": "1Gi"}, ConfigLayer.PROJECT)
    with pytest.raises(SettingError, match="kan alleen omhoog"):
        check_setting_changes([RUIM_VOLUME], None, {"storage": "2Gi"}, ConfigLayer.PROJECT)
    check_setting_changes([RUIM_VOLUME], {}, {"storage": "8Gi"}, ConfigLayer.PROJECT)


def test_een_verlaging_wordt_bij_het_opslaan_geweigerd(declaring: Any) -> None:
    declaring(VOLUME)
    oud = _project({"storage": "5Gi"})
    nieuw = _project({"storage": "2Gi"})

    asyncio.run(validate_project_structure(nieuw))  # zonder de vorige versie is er niets te vergelijken
    with pytest.raises(ProjectIntegrityError, match="kan alleen omhoog"):
        asyncio.run(validate_project_structure(nieuw, previous=oud))


def test_een_verhoging_en_een_nieuw_project_gaan_gewoon_door(declaring: Any) -> None:
    declaring(VOLUME)
    oud = _project({"storage": "2Gi"})
    asyncio.run(validate_project_structure(_project({"storage": "5Gi"}), previous=oud))
    asyncio.run(validate_project_structure(_project({"storage": "2Gi"}), previous={"name": "demo"}))


def test_een_verlaging_elders_in_het_bestand_verwart_de_vergelijking_niet(declaring: Any) -> None:
    """Blokken worden op hun PLEK gekoppeld, niet op volgorde."""
    declaring(VOLUME)
    oud = _project({"storage": "5Gi"})
    nieuw = copy.deepcopy(oud)
    nieuw["deployments"].append({"name": "deployment-2", "cluster": "local", "namespace": "demo", "components": []})
    asyncio.run(validate_project_structure(nieuw, previous=oud))


def _store_op(vorige: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> GitProjectStore:
    """Een echte ``GitProjectStore`` met ``vorige`` als de vastgelegde versie."""
    get_project_service().clear_all_projects()
    remote = FakeRemote()
    remote.commit("initial", {RELATIVE_PATH: dump_yaml_to_string(vorige)})
    connector = FakeGitConnector(remote, str(tmp_path))
    store = GitProjectStore(working_dir=str(tmp_path))

    async def _get_connector() -> FakeGitConnector:
        return connector

    monkeypatch.setattr(store, "get_connector", _get_connector)
    return store


def test_de_opslagroute_geeft_de_vorige_versie_door(monkeypatch: pytest.MonkeyPatch, tmp_path, declaring: Any) -> None:
    """De regel moet ook echt afgaan op de weg die een projectbestand wegschrijft.

    Een regel over een WIJZIGING kan alleen daar draaien waar beide versies in handen
    zijn. ``validate_project_structure`` krijgt er maar een, dus zonder deze koppeling in
    de store zou de regel decoratie zijn: groen in een unittest en nooit in productie.
    """
    declaring(VOLUME)
    store = _store_op(_project({"storage": "5Gi"}), monkeypatch, tmp_path)

    with pytest.raises(ProjectIntegrityError, match="kan alleen omhoog"):
        asyncio.run(store.save("demo", _project({"storage": "2Gi"}), message="krimp", actor="test"))

    asyncio.run(store.save("demo", _project({"storage": "8Gi"}), message="groei", actor="test"))


def _zonder_configblok() -> dict[str, Any]:
    """Hetzelfde project, maar de dienst staat er als kale string in."""
    data = _project({})
    data["services"] = [_DATABASE.value]
    return data


def _zonder_de_dienst() -> dict[str, Any]:
    """Hetzelfde project, maar de dienst wordt niet meer gebruikt."""
    data = _project({})
    data["services"] = []
    data["components"] = [{"name": "backend", "type": "single", "services": []}]
    return data


@pytest.mark.parametrize(
    "nieuw",
    [
        pytest.param(_project({"storage": "2Gi"}), id="expliciet-verlaagd"),
        pytest.param(_project({}), id="veld-weggelaten"),
        pytest.param(_zonder_configblok(), id="configblok-weg"),
    ],
)
def test_dezelfde_verlaging_wordt_langs_elke_weg_geweigerd(
    nieuw: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path, declaring: Any
) -> None:
    """Van 5Gi naar effectief 1Gi, op drie manieren opgeschreven -- en drie keer nee.

    De uitkomst is elke keer dezelfde: de standaard van de dienst, kleiner dan wat er
    stond. Weigeren langs de ene weg en doorlaten langs de andere zou de grendel
    omzeilbaar maken door het veld gewoon leeg te laten, en dat is precies wat een leeg
    wizardveld doet.
    """
    declaring(VOLUME)
    store = _store_op(_project({"storage": "5Gi"}), monkeypatch, tmp_path)

    with pytest.raises(ProjectIntegrityError, match="kan alleen omhoog"):
        asyncio.run(store.save("demo", nieuw, message="krimp", actor="test"))


def test_de_dienst_helemaal_niet_meer_gebruiken_is_geen_verlaging(
    monkeypatch: pytest.MonkeyPatch, tmp_path, declaring: Any
) -> None:
    """Een dienst opzeggen is een verwijdering, geen verkleining van zijn volume."""
    declaring(VOLUME)
    store = _store_op(_project({"storage": "5Gi"}), monkeypatch, tmp_path)

    asyncio.run(store.save("demo", _zonder_de_dienst(), message="dienst eruit", actor="test"))


@pytest.mark.parametrize(
    "oud",
    [
        pytest.param(_project({"storage": "5Gi"}), id="expliciet-5Gi"),
        pytest.param(_project({}), id="veld-weggelaten"),
        pytest.param(_zonder_configblok(), id="kale-dienst"),
    ],
)
def test_de_vorige_versie_wordt_net_zo_gelezen_als_de_nieuwe(
    oud: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path, declaring: Any
) -> None:
    """De spiegelrichting van de drie wegen hierboven: nu staat de VORIGE versie kaal.

    Alle drie zeggen hetzelfde: effectief 5Gi, de standaard van de dienst. Wordt de vorige
    versie op zijn WAARDE opgezocht in plaats van op zijn sleutel, dan vallen "de dienst
    staat er kaal in" en "de dienst staat er niet" weer samen, en glipt een verlaging naar
    2Gi -- keurig binnen de grenzen -- langs de grendel.

    Meetbaar is dat alleen met een standaard BOVEN de ondergrens: pas dan bestaat er een
    geldige waarde onder de standaard.
    """
    declaring(RUIM_VOLUME)
    store = _store_op(oud, monkeypatch, tmp_path)

    with pytest.raises(ProjectIntegrityError, match="kan alleen omhoog"):
        asyncio.run(store.save("demo", _project({"storage": "2Gi"}), message="krimp", actor="test"))


def test_een_kale_vorige_dienst_is_de_standaard_en_geen_afwezigheid(declaring: Any) -> None:
    """Dezelfde meting een laag lager, met de drie uitkomsten naast elkaar."""
    declaring(RUIM_VOLUME)

    with pytest.raises(ProjectIntegrityError, match="kan alleen omhoog"):
        asyncio.run(validate_project_structure(_project({"storage": "2Gi"}), previous=_zonder_configblok()))
    # Omhoog vanaf diezelfde kale dienst mag wel.
    asyncio.run(validate_project_structure(_project({"storage": "8Gi"}), previous=_zonder_configblok()))
    # En stond de dienst er vorige keer niet, dan is dit een toevoeging: alleen grenzen.
    asyncio.run(validate_project_structure(_project({"storage": "2Gi"}), previous=_zonder_de_dienst()))


def _redis_project(dep_config: dict[str, Any] | None, *, in_deployment: bool = True) -> dict[str, Any]:
    """Een project met ``namespace-redis``, met de configuratie op de DEPLOYMENTlaag.

    Die dienst heeft geen configmodel, dus het model laat een gedeclareerd veld hier door
    en de speelruimte is het enige dat er iets van vindt -- precies de situatie waarin een
    dienst straks iets op de deploymentlaag openzet.
    """
    deployment: dict[str, Any] = {
        "name": "deployment-1",
        "cluster": "local",
        "namespace": "demo",
        "components": [{"reference": "backend"}],
    }
    if in_deployment:
        deployment["services"] = [
            {"name": _REDIS.value, "config": dict(dep_config)} if dep_config is not None else _REDIS.value
        ]
    return {
        "schema-version": 2,
        "name": "demo",
        "display-name": "Demo",
        "description": "test project",
        "users": [{"email": "admin@example.com", "role": "admin"}],
        "clusters": ["local"],
        "services": [_REDIS.value],
        "components": [{"name": "backend", "type": "single", "services": [_REDIS.value]}],
        "deployments": [deployment],
        "config": {"api-key": "base64+age:dGVzdC1hcGkta2V5"},
    }


def test_de_regel_draait_net_zo_op_de_deploymentlaag(monkeypatch: pytest.MonkeyPatch) -> None:
    """Een grow_only-veld hoeft niet op het project te staan; het staat op EEN laag.

    De grendel op de declaratie zegt "precies een laag", niet "de projectlaag". Deze
    meting is de tegenhanger van de projectrijen hierboven, op de laag waar de reviewer
    de omzeiling vond: hetzelfde blok, per deployment gekoppeld, met dezelfde drie
    uitkomsten.
    """
    provider = get_service(_REDIS)
    monkeypatch.setattr(provider, "config_settings", lambda: (DEPLOYMENT_VOLUME,))
    vorige = _redis_project({"storage": "8Gi"})

    # Expliciet verlagen op de deploymentlaag.
    with pytest.raises(ProjectIntegrityError, match="kan alleen omhoog"):
        asyncio.run(validate_project_structure(_redis_project({"storage": "2Gi"}), previous=vorige))
    # Het blok weglaten is dezelfde verlaging: effectief de standaard, 5Gi.
    with pytest.raises(ProjectIntegrityError, match="kan alleen omhoog"):
        asyncio.run(validate_project_structure(_redis_project(None), previous=vorige))
    # Omhoog mag, en de dienst uit de deployment halen is een verwijdering.
    asyncio.run(validate_project_structure(_redis_project({"storage": "10Gi"}), previous=vorige))
    asyncio.run(validate_project_structure(_redis_project(None, in_deployment=False), previous=vorige))
    # En stond de dienst er in die deployment nog niet, dan is dit een toevoeging.
    asyncio.run(
        validate_project_structure(
            _redis_project({"storage": "2Gi"}), previous=_redis_project(None, in_deployment=False)
        )
    )


def _mount_project(mounts: dict[str, str] | None) -> dict[str, Any]:
    """Een project waarin de dienst een record PER MOUNT bijhoudt op de deployment-component.

    Dat is de vorm die het schema daar zelf beschrijft (``deployment-component-service``:
    of een record met een config, of een LIJST van records per ``reference``) en die de
    opslagdiensten gebruiken. Hier op ``namespace-redis``, want die dienst heeft geen
    configmodel op deze laag, zodat het model een gedeclareerd veld doorlaat en de
    speelruimte het enige is dat er iets van vindt.
    """
    component: dict[str, Any] = {"reference": "backend"}
    if mounts is not None:
        component["services"] = {
            _REDIS.value: [{"reference": naam, "config": {"storage": maat}} for naam, maat in mounts.items()]
        }
    return {
        "schema-version": 2,
        "name": "demo",
        "display-name": "Demo",
        "description": "test project",
        "users": [{"email": "admin@example.com", "role": "admin"}],
        "clusters": ["local"],
        "services": [_REDIS.value],
        "components": [{"name": "backend", "type": "single", "services": [_REDIS.value]}],
        "deployments": [
            {
                "name": "deployment-1",
                "cluster": "local",
                "namespace": "demo",
                "components": [component],
            }
        ],
        "config": {"api-key": "base64+age:dGVzdC1hcGkta2V5"},
    }


def test_twee_mounts_van_dezelfde_dienst_zijn_twee_plekken() -> None:
    """Een plek is fijnmaziger dan een laag: de mount hoort erbij.

    Op de deployment-componentlaag draagt een component meerdere blokken van dezelfde
    dienst, een per mount. Deelden die een sleutel, dan overleefde van de vorige versie
    alleen de LAATSTE mount en werd elke mount daartegen gelegd.
    """
    blokken = iter_service_config_blocks(_mount_project({"data": "8Gi", "logs": "2Gi"}))
    per_mount = {b.location: b.config for b in blokken if b.name == _REDIS.value and "/mount:" in b.location}
    assert per_mount == {
        "deployment:deployment-1/component:backend/mount:data": {"storage": "8Gi"},
        "deployment:deployment-1/component:backend/mount:logs": {"storage": "2Gi"},
    }


def test_een_mount_verkleinen_wordt_niet_gedekt_door_een_ANDERE_mount(monkeypatch: pytest.MonkeyPatch) -> None:
    """De verkleining van mount 'data' mag niet tegen mount 'logs' worden weggestreept.

    Dezelfde klasse als het veld weglaten, het blok weglaten en de laag verwisselen: een
    echte verkleining van dezelfde PVC, opgeschreven op een plek waar de koppeling hem
    niet zag. Twee rijen: naast een kleinere mount en naast een ongewijzigde mount.
    """
    provider = get_service(_REDIS)
    monkeypatch.setattr(provider, "config_settings", lambda: (MOUNT_VOLUME,))

    # 'data' krimpt van 8Gi naar 2Gi terwijl 'logs' op 2Gi blijft staan.
    with pytest.raises(ProjectIntegrityError, match="kan alleen omhoog"):
        asyncio.run(
            validate_project_structure(
                _mount_project({"data": "2Gi", "logs": "2Gi"}), previous=_mount_project({"data": "8Gi", "logs": "2Gi"})
            )
        )
    # En naast een mount die niet verandert.
    with pytest.raises(ProjectIntegrityError, match="kan alleen omhoog"):
        asyncio.run(
            validate_project_structure(
                _mount_project({"data": "2Gi", "logs": "8Gi"}), previous=_mount_project({"data": "8Gi", "logs": "8Gi"})
            )
        )
    # Omhoog mag hier net zo goed, en een mount erbij is een toevoeging.
    asyncio.run(
        validate_project_structure(
            _mount_project({"data": "10Gi", "logs": "2Gi"}), previous=_mount_project({"data": "8Gi", "logs": "2Gi"})
        )
    )
    asyncio.run(
        validate_project_structure(
            _mount_project({"data": "8Gi", "logs": "2Gi"}), previous=_mount_project({"data": "8Gi"})
        )
    )


def test_een_ongewijzigd_bestand_met_ongelijke_mounts_blijft_opslaanbaar(monkeypatch: pytest.MonkeyPatch) -> None:
    """De vervelendste kant van dezelfde sleutel: niets veranderen en toch geweigerd worden.

    Met een sleutel per component werd 'data' vergeleken met de laatste mount van de
    vorige versie ('logs', 8Gi), dus was een project met twee ongelijke mounts daarna
    voor ELKE wijziging onopslaanbaar -- ook voor een wijziging die de opslag niet raakt.
    """
    provider = get_service(_REDIS)
    monkeypatch.setattr(provider, "config_settings", lambda: (MOUNT_VOLUME,))
    bestand = _mount_project({"data": "2Gi", "logs": "8Gi"})

    asyncio.run(validate_project_structure(copy.deepcopy(bestand), previous=copy.deepcopy(bestand)))

    gewijzigd = copy.deepcopy(bestand)
    gewijzigd["description"] = "iets anders"
    asyncio.run(validate_project_structure(gewijzigd, previous=copy.deepcopy(bestand)))


# --- 7b. een wandeling, twee lezers -----------------------------------------------


def test_een_dienst_zonder_configblok_staat_toch_in_de_wandeling() -> None:
    """Anders is 'het blok is weg' niet te onderscheiden van 'de dienst is weg'.

    Het eerste is een wijziging om te beoordelen (alles valt terug op de standaard), het
    tweede is een verwijdering. Zonder het kale blok in de wandeling ziet de
    wijzigingstoets ze allebei als niets.
    """
    (blok,) = [b for b in iter_service_config_blocks(_zonder_configblok()) if b.location == "project"]
    assert blok.name == _DATABASE.value
    assert blok.config is None
    assert [b for b in iter_service_config_blocks(_zonder_de_dienst()) if b.name == _DATABASE.value] == []


def test_de_wandeling_ziet_ook_de_blokken_die_een_component_eigenschap_zijn() -> None:
    """Een wandeling, twee lezers: de waardetoets en de wijzigingstoets zien hetzelfde.

    ``user-env-vars`` en ``aliases`` zijn diensten waarvan de config een eigenschap van
    het component is in plaats van een blok in een ``services:``-lijst. Stonden ze in
    maar een van de twee wandelingen, dan werd een setting op zo'n dienst wel op zijn
    grenzen getoetst en nooit op zijn wijziging.
    """
    data = _project({"storage": "1Gi"})
    data["components"][0]["user-env-vars"] = "API_KEY=x"

    (env,) = [b for b in iter_service_config_blocks(data) if b.owned_property == "user-env-vars" and b.config]
    assert env.name == ServiceType.USER_ENV_VARS.value
    assert env.layer is ConfigLayer.COMPONENT
    assert env.location == "component:backend"
    assert env.where == "van component 'backend'"


def _elke_laag_op_een_andere_index() -> dict[str, Any]:
    """Een blok op elke laag en in elke vorm, telkens op een andere index.

    Geen twee indexen zijn gelijk, zodat een verwisselde teller een ander pad oplevert.
    """
    data = _project({"storage": "1Gi"})
    data["components"] = [
        {"name": "frontend", "type": "single"},
        {
            "name": "backend",
            "type": "single",
            "services": ["publish-on-web", _DATABASE.value, {"name": _REDIS.value, "config": {"storage": "1Gi"}}],
            "user-env-vars": "API_KEY=x",
            "aliases": {"DB": "$DATABASE_DB"},
        },
    ]
    data["deployments"] = [
        {"name": "deployment-1", "components": [{"reference": "frontend"}]},
        {
            "name": "deployment-2",
            "services": ["clone", _DATABASE.value, {"name": _REDIS.value, "config": {"generation": 3}}],
            "components": [
                {"reference": "frontend"},
                {"reference": "extra", "services": ["publish-on-web", {"name": _REDIS.value, "config": {"x": 1}}]},
                {
                    "reference": "backend",
                    "services": {
                        "publish-on-web": {"config": {"subdomain": "api"}},
                        _REDIS.value: [
                            {"reference": "data", "config": {"storage": "8Gi"}},
                            {"reference": "logs", "config": {"storage": "2Gi"}},
                        ],
                    },
                    "user-env-vars": "LOG_LEVEL=debug",
                },
            ],
        },
    ]
    return data


def _volg_pad(data: Any, pad: str) -> Any:
    """Loop een ``/``-pad af: een getal is een index, een naam zoekt een dienst of sleutel."""
    for deel in pad.split("/"):
        if isinstance(data, list) and deel.isdigit():
            data = data[int(deel)]
        elif isinstance(data, list):
            (data,) = [e for e in data if isinstance(e, dict) and e.get("name") == deel]
        else:
            data = data[deel]
    return data


def test_het_pad_van_een_blok_wijst_naar_dat_blok_in_het_bestand() -> None:
    """``path`` staat in de melding over een leesbaar geheim en moet dus ergens heen wijzen.

    ``location`` herkent een blok bij naam, ``path`` wijst het aan op positie. Een
    verwisselde of vergeten teller wijst naar een ander blok of naar niets.
    """
    data = _elke_laag_op_een_andere_index()
    blokken = [b for b in iter_service_config_blocks(data) if b.config is not None]

    assert {(b.layer, b.owned_property is not None) for b in blokken} == {
        (ConfigLayer.PROJECT, False),
        (ConfigLayer.COMPONENT, False),
        (ConfigLayer.COMPONENT, True),
        (ConfigLayer.DEPLOYMENT, False),
        (ConfigLayer.DEPLOYMENT_COMPONENT, False),
        (ConfigLayer.DEPLOYMENT_COMPONENT, True),
    }
    assert len(blokken) == 10
    for blok in blokken:
        assert _volg_pad(data, blok.path) is blok.config, f"{blok.path} wijst niet naar {blok.location}"


def test_een_dienst_op_een_eigenschap_wordt_ook_op_zijn_grenzen_getoetst(monkeypatch: pytest.MonkeyPatch) -> None:
    """En niet alleen op zijn wijziging -- anders staat dezelfde scheefte omgekeerd terug.

    Zo'n blok ging langs het pydantic-model en verder niets, terwijl de wijzigingstoets
    hem wel meeneemt. Dan wordt een waarde wel op zijn VERANDERING beoordeeld en nooit op
    de speelruimte waar hij in moet blijven.
    """
    provider = get_service(ServiceType.ALIASES)
    monkeypatch.setattr(provider, "config_settings", lambda: (EIGENSCHAP_LIMIET,))
    data = _project({"storage": "1Gi"})
    data["components"][0]["aliases"] = {"LIMIET": "500"}

    with pytest.raises(ProjectIntegrityError, match="'LIMIET' valt buiten zijn speelruimte"):
        validate_service_configs(data)

    data["components"][0]["aliases"] = {"LIMIET": "50"}
    validate_service_configs(data)


def test_de_weigering_op_een_eigenschapsblok_noemt_de_waarde_niet(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dezelfde reden waarom de modelfout ernaast de waarde ook niet noemt.

    ``user-env-vars`` is de eigen omgeving van een component en accepteert een platte
    ``dict[str, str]``, dus een waarde op een gedeclareerd pad kan daar een geplakt geheim
    zijn -- en deze zin gaat zowel het centrale log in als het antwoord aan de aanroeper.
    De weigering wordt daarom uit de DECLARATIE opgebouwd: het veld en zijn speelruimte,
    niet wat er gelezen is. Voor een blok in een ``services:``-lijst blijft de waarde er
    wel in staan, want daar is het de grens zelf die wordt teruggeciteerd.
    """
    provider = get_service(ServiceType.USER_ENV_VARS)
    monkeypatch.setattr(provider, "config_settings", lambda: (EIGENSCHAP_LIMIET,))
    data = _project({"storage": "1Gi"})
    data["components"][0]["user-env-vars"] = {"LIMIET": "hunter2"}

    with pytest.raises(ProjectIntegrityError) as fout:
        validate_service_configs(data)

    assert "hunter2" not in str(fout.value)
    assert "'LIMIET' valt buiten zijn speelruimte" in str(fout.value)
    assert EIGENSCHAP_LIMIET.latitude() in str(fout.value)

    # Een blok in een services:-lijst citeert de waarde wel: dat is de grens zelf.
    declarerend = get_service(_DATABASE)
    monkeypatch.setattr(declarerend, "config_settings", lambda: (VOLUME,))
    with pytest.raises(ProjectIntegrityError, match="je gaf 50Gi"):
        validate_service_configs(_project({"storage": "50Gi"}))


# --- 8. de wizard leest dezelfde declaratie ---------------------------------------


def test_het_formulierveld_komt_uit_de_declaratie() -> None:
    veld = setting_field(CONNECTIONS, _DATABASE, ConfigLayer.PROJECT)

    assert veld.label == "Connectielimiet"
    assert veld.widget is WidgetType.NUMBER
    assert veld.editable.default == 20
    assert veld.editable.yaml_path == config_path(ConfigLayer.PROJECT, _DATABASE, "config", "connection-limit")
    assert veld.editable.virtualize == ("services", "_services-config")


def test_de_helptekst_noemt_dezelfde_grenzen_als_de_validatie_afdwingt() -> None:
    veld = setting_field(CONNECTIONS, _DATABASE, ConfigLayer.PROJECT)
    assert veld.description is not None
    assert "1" in veld.description
    assert "100" in veld.description
    assert "20" in veld.description
    volume_beschrijving = setting_field(VOLUME, _DATABASE, ConfigLayer.PROJECT).description
    assert volume_beschrijving is not None
    assert "alleen verhogen" in volume_beschrijving


def test_het_formulier_weigert_precies_wat_de_validatie_weigert() -> None:
    """Twee lezers, een declaratie: het scherm kan niets beloven wat de save weigert."""
    validator = ConfigSettingValidator(CONNECTIONS)
    assert validator.validate("60") == []
    assert validator.validate("") == []  # leeg valt terug op de standaard van de dienst
    (melding,) = validator.validate("200")
    with pytest.raises(SettingError) as opgeslagen:
        CONNECTIONS.check(200)
    assert melding == str(opgeslagen.value)


def test_een_gesloten_verzameling_kan_een_keuzelijst_dragen() -> None:
    """De widget mag anders, de declaratie blijft wat het veld beoordeelt."""
    veld = setting_field(IMAGE, _DATABASE, ConfigLayer.PROJECT, widget=WidgetType.SELECT, values_provider="images")

    assert veld.widget is WidgetType.SELECT
    assert veld.editable.values_provider == "images"
    assert veld.examples == list(IMAGE.allowed)
    assert veld.editable.validator is not None
    assert veld.editable.validator.validate("docker.io/library/postgres:latest")


def test_een_genest_veld_wijst_naar_de_geneste_plek() -> None:
    veld = setting_field(CPU, _DATABASE, ConfigLayer.PROJECT)
    assert veld.editable.yaml_path == config_path(
        ConfigLayer.PROJECT, _DATABASE, "config", "resources", "limits", "cpu"
    )
    assert veld.widget is WidgetType.TEXT
