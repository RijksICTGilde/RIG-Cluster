"""SOPS draait in een thread, niet op de event loop.

WAAROM DIT EEN EIGEN TEST HEEFT

``encrypt_to_sops_files`` start per bestand het ``sops``-binary, synchroon, en
``_existing_secret_pairs`` doet dat nog eens voor de vorige ciphertext. Dat gebeurt bij
elke refresh, per deployment, in een blok zonder ``await``. Zolang dat loopt handelt OPI
geen enkel ander verzoek af en komt geen enkele andere taak aan de beurt: FastAPI, de
TaskWorker en alle schedulers delen een loop.

Niet getoetst wordt of ``asyncio.to_thread`` wordt aangeroepen, maar of een tweede taak
blijft tikken terwijl het trage werk loopt. Dat is de storing in de vorm waarin een
gebruiker hem merkt.
"""

from __future__ import annotations

import os
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.core.config import settings
from opi.generation.manifests import ManifestGenerator
from opi.manager.argo_manager import ArgoManager
from opi.manager.project_manager import ProjectManager
from opi.services.catalog.base import SecretFileSpec
from tests.loop_meeloper import meeloper

#: Zo lang doet een aanroep van het sops-binary erover in deze test. Ruim boven de grens,
#: kort genoeg om de suite niet op te houden. Een echte encrypt duurt tientallen ms, maal
#: het aantal secrets van een deployment.
_SOPSDUUR = 0.3
_GRENS = 0.1


def _traag_sops(*_args: Any, **_kwargs: Any) -> MagicMock:
    """Een ``subprocess.run`` die zich gedraagt als een trage sops-aanroep."""
    time.sleep(_SOPSDUUR)
    resultaat = MagicMock()
    resultaat.returncode = 0
    resultaat.stdout = "sops: versleuteld\n"
    resultaat.stderr = ""
    return resultaat


@pytest.mark.asyncio
async def test_het_versleutelen_van_repository_secrets_blokkeert_de_loop_niet(tmp_path) -> None:
    """``create_repository_secrets`` versleutelt drie repositories in een thread."""
    git_connector = AsyncMock()
    git_connector.get_working_dir = AsyncMock(return_value=str(tmp_path))

    project_manager = AsyncMock()
    project_manager._manifest_generator = ManifestGenerator()
    project_manager.get_name = AsyncMock(return_value="demo")
    project_manager.get_deployments = AsyncMock(return_value=[{"name": "productie"}])
    project_manager.get_git_connector_for_argocd = AsyncMock(return_value=git_connector)
    project_manager.get_repositories = AsyncMock(
        return_value=[
            {"name": naam, "url": f"https://forgejo.example.org/zad/{naam}.git", "username": "git", "password": "x"}
            for naam in ("een", "twee", "drie")
        ]
    )

    manager = ArgoManager(project_manager)

    with (
        patch("opi.utils.sops.subprocess.run", side_effect=_traag_sops),
        patch.object(settings, "SOPS_AGE_PUBLIC_KEY", "age1publiek"),
        patch.object(settings, "SOPS_AGE_PRIVATE_KEY", None),
    ):
        async with meeloper() as meting:
            await manager.create_repository_secrets({"name": "demo"})

    meting.eis_onder(_GRENS, "SOPS-versleuteling van de repository-secrets")


@pytest.mark.asyncio
async def test_de_vorige_ciphertext_lezen_blokkeert_de_loop_niet(tmp_path) -> None:
    """``_write_secret_file`` leest de vorige ciphertext via het sops-binary."""
    directory = str(tmp_path)
    with open(os.path.join(directory, "geheim-secret.sops.yaml"), "w") as f:
        f.write("ciphertext\n")

    pm = ProjectManager.__new__(ProjectManager)
    pm._manifest_generator = ManifestGenerator()

    sjabloon = os.path.join(os.path.dirname(__file__), "..", "manifests", "generic-secret.yaml.to-sops.jinja")

    with patch("opi.utils.sops.subprocess.run", side_effect=_traag_sops):
        async with meeloper() as meting:
            await pm._write_secret_file(
                SecretFileSpec(secret_name="geheim", secret_pairs={"a": "b"}, keep_existing_values=True),
                deployment_name="productie",
                namespace="rig-demo",
                output_dir=directory,
                template_path=sjabloon,
                created_files=[],
                private_key="AGE-SECRET-KEY-NIET-ECHT",
            )

    meting.eis_onder(_GRENS, "het lezen van de vorige ciphertext")
