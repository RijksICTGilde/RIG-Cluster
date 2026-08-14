"""De toegestane waarden van een configveld staan in het OpenAPI-document.

Gemeten aan de UITVOER (``app.openapi()``, hetzelfde document als ``/openapi.json``) en niet
aan de broncode: een client leest dat document, dus daar moet het antwoord staan. Dat een
veld bestaat is niet genoeg -- de klacht was juist dat je moest raden welke waarden erin
mogen, met sleep-mode als voorbeeld.

De keuzes komen uit dezelfde declaratie die het formulier gebruikt (de ``values_provider``
van de editable). Daarom vergelijken deze tests met die provider en niet met een lijst die
hier is overgetypt: een tweede lijst zou precies het probleem herhalen.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from opi.api.openapi_choices import _CONFIG_ROUTE_RE, CHOICES_KEY, CHOICES_SOURCE_KEY, _resolve, _walk
from opi.core.config import settings
from opi.forms.visualizers.providers import (
    PROVIDER_REGISTRY,
    UNDECLARED_SOURCE,
    OptionsSource,
    StorageSizeOptionsProvider,
    WakeModeOptionsProvider,
)
from opi.server import app
from opi.services.catalog.base import ConfigLayer
from opi.services.catalog.sleep_mode.options import sleep_after_deploy_options, sleep_after_wake_options
from opi.services.registry import SERVICES
from opi.services.services_enums import ServiceType

SLEEP_MODE_PUT = "/api/v2/projects/{project_name}/services/sleep-mode/config/project"


@pytest.fixture(scope="module")
def spec() -> dict[str, Any]:
    return app.openapi()


@pytest.fixture(scope="module")
def schemas(spec: dict[str, Any]) -> dict[str, Any]:
    return spec["components"]["schemas"]


@pytest.fixture(scope="module")
def sleep_mode_properties(spec: dict[str, Any], schemas: dict[str, Any]) -> dict[str, Any]:
    """De velden van de sleep-mode-config zoals een client ze uit het document haalt."""
    body = spec["paths"][SLEEP_MODE_PUT]["put"]["requestBody"]["content"]["application/json"]["schema"]
    return _resolve(body, schemas)["properties"]


def _consts(property_schema: dict[str, Any]) -> list[Any]:
    return [choice["const"] for choice in property_schema[CHOICES_KEY]]


def _titles(property_schema: dict[str, Any]) -> list[str]:
    return [choice["title"] for choice in property_schema[CHOICES_KEY]]


class TestGeserveerdDocument:
    """Over HTTP opgehaald, want dat is wat een client werkelijk krijgt."""

    def test_sleep_mode_staat_met_keuzes_in_openapi_json(self) -> None:
        response = TestClient(app).get("/openapi.json")
        assert response.status_code == 200, response.text
        properties = response.json()["components"]["schemas"]["SleepModeConfig"]["properties"]

        assert properties["wake-mode"]["enum"] == ["auto", "confirm", "manual"]
        # De sleutelnamen staan hier voluit: een client leest ze zo uit het document, dus
        # als ze veranderen is dat een breuk en geen detail.
        assert [choice["const"] for choice in properties["sleep-after-deploy"]["x-choices"]] == [
            option["value"] for option in sleep_after_deploy_options(settings.CLUSTER_MANAGER)
        ]
        assert (
            properties["waker-component"]["x-choices-source"]["endpoint"]
            == "GET /api/v2/projects/{project_name}/components"
        )

    def test_het_document_legt_zijn_eigen_uitbreidingen_uit(self) -> None:
        """x-choices en x-choices-source zijn geen standaardvocabulaire.

        Wie ze tegenkomt zonder uitleg kan er twee kanten mee op, en de verkeerde kant
        (x-choices lezen als validatie) breekt een gegenereerde client op een bestaand
        projectbestand. De uitleg hoort dus in het document zelf te staan.
        """
        description = TestClient(app).get("/openapi.json").json()["info"]["description"]

        assert "x-choices" in description
        assert "x-choices-source" in description
        assert "endpoint" in description


class TestSleepMode:
    """Het geval uit de melding: sleep-mode accepteert per veld een beperkte set."""

    def test_wake_mode_noemt_zijn_waarden_als_enum(self, sleep_mode_properties: dict[str, Any]) -> None:
        # wake-mode is een gesloten keuze (Literal in het configmodel), dus de toegestane
        # waarden horen als enum in het schema en niet in een zin.
        assert sleep_mode_properties["wake-mode"]["enum"] == ["auto", "confirm", "manual"]

    def test_wake_mode_geeft_een_label_per_waarde(self, sleep_mode_properties: dict[str, Any]) -> None:
        wake_mode = sleep_mode_properties["wake-mode"]
        expected = WakeModeOptionsProvider().get_options()
        assert _consts(wake_mode) == [option["value"] for option in expected]
        assert _titles(wake_mode) == [option["label"] for option in expected]
        # De uitleg per keuze reist mee: "auto" zegt niet wat er gebeurt, "Wekt bij het
        # eerste bezoek" wel.
        assert wake_mode[CHOICES_KEY][0]["description"] == expected[0]["description"]

    def test_wake_mode_enum_en_keuzes_zijn_dezelfde_verzameling(self, sleep_mode_properties: dict[str, Any]) -> None:
        wake_mode = sleep_mode_properties["wake-mode"]
        assert set(_consts(wake_mode)) == set(wake_mode["enum"])

    def test_sleep_after_deploy_noemt_de_aangeboden_duren(self, sleep_mode_properties: dict[str, Any]) -> None:
        # Het configmodel accepteert elke duur (48h, 90m, 2d), dus een enum zou hier
        # smaller documenteren dan de API accepteert. De aangeboden lijst staat er wel,
        # met label, zodat een client niet hoeft te raden wat gebruikelijk is.
        expected = sleep_after_deploy_options(settings.CLUSTER_MANAGER)
        field = sleep_mode_properties["sleep-after-deploy"]
        assert _consts(field) == [option["value"] for option in expected]
        assert _titles(field) == [option["label"] for option in expected]
        assert "enum" not in field

    def test_sleep_after_wake_noemt_de_aangeboden_duren(self, sleep_mode_properties: dict[str, Any]) -> None:
        expected = sleep_after_wake_options(settings.CLUSTER_MANAGER)
        field = sleep_mode_properties["sleep-after-wake"]
        assert _consts(field) == [option["value"] for option in expected]
        assert _titles(field) == [option["label"] for option in expected]

    def test_de_standaardwaarde_staat_in_default(self, sleep_mode_properties: dict[str, Any]) -> None:
        # Een standaard hoort in `default`, en hij moet ook echt een van de keuzes zijn.
        assert sleep_mode_properties["sleep-after-deploy"]["default"] == "48h"
        assert sleep_mode_properties["sleep-after-wake"]["default"] == "1h"
        assert sleep_mode_properties["wake-mode"]["default"] == "confirm"
        assert "48h" in _consts(sleep_mode_properties["sleep-after-deploy"])

    def test_waker_component_wijst_de_bron_aan_in_plaats_van_een_vaste_lijst(
        self, sleep_mode_properties: dict[str, Any]
    ) -> None:
        # De componenten verschillen per project, dus een opsomming zou een momentopname
        # van een willekeurig project zijn. Het endpoint dat de lijst levert kan wel.
        field = sleep_mode_properties["waker-component"]
        assert CHOICES_KEY not in field
        source = field[CHOICES_SOURCE_KEY]
        assert source["endpoint"] == "GET /api/v2/projects/{project_name}/components"
        assert source["path"] == "components[].name"
        assert source["description"]

    def test_booleans_krijgen_geen_keuzelijst(self, sleep_mode_properties: dict[str, Any]) -> None:
        # Het formulier toont Ja/Nee, maar de API wil een echte JSON-boolean: de strings
        # "true"/"false" als toegestane waarden opschrijven zou onwaar zijn.
        for name in ("enabled", "waker"):
            field = sleep_mode_properties[name]
            assert field["type"] == "boolean"
            assert CHOICES_KEY not in field
            assert CHOICES_SOURCE_KEY not in field


def _annotated_properties(node: Any, trail: str, found: list[tuple[str, dict[str, Any]]]) -> None:
    """Elke plek in het document die een keuzelijst of een bron draagt."""
    if not isinstance(node, dict):
        return
    if CHOICES_KEY in node or CHOICES_SOURCE_KEY in node:
        found.append((trail, node))
    for key, value in node.items():
        if key in ("properties", "$defs") and isinstance(value, dict):
            for name, child in value.items():
                _annotated_properties(child, f"{trail}/{name}", found)
        elif key == "items":
            _annotated_properties(value, f"{trail}[]", found)
        elif key == "anyOf" and isinstance(value, list):
            for branch in value:
                _annotated_properties(branch, trail, found)


@pytest.fixture(scope="module")
def annotated(schemas: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    found: list[tuple[str, dict[str, Any]]] = []
    for name, schema in schemas.items():
        _annotated_properties(schema, name, found)
    return found


class TestHetHeleDocument:
    def test_er_zijn_keuzelijsten(self, annotated: list[tuple[str, dict[str, Any]]]) -> None:
        # Bewaakt de tests hieronder tegen stil slagen op een leeg document.
        assert len(annotated) > 10

    def test_een_veld_kiest_een_vorm(self, annotated: list[tuple[str, dict[str, Any]]]) -> None:
        offenders = [trail for trail, node in annotated if CHOICES_KEY in node and CHOICES_SOURCE_KEY in node]
        assert not offenders, f"een veld heeft of een vaste lijst of een bron, niet allebei: {offenders}"

    def test_keuzes_en_enum_zeggen_hetzelfde(
        self, annotated: list[tuple[str, dict[str, Any]]], schemas: dict[str, Any]
    ) -> None:
        """Waar het model de keuze zelf kent, moeten formulier en model het eens zijn.

        Dit is de drift-lock tussen de twee: gaat er een waarde uit het configmodel of komt
        er een bij in de provider, dan valt dat hier om in plaats van in een client.
        """
        for trail, node in annotated:
            if CHOICES_KEY not in node:
                continue
            enum = _enum_of(node, schemas)
            if enum is None:
                continue
            assert set(_consts(node)) == set(enum), f"{trail}: keuzelijst en enum lopen uit elkaar"

    def test_een_lege_keuze_is_geen_toegestane_waarde(self, annotated: list[tuple[str, dict[str, Any]]]) -> None:
        # Een lege optie in een formulier betekent "niet ingevuld"; via de API laat je het
        # veld dan weg. Als waarde opschrijven zou een keuze suggereren die niet bestaat.
        offenders = [trail for trail, node in annotated if CHOICES_KEY in node and "" in _consts(node)]
        assert not offenders, f"lege waarde als keuze aangeboden: {offenders}"

    def test_elke_keuze_heeft_een_label(self, annotated: list[tuple[str, dict[str, Any]]]) -> None:
        for trail, node in annotated:
            if CHOICES_KEY not in node:
                continue
            assert all(choice.get("title") for choice in node[CHOICES_KEY]), f"{trail}: keuze zonder label"

    def test_elke_bron_zegt_waar_de_waarden_vandaan_komen(self, annotated: list[tuple[str, dict[str, Any]]]) -> None:
        for trail, node in annotated:
            source = node.get(CHOICES_SOURCE_KEY)
            if source is None:
                continue
            assert source.get("description"), f"{trail}: bron zonder uitleg"
            # Een endpoint zonder plek in het antwoord laat een client alsnog zoeken.
            assert bool(source.get("endpoint")) == bool(source.get("path")), f"{trail}: endpoint en path horen samen"


def _enum_of(node: dict[str, Any], schemas: dict[str, Any]) -> list[Any] | None:
    """De enum van dit veld, ook als hij achter een ``$ref`` of een ``anyOf`` zit."""
    for branch in [node, *node.get("anyOf", [])]:
        resolved = _resolve(branch, schemas)
        if "enum" in resolved:
            return resolved["enum"]
        if "items" in resolved:
            items = _resolve(resolved["items"], schemas)
            if "enum" in items:
                return items["enum"]
    return None


def _config_providers() -> list[tuple[str, str]]:
    """Elke provider die een service-configveld vult, met het veld erbij."""
    found: list[tuple[str, str]] = []
    for service_type, service in SERVICES.items():
        for layer in ConfigLayer:
            found.extend(
                (f"{service_type.value}/{layer.value}:{editable.yaml_path}", editable.values_provider)
                for editable in _walk(service.config_editables(layer))
                if editable.values_provider
            )
    return found


class TestProviderContract:
    """Een provider zegt zelf of zijn lijst vastligt; de documentatie gokt nooit."""

    def test_er_zijn_providers_om_te_toetsen(self) -> None:
        assert len(_config_providers()) > 10

    @pytest.mark.parametrize(("field", "provider_name"), _config_providers(), ids=lambda value: str(value))
    def test_elke_provider_declareert_zijn_bron(self, field: str, provider_name: str) -> None:
        provider_class = PROVIDER_REGISTRY[provider_name]
        source = getattr(provider_class, "options_source", UNDECLARED_SOURCE)
        assert source is None or isinstance(source, OptionsSource), (
            f"{field}: {provider_name} moet 'options_source' declareren -- None als de lijst vastligt, "
            f"een OptionsSource als hij per project verschilt. Zonder declaratie blijven de toegestane "
            f"waarden uit de API-documentatie."
        )


class TestElkeConfigRouteIsBekeken:
    """De annotatie loopt over de gegenereerde configroutes; die moeten gevonden worden."""

    def test_de_sleep_mode_route_matcht(self) -> None:
        match = _CONFIG_ROUTE_RE.match(SLEEP_MODE_PUT)
        assert match is not None
        assert match.group("service") == ServiceType.SLEEP_MODE.value
        assert match.group("target") == ConfigLayer.PROJECT.value

    def test_elk_veld_met_een_vaste_keuzelijst_krijgt_die_ook(self, spec: dict[str, Any], schemas: dict[str, Any]):
        """Geen enkel configveld met een keuzelijst blijft ongedocumenteerd achter.

        Booleans zijn de bewuste uitzondering (zie ``TestSleepMode``), dus die telt niet mee.
        """
        from opi.api.openapi_choices import _is_boolean, _layer_for, _locate, _service_for

        missing: list[str] = []
        for path, item in spec["paths"].items():
            match = _CONFIG_ROUTE_RE.match(path)
            if not match or "put" not in item:
                continue
            body = item["put"]["requestBody"]["content"]["application/json"]["schema"]
            service = _service_for(match.group("service"))
            layer = _layer_for(match.group("target"))
            if service is None or layer is None:
                continue
            for editable in _walk(service.config_editables(layer)):
                if not editable.values_provider:
                    continue
                node = _locate(body, editable.yaml_path, schemas)
                if node is None or _is_boolean(node):
                    continue
                if CHOICES_KEY not in node and CHOICES_SOURCE_KEY not in node:
                    missing.append(f"{match.group('service')}/{match.group('target')}:{editable.yaml_path}")
        assert not missing, f"deze velden hebben een keuzelijst in het formulier maar niet in de API: {missing}"


class TestVoorbeeldenUitHetFormulier:
    """Een veld met een vrij formaat is zonder voorbeeld het lastigst voor een client.

    ``match`` van sleep-mode is een lijst strings; het schema zegt "string" en verder niets,
    terwijl het om een glob op de deploymentnaam gaat. Het formulier toont dat wel. Daarom
    gaan de voorbeelden van de visualizer mee naar het document, als het standaard
    ``examples`` uit JSON Schema.

    Alleen ``examples``, niet ``placeholder``: een placeholder is even vaak een aanwijzing
    ("Naam van de applicatie") als een waarde, en die als voorbeeld publiceren zou een client
    iets voorhouden dat hij niet kan versturen.
    """

    def test_match_toont_hoe_een_patroon_eruitziet(self, sleep_mode_properties: dict[str, Any]) -> None:
        # Op de items en niet op het veld zelf: een voorbeeld is een instantie van het schema
        # waar het op staat, en een instantie van een lijstveld zou een lijst zijn.
        assert sleep_mode_properties["match"]["items"]["examples"] == ["pr-*", "*-preview", "acceptatie"]

    def test_elk_voorbeeld_in_het_formulier_staat_ook_in_de_api(self, spec: dict[str, Any], schemas: dict[str, Any]):
        from opi.api.openapi_choices import _examples_for, _layer_for, _locate, _service_for

        missing: list[str] = []
        for path, item in spec["paths"].items():
            match = _CONFIG_ROUTE_RE.match(path)
            if not match or "put" not in item:
                continue
            body = item["put"]["requestBody"]["content"]["application/json"]["schema"]
            service, layer = _service_for(match.group("service")), _layer_for(match.group("target"))
            if service is None or layer is None:
                continue
            for yaml_path, examples in _examples_for(service, layer).items():
                node = _locate(body, yaml_path, schemas)
                if node is None:
                    continue
                gevonden = node.get("examples") or (node.get("items") or {}).get("examples")
                if gevonden != examples:
                    missing.append(f"{match.group('service')}:{yaml_path}")
        assert not missing, f"deze velden tonen voorbeelden in het formulier maar niet in de API: {missing}"


class TestEenStandaardwaardeIsKiesbaar:
    """Een standaardwaarde die niet in de keuzelijst staat, is een fout aan één van beide.

    Gevonden op de database van een project: het configmodel gaf ``10Gi`` mee terwijl het
    formulier 50Mi tot en met 1Gi aanbood. De standaardwaarde was dus niet te kiezen, en wie
    het veld in de wizard aanraakte kreeg stilzwijgend iets anders dan wat er stond. Dat kan
    omdat de twee verschillende eigenaren hebben: de lijst hangt aan de editable
    (``values_provider``) en de standaardwaarde aan het Pydantic-configmodel. Niemand hield ze
    naast elkaar; deze test doet dat.

    Merk op dat een provider niets afdwingt. De lijst is een aanbod, geen validatie: via de
    API kan een andere waarde er gewoon in. Juist daarom moet wat wij zelf als standaard
    invullen er wél in staan.
    """

    def test_elke_standaardwaarde_staat_in_zijn_eigen_keuzelijst(
        self, annotated: list[tuple[str, dict[str, Any]]]
    ) -> None:
        offenders = [
            f"{trail}: default {node['default']!r} staat niet in {_consts(node)}"
            for trail, node in annotated
            if CHOICES_KEY in node and "default" in node and node["default"] not in _consts(node)
        ]
        assert not offenders, f"een standaardwaarde die je niet kunt kiezen: {offenders}"

    def test_de_startmaat_van_de_opslagdiensten_is_te_kiezen(self) -> None:
        """Hetzelfde voor een waarde die niet uit een configmodel komt.

        persistent-storage en temp-storage schrijven bij het aanzetten een eerste mount in het
        projectbestand (``storage_config``). Die maat komt niet uit een default van het model
        maar uit de dienstdefinitie, dus de test hierboven ziet hem niet.
        """
        maten = {str(option["value"]) for option in StorageSizeOptionsProvider().get_options()}

        for service_type in (ServiceType.PERSISTENT_STORAGE, ServiceType.TEMP_STORAGE):
            start = SERVICES[service_type].definition.storage_config
            assert start is not None
            assert start["size"] in maten, f"{service_type.value} begint op {start['size']}, niet in {sorted(maten)}"
