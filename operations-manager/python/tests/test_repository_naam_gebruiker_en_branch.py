"""``repositories[].name``, ``.username`` en ``.branch`` zijn begrensd, en het wachtwoord ook.

De drie velden gaan uit het projectbestand rechtstreeks een ArgoCD-Secret in. De sjablonen
coderen ze als YAML-scalar (``tests/test_argocd_templates_escaping.py``); dit is de laag
ervoor, die een waarde weigert die daar niets te zoeken heeft. Het ontsleutelde wachtwoord
komt niet uit het schema maar uit AGE, dus dat wordt bij de bron gecontroleerd.
"""

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from opi.core.config import settings
from opi.core.project_schema import ProjectSchemaError, validate_project_schema
from opi.manager.argo_manager import ArgoManager

GELDIG: dict[str, list[str]] = {
    "name": ["main-repo", "github-repo", "main-repo-autocreated", "r1"],
    "username": ["git", "rig-admin", "someuser", "mail@rijksoverheid.nl", "a_b.c-d"],
    "branch": ["main", "release/2026.10", "v1.2.3", "feature_x"],
}

ONGELDIG: dict[str, list[str]] = {
    "name": ["Main-Repo", "main_repo", "-main", "main-", "main repo", "main\nrepo", ""],
    "username": ["git user", "git\nroot", 'git"', "", "git:token"],
    "branch": ["main\nextra", "release/../main", "a..b", "main branch", "", "branch#1"],
}


def _project(**repository: Any) -> dict[str, Any]:
    return {
        "schema-version": 2.9,
        "name": "demo",
        "clusters": ["odcn-production"],
        "users": [{"email": "admin@rijksoverheid.nl", "role": "admin"}],
        "repositories": [
            {"name": "main-repo", "url": "https://example.org/demo.git", "branch": "main", "path": ".", **repository}
        ],
    }


class TestHetSchemaBegrenstDeRepository:
    @pytest.mark.parametrize(
        ("veld", "waarde"), [(veld, waarde) for veld, waarden in ONGELDIG.items() for waarde in waarden]
    )
    def test_weigert(self, veld: str, waarde: str) -> None:
        with pytest.raises(ProjectSchemaError) as exc:
            validate_project_schema(_project(**{veld: waarde}))
        assert exc.value.field_path == f"repositories/0/{veld}"

    @pytest.mark.parametrize(
        ("veld", "waarde"), [(veld, waarde) for veld, waarden in GELDIG.items() for waarde in waarden]
    )
    def test_laat_door(self, veld: str, waarde: str) -> None:
        validate_project_schema(_project(**{veld: waarde}))

    @pytest.mark.parametrize("veld", list(ONGELDIG))
    def test_de_melding_noemt_de_regel(self, veld: str) -> None:
        with pytest.raises(ProjectSchemaError) as exc:
            validate_project_schema(_project(**{veld: "een waarde met witruimte"}))
        assert f"Veld 'repositories/0/{veld}'" in str(exc.value)
        assert "zonder witruimte" in str(exc.value) or "kleine letters" in str(exc.value)


class TestHetOntsleuteldeWachtwoord:
    """Het schema ziet alleen de ciphertext; wat eruit komt wordt hier gecontroleerd."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("wachtwoord", ["plain:token\nmet-regeleinde", "plain:token\rmet-return", "plain:a\x00b"])
    async def test_weigert_een_regeleinde_of_nul(self, wachtwoord: str) -> None:
        manager = ArgoManager(MagicMock())
        with pytest.raises(ValueError, match="main-repo"):
            await manager.prepare_repository_variables(
                name="demo-main-repo",
                namespace="argocd",
                repository={"name": "main-repo", "url": "https://example.org/demo.git", "password": wachtwoord},
                repo_type="git",
                project_name="demo",
            )

    @pytest.mark.asyncio
    async def test_laat_een_gewoon_token_door(self) -> None:
        manager = ArgoManager(MagicMock())
        with patch.object(settings, "SOPS_AGE_PRIVATE_KEY", None):
            variabelen = await manager.prepare_repository_variables(
                name="demo-main-repo",
                namespace="argocd",
                repository={"name": "main-repo", "url": "https://example.org/demo.git", "password": "plain:ghp_x"},
                repo_type="git",
                project_name="demo",
            )
        assert variabelen["password"] == "ghp_x"
