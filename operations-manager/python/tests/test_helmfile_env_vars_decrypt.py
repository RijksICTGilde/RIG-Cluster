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

De validatie van de vorm gebeurt hier na ontsleuteling, omdat alleen daar te zien is wat
de plugin uiteindelijk voor zich krijgt. Het formaat zelf en de parser staan in
``tests/test_cmp_env_contract.py``.

Sinds RC-333 gaan de env-vars niet meer als plat ``.cmp-env`` naar git maar als
``cmp-env.sops.yaml``, versleuteld op de projectsleutel. De tests die de versleuteling zelf
meten draaien op echte SOPS en skippen zonder die binary; de rest houdt de encryptie gemockt
en kijkt naar het platte ``cmp-env.to-sops.yaml`` dat eraan voorafgaat.
"""

from __future__ import annotations

import shutil
from contextlib import ExitStack
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.utils.sops import decrypt_sops_with_key, generate_sops_key_pair, sops_filenames
from ruamel.yaml import YAML

if TYPE_CHECKING:
    from pathlib import Path

needs_sops = pytest.mark.skipif(shutil.which("sops") is None, reason="requires the sops binary")

CMP_ENV_TO_SOPS = sops_filenames("cmp-env").plaintext
CMP_ENV_SOPS = sops_filenames("cmp-env").encrypted

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


def _wire(manager: Any, public_key: str = "age1demo") -> None:
    """Alles rond het cmp-env-blok: git-clone, values-extractie, secrets, SOPS."""
    manager.get_contents = AsyncMock(return_value={"name": "docs", "config": {"age-public-key": public_key}})
    manager._get_helm_values_context = AsyncMock(return_value={})
    manager._clone_helmfile_source = AsyncMock(return_value=("unused", None))
    manager._write_helmfile_custom_files = MagicMock(return_value={})
    manager._create_deployment_secrets = AsyncMock(return_value=[])
    manager._project_file_handler = MagicMock()
    manager._project_file_handler.get_helmfile_by_name = MagicMock(return_value={"name": "docs"})
    manager._project_file_handler.extract_helmfile_values = AsyncMock(return_value={})
    manager._project_file_handler.extract_deployment_helmfile_values = AsyncMock(return_value={})


async def _process(manager: Any, deployment: dict[str, Any], target: Path, *, echte_sops: bool = False) -> None:
    with ExitStack() as stack:
        stack.enter_context(patch("opi.manager.project_manager.get_prefixed_namespace", return_value="rig-docs"))
        if not echte_sops:
            stack.enter_context(patch("opi.manager.project_manager.encrypt_to_sops_files_or_fail"))
        await manager._process_helmfile_deployment(deployment, MagicMock(), str(target))


def _env_vars_op_schijf(target: Path) -> dict[str, str]:
    """De map zoals OPI hem platte tekst neerzet, voor de SOPS-stap."""
    return YAML(typ="safe").load((target / CMP_ENV_TO_SOPS).read_text())


@pytest.mark.asyncio
async def test_versleutelde_env_vars_openen_met_de_projectsleutel(tmp_path: Path) -> None:
    decrypt = AsyncMock(side_effect=lambda content, key: f"open-met-{key}")
    manager = _manager()
    manager._sops_private_key_for = AsyncMock(return_value=PROJECT_KEY)
    _wire(manager)

    deployment = _deployment({"EERSTE": AGE_BLOCK_1, "TWEEDE": AGE_BLOCK_2, "PLAT": "ja"})
    with patch("opi.manager.project_manager.decrypt_age_content", decrypt):
        await _process(manager, deployment, tmp_path)

    assert _env_vars_op_schijf(tmp_path) == {
        "EERSTE": f"open-met-{PROJECT_KEY}",
        "TWEEDE": f"open-met-{PROJECT_KEY}",
        "PLAT": "ja",
    }

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

    assert _env_vars_op_schijf(tmp_path) == {"ALLEEN": "plat"}
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

    assert not (tmp_path / CMP_ENV_TO_SOPS).exists()


@pytest.mark.asyncio
async def test_een_gereserveerde_sleutel_wordt_geweigerd(tmp_path: Path) -> None:
    """De plugin leest ``ARGOCD_APP_NAMESPACE`` zelf, dus een deployment mag hem niet zetten."""
    manager = _manager()
    manager._sops_private_key_for = AsyncMock(return_value=PROJECT_KEY)
    _wire(manager)

    with pytest.raises(ValueError, match=r"'ARGOCD_APP_NAMESPACE' is reserved"):
        await _process(manager, _deployment({"ARGOCD_APP_NAMESPACE": "van-de-tenant"}), tmp_path)

    assert not (tmp_path / CMP_ENV_TO_SOPS).exists()


@pytest.mark.asyncio
async def test_een_regeleinde_na_ontsleuteling_wordt_geweigerd(tmp_path: Path) -> None:
    """Een tweede regel uit een versleutelde waarde zou een onbewaakt paar zijn.

    Het schema ziet alleen het AGE-blok, dus dit is de enige plek waar de vorm van de
    platte waarde nog te toetsen is.
    """
    manager = _manager()
    manager._sops_private_key_for = AsyncMock(return_value=PROJECT_KEY)
    _wire(manager)

    decrypt = AsyncMock(return_value="eerste\nARGOCD_APP_NAMESPACE=kaper")
    with (
        patch("opi.manager.project_manager.decrypt_age_content", decrypt),
        pytest.raises(ValueError, match=r"value of 'MELDING' contains a newline"),
    ):
        await _process(manager, _deployment({"MELDING": AGE_BLOCK_1}), tmp_path)

    assert not (tmp_path / CMP_ENV_TO_SOPS).exists()


@pytest.mark.asyncio
async def test_een_lange_waarde_met_spaties_blijft_ongewijzigd(tmp_path: Path) -> None:
    """De YAML-emitter mag een lange waarde niet over regels vouwen.

    Vouwen trekt een reeks spaties samen, dus de waarde die de render bereikt zou
    stilletjes verschillen van wat de tenant opgaf. Het platte bestand van voor RC-333
    schreef de bytes onveranderd weg.
    """
    waarde = "x" * 70 + "  twee spaties  " + "y" * 40
    manager = _manager()
    manager._sops_private_key_for = AsyncMock(return_value=PROJECT_KEY)
    _wire(manager)

    await _process(manager, _deployment({"LANG": waarde}), tmp_path)

    assert _env_vars_op_schijf(tmp_path) == {"LANG": waarde}


@needs_sops
@pytest.mark.asyncio
async def test_de_env_vars_staan_versleuteld_in_de_deploymentmap(tmp_path: Path) -> None:
    """Wat naar zad-deployments gaat is cijfertekst op de projectsleutel, geen platte tekst."""
    private_key, public_key = generate_sops_key_pair()
    manager = _manager()
    manager._sops_private_key_for = AsyncMock(return_value=private_key)
    _wire(manager, public_key)

    await _process(manager, _deployment({"ENVIRONMENT": "productie", "API_KEY": "geheim"}), tmp_path, echte_sops=True)

    assert not (tmp_path / ".cmp-env").exists()
    assert not (tmp_path / CMP_ENV_TO_SOPS).exists()
    versleuteld = tmp_path / CMP_ENV_SOPS
    assert "geheim" not in versleuteld.read_text()

    ontsleuteld = decrypt_sops_with_key(str(versleuteld), private_key)
    assert ontsleuteld is not None
    assert YAML(typ="safe").load(ontsleuteld) == {"API_KEY": "geheim", "ENVIRONMENT": "productie"}


@pytest.mark.asyncio
async def test_een_bestaand_plat_cmp_env_wordt_opgeruimd(tmp_path: Path) -> None:
    """De deployments die nog een plat bestand hebben raken het kwijt bij de eerste ronde."""
    manager = _manager()
    manager._sops_private_key_for = AsyncMock(return_value=PROJECT_KEY)
    _wire(manager)
    (tmp_path / ".cmp-env").write_text("ENVIRONMENT=productie\n")

    await _process(manager, _deployment({"ENVIRONMENT": "productie"}), tmp_path)

    assert not (tmp_path / ".cmp-env").exists()
    assert _env_vars_op_schijf(tmp_path) == {"ENVIRONMENT": "productie"}


@pytest.mark.asyncio
async def test_zonder_env_vars_blijft_er_geen_bestand_achter(tmp_path: Path) -> None:
    """Een weggehaalde variabele moet ook uit de render verdwijnen."""
    manager = _manager()
    manager._sops_private_key_for = AsyncMock(return_value=PROJECT_KEY)
    _wire(manager)
    (tmp_path / CMP_ENV_SOPS).write_text("sops: van-een-vorige-ronde\n")
    (tmp_path / ".cmp-env").write_text("ENVIRONMENT=productie\n")

    await _process(manager, _deployment({}), tmp_path)

    assert not (tmp_path / CMP_ENV_SOPS).exists()
    assert not (tmp_path / CMP_ENV_TO_SOPS).exists()
    assert not (tmp_path / ".cmp-env").exists()


@needs_sops
@pytest.mark.asyncio
async def test_een_ongewijzigde_waarde_levert_geen_nieuwe_cijfertekst_op(tmp_path: Path) -> None:
    """SOPS versleutelt niet deterministisch, dus zonder skip-if-unchanged churnt elke ronde."""
    private_key, public_key = generate_sops_key_pair()
    manager = _manager()
    manager._sops_private_key_for = AsyncMock(return_value=private_key)
    _wire(manager, public_key)

    await _process(manager, _deployment({"ENVIRONMENT": "productie"}), tmp_path, echte_sops=True)
    eerste = (tmp_path / CMP_ENV_SOPS).read_bytes()

    await _process(manager, _deployment({"ENVIRONMENT": "productie"}), tmp_path, echte_sops=True)
    assert (tmp_path / CMP_ENV_SOPS).read_bytes() == eerste

    await _process(manager, _deployment({"ENVIRONMENT": "acceptatie"}), tmp_path, echte_sops=True)
    gewijzigd = tmp_path / CMP_ENV_SOPS
    assert gewijzigd.read_bytes() != eerste
    ontsleuteld = decrypt_sops_with_key(str(gewijzigd), private_key)
    assert ontsleuteld is not None
    assert YAML(typ="safe").load(ontsleuteld) == {"ENVIRONMENT": "acceptatie"}
