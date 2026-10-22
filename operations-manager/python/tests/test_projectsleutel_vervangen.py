"""De projectsleutel vervangen: de omzetting, de afbreekregel en de admin-route.

Wat hier gemeten wordt, in de termen van het plan:

* elk veld dat op de OUDE projectsleutel stond gaat naar de nieuwe, met dezelfde plaintext;
* de velden op de platformsleutel en de platte waarden blijven byte voor byte gelijk;
* een veld dat na de omzetting nog met de oude sleutel opengaat breekt de hele omzetting af;
* de route zit achter de ADMIN_API_KEY en zet een taak in de wachtrij;
* de taak slaat een projectbestand op met een andere ``age-public-key`` en verwerkt daarna;
* een onbekend project, een mislukte omzetting, een mislukte opslag of een mislukte
  herverwerking laat niets half achter en meldt de stap waarin het misging (plan, punt 5).
"""

from __future__ import annotations

import copy
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import opi.core.config
import opi.utils.age as age_module
import pytest
from fastapi import HTTPException
from opi.api import endpoint_util
from opi.api.v2 import router as v2_router_module
from opi.connectors.kopia import KopiaConnector, KopiaExecutionError, KopiaRepositoryConfig, _is_missing_repository
from opi.core import task_handlers_operations as handlers
from opi.core.cluster_config import get_prefixed_namespace
from opi.handlers.sops import format_age_key_file
from opi.manager.backup.base import BaseBackupManager, derive_backup_password
from opi.services.project_store import ProjectNotFoundError, ProjectStoreError
from opi.utils import key_rotation
from opi.utils.age import encrypt_age_content
from opi.utils.key_rotation import ConversionFailed, decrypt_field, opens_with, rotate_project_key
from opi.utils.sops import generate_sops_key_pair


@pytest.fixture
def platform_pair() -> tuple[str, str]:
    return generate_sops_key_pair()


@pytest.fixture
def project_pair() -> tuple[str, str]:
    return generate_sops_key_pair()


async def _project_file(platform_pair: tuple[str, str], project_pair: tuple[str, str]) -> dict[str, Any]:
    """Een projectbestand met de drie soorten velden die de omzetting moet onderscheiden."""
    platform_private, platform_public = platform_pair
    project_private, project_public = project_pair
    return {
        "name": "demo",
        "schema-version": 2.9,
        "components": [
            {
                "name": "component-1",
                # op de projectsleutel, in de gepantserde vorm
                "user-env-vars": await encrypt_age_content("TOKEN=hunter2", project_public),
                # plat: moet precies blijven staan
                "type": "single",
            }
        ],
        "repositories": [
            {
                "name": "main-repo",
                # op de platformsleutel: gaat niet mee
                "password": await encrypt_age_content("ghp_token", platform_public),
            }
        ],
        "config": {
            "age-public-key": project_public,
            "age-private-key": await encrypt_age_content(project_private, platform_public),
            "api-key": await encrypt_age_content("zad_api_key", project_public),
        },
    }


@pytest.mark.asyncio
async def test_elk_veld_op_de_projectsleutel_gaat_mee_en_de_rest_blijft_staan(
    platform_pair: tuple[str, str], project_pair: tuple[str, str]
) -> None:
    new_private, new_public = generate_sops_key_pair()
    project_data = await _project_file(platform_pair, project_pair)
    before = copy.deepcopy(project_data)

    rotation = await rotate_project_key(project_data, project_pair[0], new_public)

    assert rotation.rotated == ["components[0].user-env-vars", "config.api-key"]
    assert rotation.skipped == ["repositories[0].password", "config.age-private-key"]

    rotated_env = rotation.data["components"][0]["user-env-vars"]
    assert await decrypt_field(rotated_env, new_private) == "TOKEN=hunter2"
    assert not await opens_with(rotated_env, project_pair[0])
    assert await decrypt_field(rotation.data["config"]["api-key"], new_private) == "zad_api_key"

    # De platformvelden en de platte waarden byte voor byte gelijk, en het meegegeven
    # bestand onaangeroerd: de functie werkt op een kopie.
    assert rotation.data["repositories"][0]["password"] == before["repositories"][0]["password"]
    assert rotation.data["config"]["age-private-key"] == before["config"]["age-private-key"]
    assert rotation.data["components"][0]["type"] == "single"
    assert project_data == before


@pytest.mark.asyncio
async def test_een_veld_dat_niet_omgezet_kan_worden_breekt_de_hele_omzetting_af(
    platform_pair: tuple[str, str], project_pair: tuple[str, str]
) -> None:
    _, new_public = generate_sops_key_pair()
    project_data = await _project_file(platform_pair, project_pair)
    before = copy.deepcopy(project_data)

    with (
        patch.object(key_rotation, "convert_value", AsyncMock(side_effect=ConversionFailed("gaat niet open"))),
        pytest.raises(ConversionFailed),
    ):
        await rotate_project_key(project_data, project_pair[0], new_public)

    assert project_data == before


@pytest.mark.asyncio
async def test_een_veld_dat_de_oude_sleutel_nog_opent_is_een_fout(
    platform_pair: tuple[str, str], project_pair: tuple[str, str]
) -> None:
    """De naronde telt opnieuw: een omzetting die de waarde liet staan mag niet doorgaan."""
    _, new_public = generate_sops_key_pair()
    project_data = await _project_file(platform_pair, project_pair)
    unchanged = MagicMock(new_value=project_data["config"]["api-key"])

    with (
        patch.object(key_rotation, "convert_value", AsyncMock(return_value=unchanged)),
        pytest.raises(ConversionFailed, match="nog met de oude projectsleutel open"),
    ):
        await rotate_project_key(project_data, project_pair[0], new_public)


@pytest.fixture
def admin_key(monkeypatch: pytest.MonkeyPatch) -> str:
    """Zet de ADMIN_API_KEY op het levende settings-object.

    ``endpoint_util`` bindt ``settings`` bij import, dus in een volle run kan het naar de
    mock van een andere toets wijzen; dezelfde guard als in test_admin_projects_reconcile.
    """
    real_settings = opi.core.config.settings
    monkeypatch.setattr(endpoint_util, "settings", real_settings)
    monkeypatch.setattr(real_settings, "ADMIN_API_KEY", "test-admin-key")
    return "test-admin-key"


@pytest.mark.asyncio
async def test_de_route_weigert_zonder_admin_sleutel(admin_key: str) -> None:
    request = AsyncMock()
    request.headers = {"X-API-Key": "de-projectsleutel-van-een-lid"}

    with patch.object(v2_router_module, "create_async_task", AsyncMock()) as create, pytest.raises(HTTPException) as e:
        await v2_router_module.rotate_project_key_v2(request=request, project_name="demo")

    assert e.value.status_code == 401
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_de_route_zet_met_de_admin_sleutel_een_taak_in_de_wachtrij(admin_key: str) -> None:
    request = AsyncMock()
    request.headers = {"X-API-Key": admin_key}

    with patch.object(v2_router_module, "create_async_task", AsyncMock(return_value={"task_id": "taak-1"})) as create:
        response = await v2_router_module.rotate_project_key_v2(request=request, project_name="demo")

    assert response.status_code == 202
    assert create.await_args.kwargs["task_type"] == "rotate_project_key"
    assert create.await_args.kwargs["payload"] == {"project_name": "demo"}


@pytest.mark.asyncio
async def test_de_route_weigert_een_ongeldige_projectnaam_ook_met_de_admin_sleutel(admin_key: str) -> None:
    request = AsyncMock()
    request.headers = {"X-API-Key": admin_key}

    with (
        patch.object(v2_router_module, "create_async_task", AsyncMock()) as create,
        pytest.raises(HTTPException) as e,
    ):
        await v2_router_module.rotate_project_key_v2(request=request, project_name="UPPERCASE")

    assert e.value.status_code == 400
    create.assert_not_awaited()


async def _project_file_met_deployment(
    platform_pair: tuple[str, str], project_pair: tuple[str, str], cluster: str
) -> dict[str, Any]:
    """Hetzelfde projectbestand, met één deployment op ``cluster``."""
    project_data = await _project_file(platform_pair, project_pair)
    project_data["deployments"] = [{"name": "productie", "cluster": cluster, "namespace": "demo"}]
    return project_data


def _rotatie_omgeving(
    monkeypatch: pytest.MonkeyPatch,
    platform_pair: tuple[str, str],
    project_data: dict[str, Any],
    kek_pair: tuple[str, str] | None = None,
) -> MagicMock:
    """Settings op de testsleutels en een store waarvan ``mutate`` de omzetting echt draait.

    Dat is de weg die de taak loopt: de store leest de verse staat uit git en geeft die aan de
    wijzigingsfunctie. Het resultaat komt in ``store.mutated``, de commitboodschap in
    ``store.commit_message``.

    ``kek_pair`` zet de project-KEK; zonder dat staat hij uit, zoals op de sandbox.
    """
    platform_private, platform_public = platform_pair
    kek_private, kek_public = kek_pair or (None, None)
    real_settings = opi.core.config.settings
    monkeypatch.setattr(handlers, "settings", real_settings)
    monkeypatch.setattr(real_settings, "SOPS_AGE_PRIVATE_KEY", platform_private)
    monkeypatch.setattr(real_settings, "SOPS_AGE_PUBLIC_KEY", platform_public)
    monkeypatch.setattr(age_module.settings, "PROJECT_KEK_PRIVATE_KEY", kek_private, raising=False)
    monkeypatch.setattr(age_module.settings, "PROJECT_KEK_PUBLIC_KEY", kek_public, raising=False)

    store = MagicMock()
    store.reconcile = AsyncMock()
    store.get = MagicMock(return_value=MagicMock(filename="demo.yaml"))
    store.mutated = None
    store.commit_message = None

    async def mutate(name: str, change: Any, **kwargs: Any) -> MagicMock:
        store.mutated = await change(copy.deepcopy(project_data))
        message = kwargs["message"]
        store.commit_message = message() if callable(message) else message
        return MagicMock(after=store.mutated)

    store.mutate = AsyncMock(side_effect=mutate)
    return store


@pytest.mark.asyncio
async def test_de_taak_slaat_een_nieuwe_sleutel_op_en_verwerkt_het_project(
    platform_pair: tuple[str, str], project_pair: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Zonder project-KEK gaat de nieuwe sleutel onder de platformsleutel, zoals op de sandbox."""
    platform_private, _ = platform_pair
    project_data = await _project_file(platform_pair, project_pair)
    new_private, new_public = generate_sops_key_pair()
    store = _rotatie_omgeving(monkeypatch, platform_pair, project_data)
    refresh = AsyncMock(return_value={"status": "success", "processing": {"status": "completed"}})

    with (
        patch.object(handlers, "get_project_store", return_value=store),
        patch.object(handlers, "generate_sops_key_pair", return_value=(new_private, new_public)),
        patch.object(handlers, "handle_refresh_project", refresh),
    ):
        result = await handlers.handle_rotate_project_key({"project_name": "demo"}, MagicMock())

    assert result["status"] == "success"
    assert result["public_key"] == new_public
    assert result["rotated_fields"] == ["components[0].user-env-vars", "config.api-key"]
    refresh.assert_awaited_once()

    saved = store.mutated
    assert saved["config"]["age-public-key"] == new_public
    assert await decrypt_field(saved["config"]["age-private-key"], platform_private) == new_private
    assert await decrypt_field(saved["config"]["api-key"], new_private) == "zad_api_key"
    # Het aantal omgezette velden staat in de commitboodschap.
    assert "2 velden omgezet" in store.commit_message


@pytest.mark.asyncio
async def test_met_een_project_kek_komt_de_nieuwe_sleutel_onder_de_kek_en_niet_onder_het_platform(
    platform_pair: tuple[str, str], project_pair: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """De nieuwe projectsleutel hoort onder de project-KEK (RC-334), niet onder de platformsleutel.

    Zonder deze toets glipt een rotatie die ``encrypt_age_content(..., SOPS_AGE_PUBLIC_KEY)``
    gebruikt er stil doorheen en zet de sleutel terug op de platformsleutel.
    """
    platform_private, _ = platform_pair
    kek_private, kek_public = generate_sops_key_pair()
    project_data = await _project_file(platform_pair, project_pair)
    new_private, new_public = generate_sops_key_pair()
    store = _rotatie_omgeving(monkeypatch, platform_pair, project_data, kek_pair=(kek_private, kek_public))

    with (
        patch.object(handlers, "get_project_store", return_value=store),
        patch.object(handlers, "generate_sops_key_pair", return_value=(new_private, new_public)),
        patch.object(handlers, "handle_refresh_project", AsyncMock(return_value={"status": "success"})),
    ):
        result = await handlers.handle_rotate_project_key({"project_name": "demo"}, MagicMock())

    assert result["status"] == "success"
    wrapped = store.mutated["config"]["age-private-key"]
    assert await decrypt_field(wrapped, kek_private) == new_private
    assert await decrypt_field(wrapped, platform_private) is None


@pytest.mark.asyncio
async def test_een_veld_dat_er_tijdens_de_rotatie_bij_kwam_gaat_ook_mee(
    platform_pair: tuple[str, str], project_pair: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """De omzetting loopt op de verse staat die ``mutate`` aanlevert, niet op een eerdere kopie.

    Zou de taak een gelezen kopie opslaan en de store drieweg-mergen, dan landde een veld dat
    een gelijktijdige schrijver toevoegde op de VERVANGEN sleutel, en geen validatie ziet dat:
    ``age_pattern_violations`` toetst dat een waarde versleuteld is, niet voor welke recipient.
    """
    project_data = await _project_file(platform_pair, project_pair)
    new_private, new_public = generate_sops_key_pair()
    # Wat een andere schrijver toevoegde terwijl de taak liep.
    project_data["components"].append(
        {"name": "component-2", "user-env-vars": await encrypt_age_content("LATER=1", project_pair[1])}
    )
    store = _rotatie_omgeving(monkeypatch, platform_pair, project_data)

    with (
        patch.object(handlers, "get_project_store", return_value=store),
        patch.object(handlers, "generate_sops_key_pair", return_value=(new_private, new_public)),
        patch.object(handlers, "handle_refresh_project", AsyncMock(return_value={"status": "success"})),
    ):
        result = await handlers.handle_rotate_project_key({"project_name": "demo"}, MagicMock())

    assert "components[1].user-env-vars" in result["rotated_fields"]
    bijgekomen = store.mutated["components"][1]["user-env-vars"]
    assert await decrypt_field(bijgekomen, new_private) == "LATER=1"
    assert not await opens_with(bijgekomen, project_pair[0])


@pytest.mark.asyncio
async def test_een_ongeldige_projectnaam_breekt_meteen_af_en_raakt_de_store_niet() -> None:
    progress = MagicMock()

    with (
        patch.object(handlers, "get_project_store") as get_store,
        pytest.raises(ValueError, match="Invalid project name format"),
    ):
        await handlers.handle_rotate_project_key({"project_name": "Niet-Geldig!"}, progress)

    get_store.assert_not_called()
    progress.fail_project.assert_called_once()


@pytest.mark.asyncio
async def test_een_onbekend_project_breekt_af_en_schrijft_niets() -> None:
    store = MagicMock()
    store.reconcile = AsyncMock()
    store.get = MagicMock(return_value=None)
    store.mutate = AsyncMock()
    progress = MagicMock()

    with (
        patch.object(handlers, "get_project_store", return_value=store),
        pytest.raises(ValueError, match="not found in project registry"),
    ):
        await handlers.handle_rotate_project_key({"project_name": "demo"}, progress)

    store.mutate.assert_not_awaited()
    progress.fail_project.assert_called_once()


@pytest.mark.asyncio
async def test_een_project_dat_niet_meer_in_git_staat_verwerkt_niets_meer(
    platform_pair: tuple[str, str], project_pair: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Het registry kent het project nog, git niet meer: dan faalt de schrijfstap zelf."""
    project_data = await _project_file(platform_pair, project_pair)
    store = _rotatie_omgeving(monkeypatch, platform_pair, project_data)
    store.mutate = AsyncMock(side_effect=ProjectNotFoundError("Project 'demo' does not exist"))
    refresh = AsyncMock()
    progress = MagicMock()

    with (
        patch.object(handlers, "get_project_store", return_value=store),
        patch.object(handlers, "handle_refresh_project", refresh),
        pytest.raises(ProjectNotFoundError),
    ):
        await handlers.handle_rotate_project_key({"project_name": "demo"}, progress)

    refresh.assert_not_awaited()
    progress.fail_task.assert_called_once()
    progress.fail_project.assert_called_once()


@pytest.mark.asyncio
async def test_een_mislukte_omzetting_slaat_niets_op(
    platform_pair: tuple[str, str], project_pair: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plan, punt 5: faalt de omzetting, dan schrijft de taak het projectbestand niet."""
    project_data = await _project_file(platform_pair, project_pair)
    store = _rotatie_omgeving(monkeypatch, platform_pair, project_data)
    progress = MagicMock()

    with (
        patch.object(handlers, "get_project_store", return_value=store),
        patch.object(handlers, "rotate_project_key", AsyncMock(side_effect=ConversionFailed("gaat niet open"))),
        pytest.raises(ConversionFailed),
    ):
        await handlers.handle_rotate_project_key({"project_name": "demo"}, progress)

    # De omzetting zit IN de wijzigingsfunctie, dus een mislukte omzetting laat de store niets
    # committen: er is geen boodschap en geen resultaat.
    assert store.mutated is None
    progress.fail_task.assert_called_once()
    progress.fail_project.assert_called_once()


@pytest.mark.asyncio
async def test_een_mislukte_opslag_verwerkt_het_project_niet_meer(
    platform_pair: tuple[str, str], project_pair: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plan, punt 5: faalt het opslaan, dan start de herverwerking niet."""
    project_data = await _project_file(platform_pair, project_pair)
    new_private, new_public = generate_sops_key_pair()
    store = _rotatie_omgeving(monkeypatch, platform_pair, project_data)
    store.mutate = AsyncMock(side_effect=ProjectStoreError("git-push geweigerd"))
    refresh = AsyncMock()
    progress = MagicMock()

    with (
        patch.object(handlers, "get_project_store", return_value=store),
        patch.object(handlers, "generate_sops_key_pair", return_value=(new_private, new_public)),
        patch.object(handlers, "handle_refresh_project", refresh),
        pytest.raises(ProjectStoreError),
    ):
        await handlers.handle_rotate_project_key({"project_name": "demo"}, progress)

    refresh.assert_not_awaited()
    progress.fail_task.assert_called_once()
    progress.fail_project.assert_called_once()


@pytest.mark.asyncio
async def test_een_mislukte_herverwerking_geeft_status_failed_en_de_foutvelden_door(
    platform_pair: tuple[str, str], project_pair: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """De sleutel staat dan al vast (git-historie = terugzetpad), dus het nieuwe paar en de
    omgezette velden komen terug, ook als het herverwerken erna faalt."""
    project_data = await _project_file(platform_pair, project_pair)
    new_private, new_public = generate_sops_key_pair()
    store = _rotatie_omgeving(monkeypatch, platform_pair, project_data)
    refresh = AsyncMock(
        return_value={
            "status": "failed",
            "message": "Argo wil niet syncen",
            "error": "sync timeout",
            "error_type": "processing_failed",
            "error_category": "CrashLoop",
            "processing": {"status": "failed"},
        }
    )

    with (
        patch.object(handlers, "get_project_store", return_value=store),
        patch.object(handlers, "generate_sops_key_pair", return_value=(new_private, new_public)),
        patch.object(handlers, "handle_refresh_project", refresh),
    ):
        result = await handlers.handle_rotate_project_key({"project_name": "demo"}, MagicMock())

    assert result["status"] == "failed"
    assert result["message"] == "Argo wil niet syncen"
    assert result["error"] == "sync timeout"
    assert result["error_type"] == "processing_failed"
    assert result["error_category"] == "CrashLoop"
    assert result["public_key"] == new_public
    assert result["rotated_fields"] == ["components[0].user-env-vars", "config.api-key"]
    # De sleutel is al opgeslagen voordat de herverwerking faalde.
    store.mutate.assert_awaited_once()


@pytest.mark.asyncio
async def test_het_backupwachtwoord_verhuist_mee_naar_de_nieuwe_sleutel(
    platform_pair: tuple[str, str], project_pair: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Zonder deze stap opent de Kopia-repository na de rotatie niet meer en wist de pod de prefix."""
    cluster = opi.core.config.settings.CLUSTER_MANAGER
    project_data = await _project_file_met_deployment(platform_pair, project_pair, cluster)
    new_private, new_public = generate_sops_key_pair()
    store = _rotatie_omgeving(monkeypatch, platform_pair, project_data)

    namespace = get_prefixed_namespace(cluster, "demo")
    oud_wachtwoord = derive_backup_password(namespace, "de oude sleutel in de namespace")
    change_password = AsyncMock(return_value=True)
    kopia = MagicMock(change_repository_password=change_password)

    with (
        patch.object(handlers, "get_project_store", return_value=store),
        patch.object(handlers, "generate_sops_key_pair", return_value=(new_private, new_public)),
        patch.object(handlers, "handle_refresh_project", AsyncMock(return_value={"status": "success"})),
        patch.object(BaseBackupManager, "_derive_backup_key", AsyncMock(return_value=oud_wachtwoord)),
        patch("opi.connectors.kopia.create_kopia_connector", return_value=kopia),
    ):
        result = await handlers.handle_rotate_project_key({"project_name": "demo"}, MagicMock())

    assert result["backup_namespaces"] == [namespace]
    config, nieuw_wachtwoord = change_password.await_args.args
    assert config.password == oud_wachtwoord
    assert config.s3_prefix == f"{cluster}/{namespace}"
    # Het nieuwe wachtwoord volgt uit wat er straks als sops-age-key in de namespace komt.
    assert nieuw_wachtwoord == derive_backup_password(namespace, format_age_key_file(new_private, new_public))


@pytest.mark.asyncio
async def test_de_wachtwoordwissel_komt_na_de_opslag_en_voor_het_verwerken(
    platform_pair: tuple[str, str], project_pair: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Die volgorde is wat een mislukte wissel repareerbaar houdt.

    Vóór de opslag hangt het nieuwe wachtwoord aan een sleutel die alleen in het geheugen
    bestaat: faalt de opslag dan, dan opent de repository met geen enkel wachtwoord meer. Na de
    opslag zijn beide wachtwoorden afleidbaar (het oude uit de git-historie, het nieuwe uit het
    opgeslagen bestand). En vóór het verwerken, want dat zet de nieuwe sleutel als
    ``sops-age-key`` in de namespace, waar het oude wachtwoord uit wordt afgeleid.
    """
    cluster = opi.core.config.settings.CLUSTER_MANAGER
    project_data = await _project_file_met_deployment(platform_pair, project_pair, cluster)
    new_private, new_public = generate_sops_key_pair()
    store = _rotatie_omgeving(monkeypatch, platform_pair, project_data)

    volgorde: list[str] = []
    oorspronkelijke_mutate = store.mutate.side_effect

    async def mutate(*args: Any, **kwargs: Any) -> MagicMock:
        volgorde.append("opslaan")
        return await oorspronkelijke_mutate(*args, **kwargs)

    async def change_password(*args: Any, **kwargs: Any) -> bool:
        volgorde.append("wachtwoordwissel")
        return True

    async def refresh(*args: Any, **kwargs: Any) -> dict[str, Any]:
        volgorde.append("verwerken")
        return {"status": "success"}

    store.mutate = AsyncMock(side_effect=mutate)
    kopia = MagicMock(change_repository_password=AsyncMock(side_effect=change_password))

    with (
        patch.object(handlers, "get_project_store", return_value=store),
        patch.object(handlers, "generate_sops_key_pair", return_value=(new_private, new_public)),
        patch.object(handlers, "handle_refresh_project", AsyncMock(side_effect=refresh)),
        patch.object(BaseBackupManager, "_derive_backup_key", AsyncMock(return_value="oud")),
        patch("opi.connectors.kopia.create_kopia_connector", return_value=kopia),
    ):
        await handlers.handle_rotate_project_key({"project_name": "demo"}, MagicMock())

    assert volgorde == ["opslaan", "wachtwoordwissel", "verwerken"]


@pytest.mark.asyncio
async def test_een_mislukte_wachtwoordverhuizing_verwerkt_het_project_niet_meer(
    platform_pair: tuple[str, str], project_pair: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """De sleutel staat dan al in git, dus de wissel is met de hand te herhalen; verwerken niet.

    Verwerken zou het namespace-Secret op de nieuwe sleutel zetten, en daarmee het oude
    wachtwoord onafleidbaar maken.
    """
    cluster = opi.core.config.settings.CLUSTER_MANAGER
    project_data = await _project_file_met_deployment(platform_pair, project_pair, cluster)
    new_private, new_public = generate_sops_key_pair()
    store = _rotatie_omgeving(monkeypatch, platform_pair, project_data)

    refresh = AsyncMock(return_value={"status": "success"})
    kopia = MagicMock(change_repository_password=AsyncMock(side_effect=KopiaExecutionError("geen toegang")))
    progress = MagicMock()

    with (
        patch.object(handlers, "get_project_store", return_value=store),
        patch.object(handlers, "generate_sops_key_pair", return_value=(new_private, new_public)),
        patch.object(handlers, "handle_refresh_project", refresh),
        patch.object(BaseBackupManager, "_derive_backup_key", AsyncMock(return_value="oud")),
        patch("opi.connectors.kopia.create_kopia_connector", return_value=kopia),
        pytest.raises(KopiaExecutionError),
    ):
        await handlers.handle_rotate_project_key({"project_name": "demo"}, progress)

    store.mutate.assert_awaited_once()
    refresh.assert_not_awaited()
    assert progress.fail_task.call_count == 1


# De tests hierboven mocken create_kopia_connector weg; hieronder de echte
# change_repository_password, met alleen _run_kopia_command gemockt.


def _kopia_config() -> KopiaRepositoryConfig:
    return KopiaRepositoryConfig(
        s3_endpoint="https://minio.local",
        s3_bucket="backups",
        s3_access_key="access",
        s3_secret_key="secret",
        s3_prefix="odcn-production/demo",
        password="het-oude-wachtwoord",
    )


def _kopia_command_outcomes(outcomes: dict[str, tuple[str, int]]) -> AsyncMock:
    """Een ``_run_kopia_command``-mock die antwoordt op basis van het subcommando.

    ``outcomes`` is ``{"connect": (stderr, code), "change-password": (...), "disconnect": (...)}``;
    een subcommando dat niet in de mapping staat slaagt stilletjes.
    """

    async def respond(args: list[str], **_kwargs: object) -> tuple[str, str, int]:
        subcommand = args[1] if len(args) > 1 else args[0]
        stderr, code = outcomes.get(subcommand, ("", 0))
        return "", stderr, code

    return AsyncMock(side_effect=respond)


@pytest.mark.parametrize(
    "stderr",
    ["repository not found", "NO REPOSITORY at this prefix", "Repository Not Initialized yet"],
)
def test_elke_vorm_van_ontbrekende_repository_wordt_herkend(stderr: str) -> None:
    assert _is_missing_repository(stderr)


def test_een_echte_verbindingsfout_is_geen_ontbrekende_repository() -> None:
    assert not _is_missing_repository("connection refused")


@pytest.mark.asyncio
async def test_een_ontbrekende_repository_geeft_false_en_verandert_niets() -> None:
    """Een project dat nog nooit een backup maakte heeft geen repository: niets om over te zetten."""
    run_command = _kopia_command_outcomes({"connect": ("repository not found", 1)})

    with patch.object(KopiaConnector, "_run_kopia_command", run_command):
        result = await KopiaConnector().change_repository_password(_kopia_config(), "nieuw-wachtwoord")

    assert result is False
    assert run_command.await_count == 1


@pytest.mark.asyncio
async def test_een_echte_verbindingsfout_breekt_af_in_plaats_van_false() -> None:
    """Zonder dit onderscheid verdwijnt een echt kapotte repository stil als 'geen backup'."""
    run_command = _kopia_command_outcomes({"connect": ("connection refused", 1)})

    with (
        patch.object(KopiaConnector, "_run_kopia_command", run_command),
        pytest.raises(KopiaExecutionError, match="connection refused"),
    ):
        await KopiaConnector().change_repository_password(_kopia_config(), "nieuw-wachtwoord")

    assert run_command.await_count == 1


@pytest.mark.asyncio
async def test_het_wachtwoord_wordt_gezet_en_de_verbinding_daarna_losgemaakt() -> None:
    run_command = _kopia_command_outcomes({})

    with patch.object(KopiaConnector, "_run_kopia_command", run_command):
        result = await KopiaConnector().change_repository_password(_kopia_config(), "nieuw-wachtwoord")

    assert result is True
    subcommands = [call.args[0][1] for call in run_command.await_args_list]
    assert subcommands == ["connect", "change-password", "disconnect"]


@pytest.mark.asyncio
async def test_een_mislukte_wachtwoordwissel_breekt_af_maar_maakt_eerst_los() -> None:
    """Blijft de verbinding open, dan houdt die de volgende pod tegen die dezelfde prefix opent."""
    run_command = _kopia_command_outcomes({"change-password": ("disk full", 1)})

    with (
        patch.object(KopiaConnector, "_run_kopia_command", run_command),
        pytest.raises(KopiaExecutionError, match="disk full"),
    ):
        await KopiaConnector().change_repository_password(_kopia_config(), "nieuw-wachtwoord")

    subcommands = [call.args[0][1] for call in run_command.await_args_list]
    assert subcommands == ["connect", "change-password", "disconnect"]
