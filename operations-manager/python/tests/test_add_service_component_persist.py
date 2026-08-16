"""ProjectManager.add_service: wanneer er wél en wanneer er niet opgeslagen wordt.

De poort die dit bewaakt keek alleen naar ``services_added``. Dat is niet hetzelfde als
"er is iets veranderd": staat de dienst al op PROJECTniveau maar nog niet op het gevraagde
component, dan is ``services_added`` leeg terwijl de componentlijst wel gemuteerd is. De
mutatie werd dan zonder commit weggegooid, terwijl het antwoord hem als
``components_updated`` meldde. De API zei dus dat het component bijgewerkt was en er
veranderde niets -- gemeten op de sandbox tijdens de doorloop van RC-118, waar
``POST /api/projects/{p}/services`` met ``components: ["web"]`` ``success`` gaf, geen
commit in ``zad-projects`` achterliet en de serviceslijst van het component leeg hield.

Git is gemockt op ``save_and_commit_project``: het onderwerp is de OPSLAGBESLISSING, niet
de git-weg. Daarom controleert elke test of er opgeslagen is EN wat er dan in
``project_data`` staat -- zonder die tweede helft blijft een save die het verkeerde
bewaart onzichtbaar.
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


class TestDienstStaatAlOpHetProject:
    """De breuk: niets toe te voegen op projectniveau, wel op het component."""

    @pytest.mark.asyncio
    async def test_component_koppeling_wordt_opgeslagen(self):
        project_data = _project(["publish-on-web"], [])
        manager, save = _wire(project_data)

        result = await manager.add_service("publish-on-web", ["web"])

        assert result["success"] is True
        assert result["services_added"] == []
        assert result["components_updated"] == ["web"]
        assert save.await_count == 1, "de component-mutatie is niet opgeslagen"

    @pytest.mark.asyncio
    async def test_de_dienst_staat_daarna_echt_op_het_component(self):
        project_data = _project(["publish-on-web"], [])
        manager, save = _wire(project_data)

        await manager.add_service("publish-on-web", ["web"])

        assert "publish-on-web" in _component_service_names(project_data)
        opgeslagen = save.await_args.args[0]
        assert "publish-on-web" in _component_service_names(opgeslagen)

    @pytest.mark.asyncio
    async def test_de_commitboodschap_noemt_het_component(self):
        manager, save = _wire(_project(["publish-on-web"], []))

        await manager.add_service("publish-on-web", ["web"])

        boodschap = save.await_args.args[1]
        assert "web" in boodschap
        assert "publish-on-web" in boodschap


class TestNietsVeranderd:
    """Geen mutatie, geen commit -- die kant mag de fix niet omgooien."""

    @pytest.mark.asyncio
    async def test_dienst_staat_al_op_project_en_component(self):
        manager, save = _wire(_project(["publish-on-web"], ["publish-on-web"]))

        result = await manager.add_service("publish-on-web", ["web"])

        assert result["services_added"] == []
        assert result["components_updated"] == []
        assert save.await_count == 0, "er is opgeslagen terwijl er niets veranderde"

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
