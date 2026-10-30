"""De OTAP-markering op een deployment (RC-330).

Gemeten: dat de enum en het schema dezelfde waarden kennen, dat de markering bij een
deployment hoort en het schema er op projectniveau geen toelaat, dat de hoogste markering
van een project wordt afgeleid, dat de migratie naar 2.11 niets invult, en dat het
verlaten van ``productie`` herkend wordt.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from opi.api.v2.models import DeploymentDetail, DeploymentStatus
from opi.api.v2.router import _build_deployment_detail, _empty_live_status
from opi.core.project_environment import (
    DEFAULT_ENVIRONMENT,
    Omgeving,
    deployment_environment,
    effective_environment,
    environment_label,
    environment_markings,
    highest_environment,
    is_production_downgrade,
    log_production_downgrades,
)
from opi.core.project_schema import ProjectSchemaError, validate_project_schema
from opi.services.schema_migration import LATEST_SCHEMA_VERSION_VALUE, migrate_to_latest

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "opi" / "schemas" / "project_v2.json"


def _project(**velden: Any) -> dict[str, Any]:
    return {"name": "demo", "schema-version": LATEST_SCHEMA_VERSION_VALUE, **velden}


def test_enum_en_schema_kennen_dezelfde_waarden() -> None:
    """Twee lijsten voor een gesloten verzameling lopen uiteen; dit houdt ze gelijk."""
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    assert schema["$defs"]["environment"]["enum"] == [value.value for value in Omgeving]


def test_alleen_de_deployment_verwijst_naar_de_definitie() -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    assert schema["$defs"]["deployment"]["properties"]["environment"] == {"$ref": "#/$defs/environment"}
    assert "environment" not in schema["properties"]


def test_een_onbekende_waarde_faalt_de_validatie() -> None:
    data = _project(deployments=[{"name": "d", "cluster": "c", "namespace": "n", "environment": "staging"}])

    with pytest.raises(ProjectSchemaError):
        validate_project_schema(data)


def test_een_markering_op_het_project_wordt_geweigerd() -> None:
    """De markering hoort bij een draaiende instantie, dus op projectniveau is ze fout."""
    data = _project(environment="productie", deployments=[{"name": "d", "cluster": "c", "namespace": "n"}])

    with pytest.raises(ProjectSchemaError):
        validate_project_schema(data)


class TestDeMarkeringVanEenDeployment:
    def test_niets_gezet_geeft_ontwikkeling(self) -> None:
        deployment: dict[str, Any] = {"name": "d"}

        assert deployment_environment(deployment) is None
        assert effective_environment(deployment) is Omgeving.ONTWIKKELING
        assert DEFAULT_ENVIRONMENT is Omgeving.ONTWIKKELING

    def test_een_eigen_markering(self) -> None:
        deployment = {"name": "d", "environment": "productie"}

        assert deployment_environment(deployment) is Omgeving.PRODUCTIE
        assert effective_environment(deployment) is Omgeving.PRODUCTIE

    def test_het_label(self) -> None:
        assert environment_label({"name": "d", "environment": "test"}) == "Test"
        assert environment_label({"name": "d"}) == "Ontwikkeling"


class TestHoogsteMarkeringVanEenProject:
    """Afgeleid uit de deployments, niet opgeslagen."""

    def test_zonder_deployments_is_het_ontwikkeling(self) -> None:
        assert highest_environment(_project()) is Omgeving.ONTWIKKELING

    def test_de_hoogste_van_de_deployments_wint(self) -> None:
        data = _project(
            deployments=[
                {"name": "een", "environment": "test"},
                {"name": "twee", "environment": "productie"},
                {"name": "drie", "environment": "acceptatie"},
            ]
        )

        assert highest_environment(data) is Omgeving.PRODUCTIE

    def test_een_deployment_zonder_markering_telt_als_ontwikkeling(self) -> None:
        data = _project(deployments=[{"name": "een"}, {"name": "twee", "environment": "test"}])

        assert highest_environment(data) is Omgeving.TEST


class TestProductieVerlagen:
    def test_alle_overgangen(self) -> None:
        for before in Omgeving:
            for after in Omgeving:
                verwacht = before is Omgeving.PRODUCTIE and after is not Omgeving.PRODUCTIE
                assert is_production_downgrade(before, after) is verwacht

    def test_een_verlaagde_deployment_wordt_gemeld(self, caplog: pytest.LogCaptureFixture) -> None:
        before = environment_markings(_project(deployments=[{"name": "d", "environment": "productie"}]))
        after = environment_markings(_project(deployments=[{"name": "d", "environment": "test"}]))

        with caplog.at_level("WARNING"):
            log_production_downgrades(before, after, project_name="demo", actor="iemand@example.org")

        meldingen = [record.getMessage() for record in caplog.records]
        assert any(
            "deployment 'd'" in melding and "project 'demo'" in melding and "iemand@example.org" in melding
            for melding in meldingen
        )

    def test_een_deployment_zonder_markering_is_een_verlaging(self, caplog: pytest.LogCaptureFixture) -> None:
        """De markering weghalen brengt de deployment terug op ontwikkeling."""
        before = environment_markings(_project(deployments=[{"name": "d", "environment": "productie"}]))
        after = environment_markings(_project(deployments=[{"name": "d"}]))

        with caplog.at_level("WARNING"):
            log_production_downgrades(before, after, project_name="demo", actor="iemand@example.org")

        assert [record.getMessage() for record in caplog.records] != []

    def test_geen_melding_zonder_verlaging(self, caplog: pytest.LogCaptureFixture) -> None:
        before = environment_markings(_project(deployments=[{"name": "d", "environment": "test"}]))
        after = environment_markings(_project(deployments=[{"name": "d", "environment": "productie"}]))

        with caplog.at_level("WARNING"):
            log_production_downgrades(before, after, project_name="demo", actor="iemand@example.org")

        assert caplog.records == []


def test_de_migratie_naar_de_nieuwe_versie_vult_niets_in() -> None:
    """Besloten: geen markering afleiden. Een 2.9-bestand komt onveranderd op 2.11."""
    data: dict[str, Any] = {
        "name": "demo",
        "schema-version": 2.9,
        "deployments": [
            {"name": "een", "cluster": "c", "namespace": "n"},
            {"name": "twee", "cluster": "c", "namespace": "n"},
        ],
    }

    migrated, changed = migrate_to_latest(data)

    assert changed is True
    assert migrated["schema-version"] == LATEST_SCHEMA_VERSION_VALUE
    assert "environment" not in migrated
    assert all("environment" not in deployment for deployment in migrated["deployments"])
    assert all(effective_environment(deployment) is Omgeving.ONTWIKKELING for deployment in migrated["deployments"])
    validate_project_schema(migrated)


class TestDeDeploymentresponsDraagtDeWaarde:
    """Niet alleen dat het veld er is: welke WAARDE eruit komt.

    De bestaande API-test meet veldaanwezigheid tegen ``DeploymentDetail.model_fields``,
    en die blijft groen als de markering de verkeerde waarde oplevert.
    """

    @staticmethod
    def _detail(project_data: dict[str, Any], deployment: dict[str, Any]) -> DeploymentDetail:
        return _build_deployment_detail(deployment, "demo", project_data, _empty_live_status(DeploymentStatus.Unknown))

    def test_zonder_markering_blijft_het_eigen_veld_leeg(self) -> None:
        data = _project(deployments=[{"name": "d", "cluster": "c", "namespace": "n"}])

        detail = self._detail(data, data["deployments"][0])

        assert detail.environment is None
        assert detail.effective_environment is Omgeving.ONTWIKKELING

    def test_een_eigen_markering_komt_in_beide_velden(self) -> None:
        data = _project(deployments=[{"name": "d", "cluster": "c", "namespace": "n", "environment": "test"}])

        detail = self._detail(data, data["deployments"][0])

        assert detail.environment is Omgeving.TEST
        assert detail.effective_environment is Omgeving.TEST
