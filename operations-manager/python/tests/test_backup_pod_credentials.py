"""Back-up- en restorepods krijgen hun sleutels uit een Secret, niet uit hun pod-spec.

Een pod-spec is leesbaar voor iedereen die pods mag zien, en een argument staat in
/proc/<pid>/cmdline. Deze toetsen houden drie dingen vast (RC-271):

- geen sleutel of wachtwoord als platte ``value:`` in een van de zeven sjablonen;
- ``kopia`` krijgt zijn geheimen uit de omgeving, nooit via argv;
- een back-uppod leegt nooit zelf de prefix waarin hij schrijft.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from opi.core.backup_constants import (
    BACKUP_REPOSITORY_UNUSABLE_ERROR,
    BACKUP_REPOSITORY_UNUSABLE_EXIT_CODE,
    RESTORE_TARGET_UNUSABLE_EXIT_CODE,
)
from opi.generation.manifests import MANIFEST_FILTERS
from opi.manager.backup.base import mc_endpoint_is_usable, mc_host_url, pod_credentials_secret_name
from opi.manager.backup.bucket_backup import BucketBackupManager
from opi.manager.backup.pvc_backup import PVCBackupManager
from tests.test_backup_pod_service_account import POD_CREATORS, PROJECT, _manager

_MANIFESTS_DIR = Path(__file__).parent.parent / "manifests"

# De zeven pods van dit onderwerp. Elke andere pod in manifests/ draagt geen sleutels.
BACKUP_POD_TEMPLATES = (
    "backup-pod.yaml.jinja",
    "backup-database-pod.yaml.jinja",
    "backup-bucket-pod.yaml.jinja",
    "backup-bucket-mirror-pod.yaml.jinja",
    "restore-pod.yaml.jinja",
    "restore-database-pod.yaml.jinja",
    "restore-bucket-pod.yaml.jinja",
)

BACKUP_TEMPLATES_THAT_WRITE = (
    "backup-pod.yaml.jinja",
    "backup-database-pod.yaml.jinja",
    "backup-bucket-pod.yaml.jinja",
)

# Een env-naam van deze vorm draagt een sleutel of wachtwoord en mag nooit een `value:`
# hebben. ``MC_HOST_*`` draagt de sleutels in een URL, ``AWS_ACCESS_KEY_ID`` is de naam
# waaronder Kopia de S3-sleutel uit de omgeving leest.
_CREDENTIAL_ENV_NAME = re.compile(r"(_KEY|_PASSWORD|_SECRET)$|^MC_HOST_|^AWS_ACCESS_KEY_ID$")

# Elke waarde hieronder is een herkenbaar baken: komt er een in de gerenderde pod terecht,
# dan staat hij in de pod-spec in plaats van in het Secret. De sjablonen horen deze
# variabelen niet meer te gebruiken; ze worden meegegeven om precies dat te toetsen.
# Afgeleid van de naam, niet uitgeschreven: een letterlijk paar "..._secret_key": "<waarde>"
# leest `secret-scan` als een echte sleutel.
_SENTINELS = {
    name: f"SENTINEL-{name.replace('_', '-')}"
    for name in (
        "s3_access_key",
        "s3_secret_key",
        "kopia_password",
        "db_password",
        "target_db_password",
        "source_access_key",
        "source_secret_key",
        "target_access_key",
        "target_secret_key",
        "backup_access_key",
        "backup_secret_key",
    )
}

_RENDER_CTX: dict[str, Any] = {
    **_SENTINELS,
    "backup_image": "ghcr.io/minbzk/base-images/rig-backup@sha256:abc",
    "pod_name": "test-pod",
    "namespace": "rig-test",
    "service_account_name": "wies-sa",
    "credentials_secret": "test-pod-credentials",
    "repository_unusable_exit_code": BACKUP_REPOSITORY_UNUSABLE_EXIT_CODE,
    "target_unusable_exit_code": RESTORE_TARGET_UNUSABLE_EXIT_CODE,
    "timestamp": "20260520-073000",
    "backup_run_id": "20260520073000",
    "timeout_seconds": 3600,
    "cluster": "local",
    "project_name": "wies",
    "deployment_name": "production",
    "component_name": "frontend",
    "reference_name": "frontend-store",
    "source_type": "namespace",
    "generation": 1,
    "s3_endpoint": "minio.example:9000",
    "s3_bucket": "backups",
    "s3_disable_tls": True,
    "backup_prefix": "local/rig-test",
    "kopia_hostname": "wies-production-pvc-data",
    "kopia_source": "opi-backup@wies-production-pvc-data",
    "retention_keep_latest": 30,
    "retention_keep_daily": 30,
    "retention_keep_weekly": 4,
    "retention_keep_monthly": 12,
    "pvc_name": "wies-prod-frontend-data-1",
    "clone_pvc_name": "wies-prod-frontend-data-1-clone",
    "target_pvc_name": "wies-prod-frontend-data-2",
    "storage_name": "data",
    "pvc_generation": 1,
    "snapshot_id": "",
    "db_host": "postgres.example",
    "db_port": 5432,
    "db_name": "appdb",
    "db_user": "appuser",
    "target_db_host": "postgres.example",
    "target_db_port": 5432,
    "target_db_name": "appdb_v2",
    "target_db_user": "appuser",
    "source_schema": "appdb",
    "source_minio_endpoint": "http://minio.rig-test.svc:9000",
    "source_bucket_name": "user-uploads",
    "target_minio_endpoint": "http://minio.rig-test.svc:9000",
    "target_bucket_name": "user-uploads-v2",
    "backup_minio_endpoint": "http://minio.example:9000",
    "backup_bucket": "backups",
    "backup_path": "backups/wies/production/store/gen-1/20260520-073000",
    "clear_target": False,
}


def _render(template: str) -> dict[str, Any]:
    env = Environment(loader=FileSystemLoader(str(_MANIFESTS_DIR)), undefined=StrictUndefined, autoescape=False)
    env.filters.update(MANIFEST_FILTERS)
    return yaml.safe_load(env.get_template(template).render(**_RENDER_CTX))


def _script(pod: dict[str, Any]) -> str:
    return pod["spec"]["containers"][0]["command"][-1]


def _commands(pod: dict[str, Any]) -> str:
    """Het script zonder commentaarregels: die noemen de verboden vormen om uit te leggen."""
    return "\n".join(line for line in _script(pod).splitlines() if not line.lstrip().startswith("#"))


def _env_entries(pod: dict[str, Any]) -> list[dict[str, Any]]:
    return pod["spec"]["containers"][0]["env"]


def _credential_env_names(pod: dict[str, Any]) -> set[str]:
    return {entry["name"] for entry in _env_entries(pod) if _CREDENTIAL_ENV_NAME.search(entry["name"])}


class TestDeSjablonenDragenGeenGeheimen:
    @pytest.mark.parametrize("template", BACKUP_POD_TEMPLATES)
    def test_geen_bakenwaarde_in_de_gerenderde_pod(self, template: str) -> None:
        """Geen van de sleutels die de manager kent, komt in de pod-spec terecht."""
        rendered = yaml.safe_dump(_render(template))

        for variable, sentinel in _SENTINELS.items():
            assert sentinel not in rendered, f"{template} rendert {variable} in de pod-spec"

    @pytest.mark.parametrize("template", BACKUP_POD_TEMPLATES)
    def test_elke_sleutel_komt_uit_het_credentials_secret(self, template: str) -> None:
        pod = _render(template)
        names = _credential_env_names(pod)
        assert names, f"{template} heeft geen enkele sleutel, dat kan niet kloppen"

        for entry in _env_entries(pod):
            if entry["name"] not in names:
                continue
            assert "value" not in entry, f"{template}: {entry['name']} staat als platte waarde in de pod"
            ref = entry["valueFrom"]["secretKeyRef"]
            assert ref["name"] == _RENDER_CTX["credentials_secret"]
            assert ref["key"] == entry["name"]


class TestKopiaKrijgtZijnGeheimenUitDeOmgeving:
    @pytest.mark.parametrize("template", BACKUP_POD_TEMPLATES)
    def test_geen_sleutel_of_wachtwoord_op_de_commandoregel(self, template: str) -> None:
        commands = _commands(_render(template))

        assert "--password=" not in commands
        assert "--access-key" not in commands
        assert "--secret-access-key" not in commands
        assert "mc alias set" not in commands

    @pytest.mark.parametrize("template", BACKUP_POD_TEMPLATES)
    def test_kopia_leest_de_namen_die_hij_zelf_kent(self, template: str) -> None:
        """Kopia 0.23.1 leest KOPIA_PASSWORD, AWS_ACCESS_KEY_ID en AWS_SECRET_ACCESS_KEY."""
        pod = _render(template)
        if "kopia" not in _script(pod):
            return

        assert {"KOPIA_PASSWORD", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"} <= _credential_env_names(pod)


class TestEenBackuppodRuimtNietsOp:
    @pytest.mark.parametrize("template", BACKUP_TEMPLATES_THAT_WRITE)
    def test_de_prefix_wordt_nooit_geleegd(self, template: str) -> None:
        commands = _commands(_render(template))

        assert "mc rm" not in commands
        assert "reinitializing" not in commands

    @pytest.mark.parametrize("template", BACKUP_TEMPLATES_THAT_WRITE)
    def test_een_onbruikbare_repository_stopt_met_de_eigen_exitcode(self, template: str) -> None:
        script = _script(_render(template))

        assert f"exit {BACKUP_REPOSITORY_UNUSABLE_EXIT_CODE}" in script
        assert "exists but cannot be opened" in script


class TestDeManagerZetHetSecretVoorDePod:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", POD_CREATORS)
    async def test_het_secret_gaat_er_eerst_in_en_draagt_precies_de_sleutels_van_de_pod(self, kind: str) -> None:
        cls, create = POD_CREATORS[kind]
        manager = _manager(cls)

        await create(manager, PROJECT)

        applied = [
            yaml.safe_load(call.kwargs["stdin_input"])
            for call in manager.kubectl.run_command.await_args_list
            if call.kwargs.get("stdin_input")
        ]
        kinds = [doc["kind"] for doc in applied]
        assert kinds == ["Secret", "Pod"], kinds

        secret, pod = applied
        assert secret["metadata"]["name"] == pod_credentials_secret_name(pod["metadata"]["name"])
        assert secret["metadata"]["namespace"] == pod["metadata"]["namespace"]
        assert set(secret["stringData"]) == _credential_env_names(pod)
        assert all(secret["stringData"].values()), secret["stringData"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", POD_CREATORS)
    async def test_de_pod_wordt_eigenaar_van_zijn_secret(self, kind: str) -> None:
        """Herstart OPI voor de opruiming, dan neemt de garbage collector het Secret mee."""
        cls, create = POD_CREATORS[kind]
        manager = _manager(cls)
        manager.kubectl.run_command = AsyncMock(return_value=("pod-uid-1", "", 0))

        await create(manager, PROJECT)

        patches = [call.args[0] for call in manager.kubectl.run_command.await_args_list if call.args[0][0] == "patch"]
        assert len(patches) == 1, patches
        assert patches[0][1:3] == ["secret", pod_credentials_secret_name("pod")]
        assert "pod-uid-1" in patches[0][-1]

    @pytest.mark.asyncio
    async def test_de_opruiming_haalt_de_pod_en_zijn_secret_weg(self) -> None:
        manager = _manager(PVCBackupManager)

        await manager._cleanup_pod("rig-amt", "pod")

        deleted = manager.kubectl.run_command.await_args.args[0]
        assert deleted[:3] == ["delete", "pod/pod", "secret/pod-credentials"]
        assert "--ignore-not-found=true" in deleted


class TestGedeeltelijkFalenLaatNietsHalfOpgezetAchter:
    """Mislukt een stap van ``_apply_pod_with_credentials``, dan slaat de volgende over."""

    @pytest.mark.asyncio
    async def test_een_mislukte_secret_voorkomt_de_pod_apply(self) -> None:
        manager = _manager(PVCBackupManager)
        manager.kubectl.run_command = AsyncMock(return_value=("", "secret weigert", 1))

        with pytest.raises(RuntimeError, match="credentials secret"):
            await manager._apply_pod_with_credentials(
                namespace="rig-amt",
                pod_name="pod",
                manifest="kind: Pod",
                credentials={"KOPIA_PASSWORD": "kp"},
                what="backup",
            )

        manager.kubectl.run_command.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_een_mislukte_pod_apply_slaat_de_eigenaarzetting_over(self) -> None:
        manager = _manager(PVCBackupManager)
        manager.kubectl.run_command = AsyncMock(side_effect=[("", "", 0), ("", "pod weigert", 1)])

        with pytest.raises(RuntimeError, match="backup pod"):
            await manager._apply_pod_with_credentials(
                namespace="rig-amt",
                pod_name="pod",
                manifest="kind: Pod",
                credentials={"KOPIA_PASSWORD": "kp"},
                what="backup",
            )

        assert manager.kubectl.run_command.await_count == 2

    @pytest.mark.asyncio
    async def test_zonder_leesbare_pod_uid_blijft_de_patch_uit(self) -> None:
        manager = _manager(PVCBackupManager)
        manager.kubectl.run_command = AsyncMock(return_value=("", "pod al weg", 1))

        await manager._own_credentials_secret("rig-amt", "pod", "pod-credentials")

        manager.kubectl.run_command.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_een_mislukte_patch_blijft_best_effort(self) -> None:
        manager = _manager(PVCBackupManager)
        manager.kubectl.run_command = AsyncMock(side_effect=[("pod-uid-1", "", 0), ("", "patch weigert", 1)])

        await manager._own_credentials_secret("rig-amt", "pod", "pod-credentials")

        assert manager.kubectl.run_command.await_count == 2


class TestDeExitcodeWordtEenTaakfout:
    @pytest.mark.asyncio
    async def test_de_onbruikbare_repository_wordt_benoemd_in_plaats_van_de_logstaart(self) -> None:
        manager = _manager(PVCBackupManager)
        manager._pod_exit_code = AsyncMock(return_value=BACKUP_REPOSITORY_UNUSABLE_EXIT_CODE)  # type: ignore[method-assign]

        error = await manager._backup_pod_failure_error("rig-amt", "pod", "logs die niemand hoeft te lezen")

        assert error == BACKUP_REPOSITORY_UNUSABLE_ERROR

    @pytest.mark.asyncio
    async def test_elke_andere_fout_houdt_zijn_logstaart(self) -> None:
        manager = _manager(PVCBackupManager)
        manager._pod_exit_code = AsyncMock(return_value=1)  # type: ignore[method-assign]

        error = await manager._backup_pod_failure_error("rig-amt", "pod", "pg_dump: connection refused")

        assert "pg_dump: connection refused" in error


class TestMcHostUrl:
    def test_de_sleutels_worden_gecodeerd(self) -> None:
        url = mc_host_url("http://minio:9000", "ak/with slash", "sk+with:colon@at")

        assert url == "http://ak%2Fwith%20slash:sk%2Bwith%3Acolon%40at@minio:9000"

    def test_een_endpoint_zonder_schema_wordt_geweigerd(self) -> None:
        with pytest.raises(ValueError, match="scheme"):
            mc_host_url("minio:9000", "ak", "sk")

    @pytest.mark.parametrize(
        ("endpoint", "usable"),
        [
            ("http://minio:9000", True),
            ("https://minio.example", True),
            ("minio:9000", False),
            ("http://", False),
            ("", False),
        ],
    )
    def test_de_vorm_van_een_endpoint(self, endpoint: str, usable: bool) -> None:
        assert mc_endpoint_is_usable(endpoint) is usable

    @pytest.mark.asyncio
    async def test_een_doel_zonder_schema_is_invalidtarget_en_geen_500(self) -> None:
        """Het endpoint komt van de aanroeper, dus de fout hoort bij zijn invoer (RC-82)."""
        manager = _manager(BucketBackupManager)
        manager._derive_backup_key = AsyncMock(return_value="kp")  # type: ignore[method-assign]

        result = await manager._restore_bucket(
            cluster="sandboxed-local",
            namespace="rig-amt",
            reference_name="bucket",
            target_minio_endpoint="minio:9000",
            target_bucket_name="amt-prod",
            target_access_key="ak",
            target_secret_key="sk",
            project_name=PROJECT,
        )

        assert result.success is False
        assert result.target_unusable is True
        manager.kubectl.run_command.assert_not_awaited()


class TestGeenEnkelePodDraagtEenPlatteSleutel:
    """Een veegtoets over elk pod-sjabloon, zodat een nieuwe pod hier ook onder valt."""

    # list-snapshots-pod.yaml.jinja zet KOPIA_PASSWORD nog als platte waarde. Het sjabloon
    # wordt door niets aangeroepen (dode code uit een eerdere ronde, gemeld op de PR van
    # RC-271) en heeft dus geen manager die een Secret voor hem kan maken.
    EXEMPT = frozenset({"list-snapshots-pod.yaml.jinja"})

    def _plain_credential_envs(self, source: str) -> list[str]:
        lines = source.splitlines()
        found = []
        for index, line in enumerate(lines[:-1]):
            match = re.match(r"\s*- name: ([A-Za-z_][A-Za-z0-9_]*)\s*$", line)
            if match and _CREDENTIAL_ENV_NAME.search(match.group(1)) and lines[index + 1].lstrip().startswith("value:"):
                found.append(match.group(1))
        return found

    def test_elk_pod_sjabloon_haalt_zijn_sleutels_uit_een_secret(self) -> None:
        templates = sorted(path.name for path in _MANIFESTS_DIR.glob("*-pod.yaml.jinja"))
        assert set(BACKUP_POD_TEMPLATES) <= set(templates)

        offenders = {
            name: self._plain_credential_envs((_MANIFESTS_DIR / name).read_text())
            for name in templates
            if name not in self.EXEMPT
        }
        assert {name: envs for name, envs in offenders.items() if envs} == {}

    def test_de_veegtoets_vindt_een_platte_sleutel_wel(self) -> None:
        """Zonder deze toets zou een stille regexfout de veegtoets altijd groen maken."""
        assert self._plain_credential_envs('        - name: S3_SECRET_KEY\n          value: "oeps"\n') == [
            "S3_SECRET_KEY"
        ]
