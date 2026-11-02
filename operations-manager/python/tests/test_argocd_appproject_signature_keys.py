"""Het AppProject van een project eist de handtekening van OPI's commits.

Een handtekening die niemand controleert is documentatie: met ``signatureKeys`` weigert
ArgoCD een revisie waarvan de commit niet door die sleutel is ondertekend. Zonder sleutel
hoort het veld te ontbreken, zodat een cluster waar OPI niet ondertekent blijft werken.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
import yaml
from opi.generation.manifests import ManifestGenerator
from opi.manager.argo_manager import ArgoManager
from opi.utils import git_signing
from opi.utils.git_signing import GitSigningIdentity

if TYPE_CHECKING:
    from collections.abc import Iterator

_FINGERPRINT = "1234567890ABCDEF1234567890ABCDEFFEDCBA98"


@pytest.fixture
def argo_manager() -> ArgoManager:
    project_manager = MagicMock()
    project_manager._manifest_generator = ManifestGenerator()
    return ArgoManager(project_manager)


@pytest.fixture
def ondertekenend() -> Iterator[None]:
    vorige = git_signing._identity
    git_signing._identity = GitSigningIdentity(fingerprint=_FINGERPRINT, gnupg_home="/tmp/opi-gnupg")
    yield
    git_signing._identity = vorige


def _render(argo_manager: ArgoManager) -> dict:
    manifest = argo_manager.generate_appproject_manifest(
        name="my-project-my-project",
        namespace="rig-system",
        destination_namespace="rig-my-project",
        project_label="my-project",
        source_repos=["https://my-project@github.com/RijksICTGilde/zad-deployments.git"],
    )
    return yaml.safe_load(manifest)


def test_met_signing_key_eist_het_appproject_de_long_key_id(argo_manager: ArgoManager, ondertekenend: None) -> None:
    """ArgoCD valideert ``keyID`` als key ID (8 of 16 hex) en weigert een hele fingerprint."""
    assert _render(argo_manager)["spec"]["signatureKeys"] == [{"keyID": _FINGERPRINT[-16:]}]


def test_zonder_signing_key_rendert_het_appproject_geen_signaturekeys(argo_manager: ArgoManager) -> None:
    assert "signatureKeys" not in _render(argo_manager)["spec"]
