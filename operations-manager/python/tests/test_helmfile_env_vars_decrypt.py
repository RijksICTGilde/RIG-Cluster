"""Helmfile env-vars worden ontsleuteld met de projectsleutel, niet de systeemsleutel.

Gemelde fout: een helmfile-deployment met een AGE-versleutelde waarde in
``env-vars`` strandde op "Age decryption failed". De schrijfkant versleutelt met
de publieke sleutel van het PROJECT (``config.age-public-key``), precies zoals
``user-env-vars`` en ``helm-values``, maar ``_process_helmfile_deployment``
ontsleutelde met de systeemsleutel (``settings.SOPS_AGE_PRIVATE_KEY``). Die twee
sleutelparen zijn van elkaar verschillen, dus elke echt versleutelde env-var was
onleesbaar -- de gebruiker zette de waarde toen maar plat neer.

De fix haalt de sleutel via ``_sops_private_key_for`` (dezelfde helper als de
andere project-paden) en doet dat lui: pas als er een versleutelde waarde staat
wordt er überhaupt een sleutel opgehaald. Drie regels worden hier vastgezet:

1. **Versleutelde waarden openen met de projectsleutel.** De sleutel die aan
   ``decrypt_age_content`` wordt doorgegeven is die van
   ``_sops_private_key_for`` -- en die wordt maar één keer opgehaald, ook bij
   meerdere versleutelde waarden.
2. **Alleen platte tekst raakt het sleutelpad nooit.** Een project zonder
   versleutelde env-vars moet blijven werken, ook zonder geconfigureerde sleutel.
3. **Versleuteld zonder sleutel zegt wat er mist.** Niet de generieke
   "Missing encrypted content or private key" van ``decrypt_age_content``, maar
   welke deployment en welke variabele.

Git, SOPS-encryptie en decryptie zelf zijn gemockt; ``decrypt_age_content``
wordt alleen gepatcht waar de test iets over de sleutel beweert.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

if TYPE_CHECKING:
    from pathlib import Path

AGE_BLOCK_1 = "-----BEGIN AGE ENCRYPTED FILE-----\nZW5l\n-----END AGE ENCRYPTED FILE-----\n"
AGE_BLOCK_2 = "-----BEGIN AGE ENCRYPTED FILE-----\ndHdlZQ==\n-----END AGE ENCRYPTED FILE-----\n"
PROJECT_KEY = "AGE-SECRET-KEY-PROJECT"


def _manager():
    with (
        patch("opi.manager.project_manager.KubectlConnector"),
        patch("opi.handlers.sops.SopsHandler"),
        patch("opi.generation.manifests.ManifestGenerator"),
        patch("opi.manager.argo_manager.ArgoManager", return_value=MagicMock()),
        patch("opi.manager.bootstrap_manager.BootstrapManager", return_value=MagicMock()),
        patch("opi.manager.delete_project_manager.DeleteProjectManager", return_value=MagicMock()),
        patch("opi.manager.keycloak_manager.KeycloakManager", return_value=MagicMock()),
        patch("opi.manager.minio_manager.MinioManager", return_value=MagicMock()),
        patch("opi.manager.redis_manager.RedisManager", return_value=MagicMock()),
        patch("opi.manager.pvc_manager.PVCManager", return_value=MagicMock()),
    ):
        from opi.manager.project_manager import ProjectManager

        return ProjectManager()


def _deployment(env_vars: dict[str, str]) -> dict[str, Any]:
    return {
        "name": "production",
        "cluster": "sandboxed-local",
        "namespace": "docs",
        "helmfile": [{"reference": "docs", "env-vars": env_vars}],
    }


def _wire(manager: Any) -> None:
    """Alles rond het cmp-env-blok: git-clone, values-extractie, secrets, SOPS."""
    manager.get_contents = AsyncMock(return_value={"name": "docs", "config": {"age-public-key": "age1demo"}})
    manager._get_helm_values_context = AsyncMock(return_value={})
    manager._clone_helmfile_source = AsyncMock(return_value=("unused", None))
    manager._write_helmfile_custom_files = MagicMock(return_value={})
    manager._create_deployment_secrets = AsyncMock(return_value=[])
    manager._project_file_handler = MagicMock()
    manager._project_file_handler.get_helmfile_by_name = MagicMock(return_value={"name": "docs"})
    manager._project_file_handler.extract_helmfile_values = AsyncMock(return_value={})
    manager._project_file_handler.extract_deployment_helmfile_values = AsyncMock(return_value={})


async def _process(manager: Any, deployment: dict[str, Any], target: Path) -> None:
    with (
        patch("opi.manager.project_manager.get_prefixed_namespace", return_value="rig-docs"),
        patch("opi.manager.project_manager.encrypt_to_sops_files_or_fail"),
    ):
        await manager._process_helmfile_deployment(deployment, MagicMock(), str(target))


@pytest.mark.asyncio
async def test_versleutelde_env_vars_openen_met_de_projectsleutel(tmp_path: Path) -> None:
    decrypt = AsyncMock(side_effect=lambda content, key: f"open-met-{key}")
    manager = _manager()
    manager._sops_private_key_for = AsyncMock(return_value=PROJECT_KEY)
    _wire(manager)

    deployment = _deployment({"EERSTE": AGE_BLOCK_1, "TWEEDE": AGE_BLOCK_2, "PLAT": "ja"})
    with patch("opi.manager.project_manager.decrypt_age_content", decrypt):
        await _process(manager, deployment, tmp_path)

    inhoud = (tmp_path / ".cmp-env").read_text()
    assert f"EERSTE=open-met-{PROJECT_KEY}" in inhoud
    assert f"TWEEDE=open-met-{PROJECT_KEY}" in inhoud
    assert "PLAT=ja" in inhoud

    # Elke versleutelde waarde wordt ontsleuteld, met steeds de projectsleutel.
    assert decrypt.await_count == 2
    assert all(call.args[1] == PROJECT_KEY for call in decrypt.await_args_list)
    # Twee awaits op de helper: één lui voor de env-vars-lus (niet per waarde),
    # één door de SOPS-encryptiestap verderop in dezelfde methode.
    assert manager._sops_private_key_for.await_count == 2


@pytest.mark.asyncio
async def test_platte_env_vars_raken_het_sleutelpad_niet(tmp_path: Path) -> None:
    manager = _manager()
    manager._sops_private_key_for = AsyncMock(return_value=PROJECT_KEY)
    _wire(manager)

    with patch("opi.manager.project_manager.decrypt_age_content", AsyncMock()) as decrypt:
        await _process(manager, _deployment({"ALLEEN": "plat"}), tmp_path)

    assert (tmp_path / ".cmp-env").read_text() == "ALLEEN=plat\n"
    # Geen enkele platte waarde komt bij decrypt_age_content langs -- het bewijs
    # dat de sleutel in de env-vars-lus nooit nodig is. (De helper wordt wél nog
    # één keer aangeroepen: door de SOPS-encryptiestap verderop.)
    decrypt.assert_not_awaited()


@pytest.mark.asyncio
async def test_versleuteld_zonder_projectsleutel_zegt_wat_er_mist(tmp_path: Path) -> None:
    manager = _manager()
    manager._sops_private_key_for = AsyncMock(return_value=None)
    _wire(manager)

    with pytest.raises(ValueError, match=r"production.*'API_KEY'.*age-private-key"):
        await _process(manager, _deployment({"API_KEY": AGE_BLOCK_1}), tmp_path)

    assert not (tmp_path / ".cmp-env").exists()
