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
    ruim_gestart = QuantitySetting(
        path="storage",
        layers=(ConfigLayer.PROJECT,),
        default="5Gi",
        minimum="1Gi",
        maximum="10Gi",
        kind=QuantityKind.MEMORY,
        grow_only=True,
        label="Opslag",
    )
    with pytest.raises(SettingError, match="kan alleen omhoog"):
        check_setting_changes([ruim_gestart], {}, {"storage": "1Gi"}, ConfigLayer.PROJECT)
    check_setting_changes([ruim_gestart], {}, {"storage": "8Gi"}, ConfigLayer.PROJECT)


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
