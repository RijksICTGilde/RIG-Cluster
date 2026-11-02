"""De velden van een eigen (CNPG) PostgreSQL-cluster (RC-327 stap 5).

``DedicatedPostgresFields`` toetst de VORM (image-charset); ``DEDICATED_POSTGRES_SETTINGS``
(via het mechanisme van RC-168) toetst de SPEELRUIMTE: grenzen op storage en resources, en
dat een volume niet krimpt. Beide diensten die het veld gebruiken delen die declaraties
(``postgresql_database/__init__.py`` en ``namespace_postgres/__init__.py``).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from opi.core.project_schema import ProjectIntegrityError
from opi.forms.visualizers.bridge import editable_to_form_field
from opi.forms.visualizers.providers import PostgresStorageSizeOptionsProvider
from opi.manager.project_validation import validate_project_structure, validate_service_configs
from opi.services.catalog.namespace_postgres.visualizers import POSTGRESQL_STORAGE
from opi.services.catalog.shared.postgres import DedicatedPostgresFields
from opi.services.catalog.shared.postgres_settings import POSTGRES_RESOURCES, POSTGRES_STORAGE
from opi.services.catalog.shared.storage import STORAGE_SIZES
from pydantic import ValidationError

_PG = "postgresql-database"


def _project(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": "proj",
        "services": [{"name": _PG, "config": {"scope": "project", **config}}],
        "deployments": [{"name": "test", "cluster": "local", "services": [_PG]}],
    }


# --- image: VORM op het model -----------------------------------------------------


@pytest.mark.parametrize(
    "image",
    [
        "ghcr.io/cloudnative-pg/postgresql:17",
        "ghcr.io/rijksictgilde/algoritmeregister/postgresql-with-dictionaries:2024.11.19",
        "postgres",
    ],
)
def test_een_normale_image_passeert(image: str) -> None:
    model = DedicatedPostgresFields(image=image)
    assert model.image == image


@pytest.mark.parametrize("image", ["nginx; rm -rf /", "nginx latest", "nginx\nprivileged: true"])
def test_een_image_met_shell_of_yaml_metatekens_wordt_geweigerd(image: str) -> None:
    with pytest.raises(ValidationError):
        DedicatedPostgresFields(image=image)


def test_een_lege_image_wordt_geweigerd() -> None:
    with pytest.raises(ValidationError):
        DedicatedPostgresFields(image="")


# --- storage: SPEELRUIMTE via config_settings --------------------------------------


def test_de_standaardwaarde_past_binnen_de_grenzen() -> None:
    assert POSTGRES_STORAGE.check(POSTGRES_STORAGE.default) == POSTGRES_STORAGE.default


def test_storage_binnen_de_speelruimte_mag() -> None:
    validate_service_configs(_project({"storage": "50Mi"}))
    validate_service_configs(_project({"storage": "20Gi"}))


@pytest.mark.parametrize("waarde", ["1Mi", "21Gi"])
def test_storage_buiten_de_speelruimte_wordt_geweigerd_bij_het_inlezen(waarde: str) -> None:
    with pytest.raises(ProjectIntegrityError):
        validate_service_configs(_project({"storage": waarde}))


def test_storage_met_een_regeleinde_wordt_geweigerd() -> None:
    with pytest.raises(ProjectIntegrityError):
        validate_service_configs(_project({"storage": "2Gi\n"}))


def test_een_kleinere_storage_dan_de_vorige_versie_wordt_geweigerd() -> None:
    vorige = _project({"storage": "2Gi"})
    nieuwe = _project({"storage": "1Gi"})
    with pytest.raises(ProjectIntegrityError):
        asyncio.run(validate_project_structure(nieuwe, previous=vorige))


def test_een_grotere_of_gelijke_storage_dan_de_vorige_versie_mag() -> None:
    vorige = _project({"storage": "1Gi"})
    asyncio.run(validate_project_structure(_project({"storage": "2Gi"}), previous=vorige))
    asyncio.run(validate_project_structure(_project({"storage": "1Gi"}), previous=vorige))


# --- resources: SPEELRUIMTE via config_settings ------------------------------------


def test_de_vier_resource_velden_staan_op_projectlaag() -> None:
    for setting in POSTGRES_RESOURCES:
        assert setting.path.startswith("resources.")


@pytest.mark.parametrize(
    "config",
    [
        {"resources": {"requests": {"memory": "25Mi"}}},
        {"resources": {"limits": {"memory": "4096Mi"}}},
        {"resources": {"requests": {"cpu": "25m"}}},
        {"resources": {"limits": {"cpu": "4000m"}}},
    ],
)
def test_een_waarde_binnen_de_grenzen_passeert(config: dict[str, Any]) -> None:
    validate_service_configs(_project(config))


@pytest.mark.parametrize(
    "config",
    [
        {"resources": {"requests": {"memory": "10Mi"}}},  # onder de ondergrens
        {"resources": {"limits": {"memory": "8192Mi"}}},  # boven de bovengrens
        {"resources": {"requests": {"cpu": "1000m"}}},  # boven de bovengrens van requests
    ],
)
def test_een_waarde_buiten_de_grenzen_wordt_geweigerd(config: dict[str, Any]) -> None:
    with pytest.raises(ProjectIntegrityError):
        validate_service_configs(_project(config))


def test_een_dienst_zonder_cnpg_config_blijft_ongemoeid() -> None:
    validate_service_configs({"name": "proj", "services": [{"name": _PG, "config": {"connection-limit": 20}}]})


# --- het formulier: de keuzelijst kan de opgeslagen waarde uitdrukken ---------------

_NS_PG = "namespace-postgresql-database"


def _ns_project(storage: str) -> dict[str, Any]:
    return {
        "name": "proj",
        "services": [{"name": _NS_PG, "config": {"instances": 1, "storage": storage}}],
        "deployments": [{"name": "test", "cluster": "local", "services": [_NS_PG]}],
    }


def test_de_keuzelijst_toont_de_maten_van_het_platform() -> None:
    opties = PostgresStorageSizeOptionsProvider().get_options()
    assert [optie["value"] for optie in opties] == list(STORAGE_SIZES)


def test_een_opgeslagen_maat_boven_de_lijst_blijft_te_kiezen() -> None:
    """De speelruimte loopt tot 20Gi en de lijst tot 1Gi, dus zonder deze regel biedt het
    formulier een bestaand 2Gi-volume alleen krimp aan."""
    veld = editable_to_form_field(POSTGRESQL_STORAGE, _ns_project("2Gi"), edit_mode=True)
    assert veld.value == "2Gi"
    assert {"value": "2Gi", "label": "2Gi (eigen waarde)"} in veld.options


def test_wat_het_formulier_verstuurt_wordt_ook_opgeslagen() -> None:
    """De waarde die de select verstuurt mag niet op ``grow_only`` stranden.

    Een select kan geen waarde versturen die hij niet aanbiedt: staat de opgeslagen maat
    niet in de lijst, dan verstuurt de browser de eerste optie. Dat was hier de kleinste.
    """
    vorige = _ns_project("2Gi")
    veld = editable_to_form_field(POSTGRESQL_STORAGE, vorige, edit_mode=True)
    keuzes = [str(optie["value"]) for optie in veld.options]
    verstuurd = str(veld.value) if str(veld.value) in keuzes else keuzes[0]

    asyncio.run(validate_project_structure(_ns_project(verstuurd), previous=vorige))
