"""Aanvulling op ``test_add_service_binds_existing.py``: de andere kanten van de opslagpoort.

De breuk zelf -- een component binden aan een al geselecteerde dienst werd gemeld maar niet
opgeslagen -- staat vast in ``test_add_service_binds_existing.py``, samen met de uitrolpoort
en de eerlijkheid van ``components_updated``. Dit bestand herhaalt dat niet en dekt wat daar
niet in zit:

* de COMMITBOODSCHAP noemt wat er werkelijk gebeurde. Zonder die poort mag de boodschap
  "add service(s)  in project 'demo'" worden zodra ``services_added`` leeg is, en dan is in
  de git-geschiedenis niet meer te zien dat er een binding was;
* ``component_names=None`` op een dienst die al staat -- de "niets veranderd"-test hiernaast
  geeft wel een component mee, dus dit pad kwam er niet langs;
* de weg die altijd al werkte: een NIEUWE dienst, met en zonder componenten. De poort werd
  verruimd, en die verruiming mag de gewone toevoeging niet omgooien.

Git is gemockt op ``save_and_commit_project``: het onderwerp is de opslagbeslissing, niet de
git-weg.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


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


def _wire(project_data: dict):
    """Een manager die dit project leest, met de commit gemockt. Geeft (manager, save)."""
    manager = _manager()
    manager.get_contents = AsyncMock(return_value=project_data)
    manager.get_name = AsyncMock(return_value="demo")
    save = AsyncMock()
    manager.save_and_commit_project = save
    return manager, save


def _project(project_services: list, component_services: list) -> dict:
    return {
        "name": "demo",
        "services": list(project_services),
        "components": [{"name": "web", "services": list(component_services)}],
    }


def _component_service_names(project_data: dict) -> list[str]:
    entries = project_data["components"][0].get("services") or []
    return [e if isinstance(e, str) else e.get("reference") for e in entries]


class TestDeCommitboodschap:
    """Wat er in de git-geschiedenis komt te staan, moet de binding noemen."""

    @pytest.mark.asyncio
    async def test_de_boodschap_noemt_het_component_en_de_dienst(self):
        manager, save = _wire(_project(["publish-on-web"], []))

        await manager.add_service("publish-on-web", ["web"])

        boodschap = save.await_args.args[1]
        assert "web" in boodschap
        assert "publish-on-web" in boodschap

    @pytest.mark.asyncio
    async def test_bij_een_nieuwe_dienst_noemt_de_boodschap_de_toevoeging(self):
        manager, save = _wire(_project([], []))

        await manager.add_service("publish-on-web", ["web"])

        boodschap = save.await_args.args[1]
        assert "publish-on-web" in boodschap
        assert "demo" in boodschap


class TestNietsVeranderd:
    """Geen mutatie, geen commit -- die kant mag de verruimde poort niet omgooien."""

    @pytest.mark.asyncio
    async def test_zonder_componenten_en_dienst_bestaat_al(self):
        manager, save = _wire(_project(["publish-on-web"], []))

        result = await manager.add_service("publish-on-web", None)

        assert result["components_updated"] == []
        assert save.await_count == 0


class TestGewoneToevoeging:
    """De weg die altijd al werkte, blijft werken."""

    @pytest.mark.asyncio
    async def test_nieuwe_dienst_op_project_en_component(self):
        project_data = _project([], [])
        manager, save = _wire(project_data)

        result = await manager.add_service("publish-on-web", ["web"])

        assert "publish-on-web" in result["services_added"]
        assert result["components_updated"] == ["web"]
        assert save.await_count == 1
        assert "publish-on-web" in _component_service_names(project_data)

    @pytest.mark.asyncio
    async def test_nieuwe_dienst_zonder_componenten(self):
        project_data = _project([], [])
        manager, save = _wire(project_data)

        result = await manager.add_service("publish-on-web", None)

        assert "publish-on-web" in result["services_added"]
        assert result["components_updated"] == []
        assert save.await_count == 1
        assert _component_service_names(project_data) == []
