"""De grens op oude versies in een bucket met versioning (RC-380).

Gemeten op de vier plekken waar hij iets doet: de samenvoeging van project en deployment
(``noncurrent_versions.py``, hetzelfde patroon als de connectielimiet van RC-201), de
connector (de bug in de gelezen versioningstaat, en de idempotente lifecycle-regel), de
manager (alleen een bucket MET versioning krijgt de regel) en de deploymentknop.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from opi.connectors.minio_mc import NONCURRENT_VERSION_RULE_ID, MinioConnector, MinioExecutionError
from opi.core.project_schema import ProjectIntegrityError
from opi.forms.visualizers.bridge import editable_to_form_field
from opi.manager.minio_manager import MinioManager
from opi.manager.project_validation import validate_service_configs
from opi.services.catalog.minio.actions import minio_storage_actions
from opi.services.catalog.postgresql_database.connection_limit import deployment_connection_limit
from opi.services.catalog.seaweedfs.actions import seaweedfs_storage_actions
from opi.services.catalog.shared.noncurrent_versions import (
    MAX_NONCURRENT_VERSIONS,
    NONCURRENT_EXPIRE_DAYS,
    NoncurrentVersionRule,
    deployment_noncurrent_rule,
    project_max_noncurrent_versions,
    project_noncurrent_expire_days,
)
from opi.services.registry import get_service
from opi.services.services_enums import ServiceType

_MINIO = ServiceType.MINIO_STORAGE.value

#: Beide object-opslagdiensten dragen dezelfde grens uit dezelfde declaratie, dus het
#: samenvoegen wordt voor beide gemeten.
BEIDE_DIENSTEN = [ServiceType.MINIO_STORAGE, ServiceType.SEAWEEDFS_STORAGE]


def _project(
    project_config: dict[str, Any] | None = None,
    *,
    dienst: str = _MINIO,
    **deployment_configs: dict[str, Any] | None,
) -> dict[str, Any]:
    entry: Any = {"name": dienst, "config": project_config} if project_config is not None else dienst
    return {
        "name": "proj",
        "services": [entry],
        "deployments": [
            {
                "name": name,
                "cluster": "local",
                "services": [{"reference": dienst, "config": config}] if config is not None else [dienst],
            }
            for name, config in (deployment_configs or {"test": None}).items()
        ],
    }


# --- samenvoegen: deployment boven project boven standaard -----------------------


@pytest.mark.parametrize("service", BEIDE_DIENSTEN, ids=lambda s: s.value)
def test_zonder_waarde_geldt_de_standaard(service: ServiceType) -> None:
    rule = deployment_noncurrent_rule(_project(dienst=service.value), "test", service)
    assert rule == NoncurrentVersionRule(max_versions=30, expire_days=None)


@pytest.mark.parametrize("service", BEIDE_DIENSTEN, ids=lambda s: s.value)
def test_de_projectwaarde_geldt_voor_elke_deployment(service: ServiceType) -> None:
    project = _project({"max-noncurrent-versions": 50}, dienst=service.value, test=None, productie={})
    assert deployment_noncurrent_rule(project, "test", service).max_versions == 50
    assert deployment_noncurrent_rule(project, "productie", service).max_versions == 50


@pytest.mark.parametrize("service", BEIDE_DIENSTEN, ids=lambda s: s.value)
def test_een_deployment_overschrijft_alleen_zichzelf(service: ServiceType) -> None:
    project = _project(
        {"max-noncurrent-versions": 50}, dienst=service.value, test=None, pr_250={"max-noncurrent-versions": 5}
    )
    assert deployment_noncurrent_rule(project, "pr_250", service).max_versions == 5
    assert deployment_noncurrent_rule(project, "test", service).max_versions == 50


def test_de_grens_van_de_ene_dienst_raakt_de_andere_niet() -> None:
    """Twee declaraties zouden hier hetzelfde doen; één gedeelde declaratie mag de waarde
    van de andere dienst niet lezen, want het yaml-pad noemt de dienst."""
    project = _project({"max-noncurrent-versions": 5}, dienst=ServiceType.MINIO_STORAGE.value)
    assert deployment_noncurrent_rule(project, "test", ServiceType.MINIO_STORAGE).max_versions == 5
    assert deployment_noncurrent_rule(project, "test", ServiceType.SEAWEEDFS_STORAGE).max_versions == 30


def test_de_leeftijdsgrens_blijft_leeg_zonder_eigen_standaard() -> None:
    assert project_noncurrent_expire_days(_project(), ServiceType.MINIO_STORAGE) is None
    assert deployment_noncurrent_rule(_project(), "test", ServiceType.MINIO_STORAGE).expire_days is None


def test_een_leeftijdsgrens_wordt_meegenomen() -> None:
    project = _project({"max-noncurrent-versions": 50, "noncurrent-expire-days": 90}, test=None)
    rule = deployment_noncurrent_rule(project, "test", ServiceType.MINIO_STORAGE)
    assert rule == NoncurrentVersionRule(max_versions=50, expire_days=90)


def test_project_helpers_lezen_de_projectlaag() -> None:
    project = _project({"max-noncurrent-versions": 10, "noncurrent-expire-days": 7})
    assert project_max_noncurrent_versions(project, ServiceType.MINIO_STORAGE) == 10
    assert project_noncurrent_expire_days(project, ServiceType.MINIO_STORAGE) == 7


# --- de speelruimte ----------------------------------------------------------------


@pytest.mark.parametrize("waarde", [0, 1001])
def test_het_aantal_buiten_de_speelruimte_wordt_geweigerd(waarde: int) -> None:
    from opi.services.catalog.config_settings import SettingError

    with pytest.raises(SettingError):
        MAX_NONCURRENT_VERSIONS.check(waarde)


@pytest.mark.parametrize("waarde", [0, 3651])
def test_de_leeftijd_buiten_de_speelruimte_wordt_geweigerd(waarde: int) -> None:
    from opi.services.catalog.config_settings import SettingError

    with pytest.raises(SettingError):
        NONCURRENT_EXPIRE_DAYS.check(waarde)


@pytest.mark.parametrize("laag", ["project", "deployment"])
def test_buiten_de_speelruimte_wordt_geweigerd_bij_het_opslaan(laag: str) -> None:
    """Niet alleen ``check()`` zelf, maar de volledige weg via ``validate_service_configs``,
    net als de connectielimiet (RC-201): een afgewezen waarde blokkeert het hele bestand."""
    config = {"max-noncurrent-versions": 1001}
    project = _project(config) if laag == "project" else _project({}, test=config)
    with pytest.raises(ProjectIntegrityError):
        validate_service_configs(project)


def test_een_geldige_waarde_op_beide_lagen_valideert() -> None:
    validate_service_configs(_project({"max-noncurrent-versions": 1000}, test={"max-noncurrent-versions": 1}))


def test_de_grenzen_staan_alleen_in_de_declaratie() -> None:
    assert (MAX_NONCURRENT_VERSIONS.minimum, MAX_NONCURRENT_VERSIONS.maximum, MAX_NONCURRENT_VERSIONS.default) == (
        1,
        1000,
        30,
    )
    assert (NONCURRENT_EXPIRE_DAYS.minimum, NONCURRENT_EXPIRE_DAYS.maximum, NONCURRENT_EXPIRE_DAYS.default) == (
        1,
        3650,
        None,
    )


# --- de connector: de bug in de gelezen versioningstaat ---------------------------


async def _versioning(connector: MinioConnector, stdout: str) -> dict[str, Any]:
    with patch.object(MinioConnector, "_run_mc_command", AsyncMock(return_value=(stdout, "", 0))):
        return await connector.get_bucket_versioning_status("alias", "bucket")


@pytest.mark.asyncio
async def test_een_geneste_versioningstaat_wordt_herkend() -> None:
    """De vorm die mc RELEASE.2025-08-13 teruggeeft: ``status`` is van het commando, de
    bucketstaat staat onder ``versioning``. Zonder de fix leest dit als onversioneerd."""
    connector = MinioConnector()
    status = await _versioning(connector, '{"status": "success", "versioning": {"status": "Enabled"}}')
    assert status == {"enabled": True, "suspended": False, "status": "Enabled"}


@pytest.mark.asyncio
async def test_een_platte_versioningstaat_wordt_nog_steeds_herkend() -> None:
    """Een oudere mc-vorm zonder het ``versioning``-blok blijft werken."""
    connector = MinioConnector()
    status = await _versioning(connector, '{"status": "Suspended"}')
    assert status == {"enabled": False, "suspended": True, "status": "Suspended"}


@pytest.mark.asyncio
async def test_geen_output_is_onversioneerd() -> None:
    connector = MinioConnector()
    status = await _versioning(connector, "")
    assert status == {"enabled": False, "suspended": False, "status": "Unversioned"}


# --- de connector: de idempotente lifecycle-regel ---------------------------------


@pytest.mark.asyncio
async def test_een_bucket_zonder_regels_krijgt_de_regel_aangemaakt() -> None:
    connector = MinioConnector()
    with patch.object(MinioConnector, "_run_mc_command", AsyncMock()) as run_mock:
        run_mock.side_effect = [("", "lifecycle configuration does not exist for this bucket", 1), ("", "", 0)]
        result = await connector.ensure_noncurrent_version_rule("alias", "bucket", max_versions=30)

    assert result["status"] == "created"
    import_call = run_mock.await_args_list[-1]
    assert import_call.args[0] == ["ilm", "rule", "import", "alias/bucket"]
    document = import_call.kwargs["stdin_input"]
    assert f'"ID": "{NONCURRENT_VERSION_RULE_ID}"' in document
    assert '"NewerNoncurrentVersions": 30' in document


@pytest.mark.asyncio
async def test_een_gelijke_regel_opnieuw_zetten_schrijft_niets() -> None:
    connector = MinioConnector()
    existing = (
        f'{{"Rules": [{{"ID": "{NONCURRENT_VERSION_RULE_ID}", "Status": "Enabled", '
        '"Expiration": {"ExpiredObjectDeleteMarker": true}, '
        '"NoncurrentVersionExpiration": {"NewerNoncurrentVersions": 30}}]}'
    )
    with patch.object(MinioConnector, "_run_mc_command", AsyncMock(return_value=(existing, "", 0))) as run_mock:
        result = await connector.ensure_noncurrent_version_rule("alias", "bucket", max_versions=30)

    assert result["status"] == "unchanged"
    run_mock.assert_awaited_once()  # alleen de export, geen import


@pytest.mark.asyncio
async def test_een_andere_waarde_vervangt_alleen_de_eigen_regel() -> None:
    """Een andere regel op de bucket (een ander ID) blijft staan naast OPI's eigen regel."""
    connector = MinioConnector()
    existing = (
        '{"Rules": ['
        '{"ID": "iemand-anders", "Status": "Enabled", "Expiration": {"Days": 7}}, '
        f'{{"ID": "{NONCURRENT_VERSION_RULE_ID}", "Status": "Enabled", '
        '"Expiration": {"ExpiredObjectDeleteMarker": true}, '
        '"NoncurrentVersionExpiration": {"NewerNoncurrentVersions": 10}}'
        "]}"
    )
    with patch.object(MinioConnector, "_run_mc_command", AsyncMock()) as run_mock:
        run_mock.side_effect = [(existing, "", 0), ("", "", 0)]
        result = await connector.ensure_noncurrent_version_rule("alias", "bucket", max_versions=30, expire_days=90)

    assert result["status"] == "updated"
    document = run_mock.await_args_list[-1].kwargs["stdin_input"]
    assert '"ID": "iemand-anders"' in document
    assert '"NewerNoncurrentVersions": 30' in document
    assert '"NoncurrentDays": 90' in document


@pytest.mark.asyncio
async def test_een_mislukte_import_geeft_een_connectorfout() -> None:
    connector = MinioConnector()
    with patch.object(MinioConnector, "_run_mc_command", AsyncMock()) as run_mock:
        run_mock.side_effect = [
            ("", "lifecycle configuration does not exist for this bucket", 1),
            ("", "access denied", 1),
        ]
        with pytest.raises(MinioExecutionError, match="access denied"):
            await connector.ensure_noncurrent_version_rule("alias", "bucket", max_versions=30)


@pytest.mark.asyncio
async def test_een_bucket_zonder_lifecycle_geeft_een_lege_lijst() -> None:
    connector = MinioConnector()
    with patch.object(MinioConnector, "_run_mc_command", AsyncMock(return_value=("", "lifecycle not found", 1))):
        assert await connector.get_bucket_lifecycle("alias", "bucket") == []


# --- de manager: alleen een bucket MET versioning krijgt de regel -----------------


def _manager() -> MinioManager:
    from types import SimpleNamespace

    project_manager = SimpleNamespace(get_name=AsyncMock(return_value="proj"))
    return MinioManager(project_manager=project_manager)


@pytest.mark.asyncio
async def test_een_bucket_zonder_versioning_krijgt_geen_regel() -> None:
    mgr = _manager()
    connector = AsyncMock()
    connector.get_bucket_versioning_status.return_value = {
        "enabled": False,
        "suspended": False,
        "status": "Unversioned",
    }

    await mgr._apply_noncurrent_version_limit(connector, "alias", "bucket", _project(), "test")

    connector.ensure_noncurrent_version_rule.assert_not_awaited()


@pytest.mark.asyncio
async def test_een_bucket_met_versioning_krijgt_de_opgeloste_grens() -> None:
    mgr = _manager()
    connector = AsyncMock()
    connector.get_bucket_versioning_status.return_value = {"enabled": True, "suspended": False, "status": "Enabled"}
    connector.ensure_noncurrent_version_rule.return_value = {"status": "created", "rule": {}}

    project = _project({"max-noncurrent-versions": 7}, test=None)
    await mgr._apply_noncurrent_version_limit(connector, "alias", "bucket", project, "test")

    connector.ensure_noncurrent_version_rule.assert_awaited_once_with(
        "alias", "bucket", max_versions=7, expire_days=None
    )


@pytest.mark.asyncio
async def test_een_connectorfout_wordt_een_runtimefout() -> None:
    mgr = _manager()
    connector = AsyncMock()
    connector.get_bucket_versioning_status.return_value = {"enabled": True, "suspended": False, "status": "Enabled"}
    connector.ensure_noncurrent_version_rule.side_effect = MinioExecutionError("mc onbereikbaar")

    with pytest.raises(RuntimeError, match="mc onbereikbaar"):
        await mgr._apply_noncurrent_version_limit(connector, "alias", "bucket", _project(), "test")


# --- de knop -------------------------------------------------------------------------


def test_de_deploymentkaart_krijgt_een_knop_naar_oude_versies() -> None:
    project = _project({}, test=None, productie=None)
    (knop,) = minio_storage_actions(project, "productie")
    assert knop.label == "Oude versies"
    assert knop.modal_endpoint == "/projects/proj/modal-wizard/modal-edit-minio-deployment-1"
    assert knop.modal_title == "Oude versies - productie"


@pytest.mark.parametrize(
    ("project", "deployment"),
    [
        ({**_project({}), "services": ["publish-on-web"]}, "test"),
        (_project({}), "bestaat-niet"),
    ],
)
def test_geen_knop_zonder_de_dienst_of_zonder_deployment(project: dict[str, Any], deployment: str) -> None:
    assert minio_storage_actions(project, deployment) == []


def test_de_deploymentkaart_van_seaweedfs_krijgt_dezelfde_knop() -> None:
    """Zelfde knop als bij minio, maar met het eigen modal-voorvoegsel (RC-380)."""
    project = _project({}, dienst=ServiceType.SEAWEEDFS_STORAGE.value, test=None, productie=None)
    (knop,) = seaweedfs_storage_actions(project, "productie")
    assert knop.label == "Oude versies"
    assert knop.modal_endpoint == "/projects/proj/modal-wizard/modal-edit-seaweedfs-deployment-1"
    assert knop.modal_title == "Oude versies - productie"


@pytest.mark.parametrize(
    ("project", "deployment"),
    [
        (
            {**_project({}, dienst=ServiceType.SEAWEEDFS_STORAGE.value), "services": ["publish-on-web"]},
            "test",
        ),
        (_project({}, dienst=ServiceType.SEAWEEDFS_STORAGE.value), "bestaat-niet"),
    ],
)
def test_geen_seaweedfs_knop_zonder_de_dienst_of_zonder_deployment(project: dict[str, Any], deployment: str) -> None:
    assert seaweedfs_storage_actions(project, deployment) == []


# --- de deploymentmodal: elke dienst zijn eigen "volg het project" --------------------


@pytest.mark.parametrize(
    "service",
    BEIDE_DIENSTEN,
    ids=lambda s: s.value,
)
def test_het_empty_label_van_de_deploymentmodal_leest_de_eigen_projectwaarde(service: ServiceType) -> None:
    """De OptionsProvider van elke dienst (RC-380) kreeg een eigen ``service``-ClassVar;
    zonder die scheiding zou de modal van de ene dienst de projectwaarde van de andere
    tonen, want de velden delen dezelfde declaratie."""
    project = _project({"max-noncurrent-versions": 42}, dienst=service.value, test=None)
    sectie = get_service(service).deployment_form_section(0)
    veld = editable_to_form_field(sectie.editables[0], project, edit_mode=True)
    assert veld.options[0] == {"value": "", "label": "Volg het project (42)"}


def test_het_empty_label_van_minio_leest_niet_de_projectwaarde_van_seaweedfs() -> None:
    project = {
        "name": "proj",
        "services": [
            {"name": _MINIO, "config": {"max-noncurrent-versions": 10}},
            {"name": ServiceType.SEAWEEDFS_STORAGE.value, "config": {"max-noncurrent-versions": 99}},
        ],
        "deployments": [{"name": "test", "cluster": "local", "services": [_MINIO]}],
    }
    sectie = get_service(ServiceType.MINIO_STORAGE).deployment_form_section(0)
    veld = editable_to_form_field(sectie.editables[0], project, edit_mode=True)
    assert veld.options[0]["label"] == "Volg het project (10)"


# --- controle: de postgresql-knop is er niet ongemerkt bijgekomen ----------------


def test_de_connectielimiet_blijft_ongewijzigd_door_deze_toevoeging() -> None:
    """``service_setting_value`` deelt de implementatie met de connectielimiet; dit is de
    canary dat de refactor in ``connection_limit.py`` niets heeft verschoven."""
    project = {
        "name": "proj",
        "services": [{"name": "postgresql-database", "config": {"connection-limit": 42}}],
        "deployments": [{"name": "test", "cluster": "local", "services": ["postgresql-database"]}],
    }
    assert deployment_connection_limit(project, "test") == 42
