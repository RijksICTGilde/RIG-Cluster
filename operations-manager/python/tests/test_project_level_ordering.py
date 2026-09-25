"""De ordening tussen het projectniveau en de deployment-applicaties (RC-229).

De ServiceAccount van een project komt uit de ``*-project``-applicatie, en de podspec van
elke Deployment eronder noemt hem. Stond de Deployment er eerder, dan weigert de
ReplicaSet-controller te starten met ``serviceaccount "..." not found``.

Die ordening leunde op de sync-wave (projectniveau 0, deployments 1) en daar valt ze niet
op: een net aangemaakte kind-Application beheert nul resources en meldt zich daarmee binnen
een seconde Healthy, dus de wave-0-grendel van de umbrella gaat open voordat het
projectniveau zijn ServiceAccount heeft uitgerold. Gemeten in
``docs/rc229-welke-grendel-de-serviceaccount-liet-lopen.md``.

Wat de ordening nu draagt is de AANMAAKVOLGORDE: de deployment-applicaties gaan pas naar
git nadat het projectniveau gesynchroniseerd is. Bestaat hun CR nog niet, dan kan ArgoCD ze
ook niet zelfstandig synchroniseren. Deze toetsen staan op die volgorde, want de toets op de
wave-annotatie blijft groen terwijl de ordening stuk is.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from opi.generation.manifests import ManifestGenerator
from opi.manager.argo_manager import PROJECT_LEVEL_SYNC_TIMEOUT_SECONDEN, ArgoManager
from opi.utils.naming import generate_argocd_project_application_name

PROJECT_APP = "demo-project"


def _argo_manager(sporen: list[str]) -> ArgoManager:
    """Een ArgoManager die elke stap in ``sporen`` schrijft in plaats van hem te doen.

    Alleen de stappen die de ordening bepalen zijn gemocked; de volgorde waarin
    ``create_argocd_resources`` ze aanroept is wat hier getoetst wordt.
    """
    project_manager = MagicMock()
    project_manager.get_name = AsyncMock(return_value="demo")
    project_manager.get_contents = AsyncMock(return_value={"name": "demo"})

    async def _wacht(app_name: str) -> None:
        sporen.append(f"wacht:{app_name}")

    project_manager.wait_for_project_level_application = AsyncMock(side_effect=_wacht)

    git_connector = MagicMock()

    async def _push(message: str) -> None:
        sporen.append("push:projectniveau" if "project-level" in message else "push:deployments")

    git_connector.commit_and_push = AsyncMock(side_effect=_push)
    git_connector.get_local_commit_hash = AsyncMock(return_value="cafebabe")
    project_manager.get_git_connector_for_argocd = AsyncMock(return_value=git_connector)

    manager = ArgoManager(project_manager)

    async def _secrets(*_args: Any, **_kwargs: Any) -> None:
        sporen.append("repository-secrets")

    async def _appprojects(*_args: Any, **_kwargs: Any) -> None:
        sporen.append("appprojects")

    async def _applications(*_args: Any, **_kwargs: Any) -> bool:
        sporen.append("deployment-applicaties")
        return True

    async def _project_application(*_args: Any, **_kwargs: Any) -> str | None:
        sporen.append("projectapplicatie")
        return PROJECT_APP

    async def _kustomization(*_args: Any, **_kwargs: Any) -> None:
        sporen.append("kustomization")

    manager.create_repository_secrets = AsyncMock(side_effect=_secrets)  # type: ignore[method-assign]
    manager.create_app_projects = AsyncMock(side_effect=_appprojects)  # type: ignore[method-assign]
    manager.create_applications = AsyncMock(side_effect=_applications)  # type: ignore[method-assign]
    manager.create_project_application = AsyncMock(side_effect=_project_application)  # type: ignore[method-assign]
    manager.create_kustomization_files = AsyncMock(side_effect=_kustomization)  # type: ignore[method-assign]
    return manager


class TestDeAanmaakvolgorde:
    @pytest.mark.asyncio
    async def test_de_deployment_applicaties_gaan_pas_na_de_sync_van_het_projectniveau_naar_git(
        self,
    ) -> None:
        """De grendel: de Deployment kan niet bestaan voordat de ServiceAccount er staat."""
        sporen: list[str] = []
        await _argo_manager(sporen).create_argocd_resources()

        wacht = sporen.index(f"wacht:{PROJECT_APP}")
        # Het projectniveau staat in git EN is gesynchroniseerd voordat de
        # deployment-applicaties ook maar geschreven worden.
        assert sporen.index("projectapplicatie") < sporen.index("push:projectniveau") < wacht
        assert wacht < sporen.index("deployment-applicaties") < sporen.index("push:deployments")

    @pytest.mark.asyncio
    async def test_het_projectniveau_gaat_in_een_eigen_commit(self) -> None:
        """Eén commit zou de deployment-applicaties laten bestaan voordat er iets te
        wachten valt: de umbrella maakt dan beide CR's in dezelfde sync aan."""
        sporen: list[str] = []
        await _argo_manager(sporen).create_argocd_resources()

        assert [stap for stap in sporen if stap.startswith("push:")] == [
            "push:projectniveau",
            "push:deployments",
        ]
        eerste_push = sporen.index("push:projectniveau")
        assert "deployment-applicaties" not in sporen[:eerste_push]

    @pytest.mark.asyncio
    async def test_de_appproject_van_het_projectniveau_staat_in_dezelfde_commit(self) -> None:
        """De projectapplicatie verwijst naar zijn AppProject; kwam die pas in de tweede
        commit, dan faalt de eerste sync op een project dat nog niet bestaat."""
        sporen: list[str] = []
        await _argo_manager(sporen).create_argocd_resources()

        eerste_push = sporen.index("push:projectniveau")
        assert "appprojects" in sporen[:eerste_push]
        assert "repository-secrets" in sporen[:eerste_push]

    @pytest.mark.asyncio
    async def test_zonder_projectniveau_wordt_er_niet_gewacht(self) -> None:
        """Een project zonder deployments op dit cluster heeft geen projectapplicatie, en
        dan is er niets om op te wachten. Anders zou de aanmaak wachten op een applicatie
        die nooit komt."""
        sporen: list[str] = []
        manager = _argo_manager(sporen)
        manager.create_project_application = AsyncMock(return_value=None)  # type: ignore[method-assign]

        await manager.create_argocd_resources()

        manager.project_manager.wait_for_project_level_application.assert_not_awaited()
        assert "deployment-applicaties" in sporen

    @pytest.mark.asyncio
    async def test_de_volledige_reeks_ligt_vast_inclusief_de_kustomization_per_fase(self) -> None:
        """De kustomization draagt de splitsing, en wel twee keer.

        ArgoCD leest de projectmap in de argo-applications repo via die ene kustomization:
        wat er niet in ``resources:`` staat, bestaat voor de umbrella niet. Ontbreekt de
        eerste aanroep, dan staat het manifest van de projectapplicatie wel in de eerste
        commit maar niet in de kustomization, maakt de umbrella die CR nooit aan en loopt de
        grendel eronder zijn volle timeout vol. Ontbreekt de tweede, dan geldt hetzelfde
        voor de deployment-applicaties. Vandaar de hele reeks en niet alleen de volgorde van
        de twee pushes.
        """
        sporen: list[str] = []
        await _argo_manager(sporen).create_argocd_resources()

        assert sporen == [
            "repository-secrets",
            "appprojects",
            "projectapplicatie",
            "kustomization",
            "push:projectniveau",
            f"wacht:{PROJECT_APP}",
            "deployment-applicaties",
            "kustomization",
            "push:deployments",
        ]

    @pytest.mark.asyncio
    async def test_een_mislukte_grendel_stopt_de_aanmaak(self) -> None:
        """Doorlopen met een warning is precies wat RC-229 afwijst.

        De oude wacht ving ``(TimeoutError, RuntimeError)`` af en logde een warning. Bleef
        dat zo, dan pusht de tweede commit de deployment-applicaties alsnog en staat de
        Deployment er weer voor zijn ServiceAccount: de fout is dan hersteld op papier. De
        fout hoort door te lopen naar ``process_project``, die er ``return False`` van
        maakt.
        """
        sporen: list[str] = []
        manager = _argo_manager(sporen)
        manager.project_manager.wait_for_project_level_application = AsyncMock(
            side_effect=TimeoutError("Timed out after 240s waiting for 'demo-project' to sync")
        )

        with pytest.raises(TimeoutError):
            await manager.create_argocd_resources()

        manager.create_applications.assert_not_awaited()
        assert [stap for stap in sporen if stap.startswith("push:")] == ["push:projectniveau"]

    @pytest.mark.asyncio
    async def test_een_onbereikbare_argocd_stopt_de_aanmaak_ook(self) -> None:
        """Tweede foutvorm van de grendel: geen timeout maar een RuntimeError (login
        mislukt, refresh levert niets op). Die mag net zomin geslikt worden."""
        sporen: list[str] = []
        manager = _argo_manager(sporen)
        manager.project_manager.wait_for_project_level_application = AsyncMock(
            side_effect=RuntimeError("Failed to login to ArgoCD")
        )

        with pytest.raises(RuntimeError):
            await manager.create_argocd_resources()

        manager.create_applications.assert_not_awaited()
        assert "push:deployments" not in sporen


def _project_manager(monkeypatch: Any, connector: AsyncMock, *, bestaat_al: bool) -> Any:
    """Een ProjectManager met alleen wat de grendel aanraakt.

    ``bestaat_al`` is wat de Kubernetes-API over de applicatie zegt: bij True hoeft de
    umbrella niet ververst te worden.
    """
    from opi.manager import project_manager as pm_module

    monkeypatch.setattr(pm_module, "create_argo_connector", lambda: connector)

    manager = pm_module.ProjectManager.__new__(pm_module.ProjectManager)
    manager._argo_manager = MagicMock()
    manager._argo_manager.last_pushed_argo_commit = "cafebabe"
    manager._argo_manager.wait_for_application_created = AsyncMock(return_value=True)
    manager._argo_manager.wait_for_application_synced = AsyncMock(return_value=True)
    manager._kubectl_connector = MagicMock()
    manager._kubectl_connector.argocd_application_exists = AsyncMock(return_value=bestaat_al)
    manager._keep_umbrella_refreshed = AsyncMock(return_value=None)
    return manager


class TestDeGrendelZelf:
    @pytest.mark.asyncio
    async def test_de_wacht_eist_een_sync_na_onze_eigen_refresh(self, monkeypatch: Any) -> None:
        """Zonder ``refreshed_after`` stelt een ``Synced`` van VOOR onze commit de wacht bij
        een herhaalrun meteen tevreden, en dan is de grendel er alleen op papier."""
        connector = AsyncMock()
        connector.login = AsyncMock(return_value=True)
        connector.refresh_application = AsyncMock(return_value="2026-09-25T09:32:17Z")
        manager = _project_manager(monkeypatch, connector, bestaat_al=False)

        await manager.wait_for_project_level_application(PROJECT_APP)

        connector.refresh_application.assert_awaited_once_with(PROJECT_APP)
        kwargs = manager._argo_manager.wait_for_application_synced.await_args.kwargs
        assert kwargs["app_name"] == PROJECT_APP
        assert kwargs["refreshed_after"] == "2026-09-25T09:32:17Z"

    @pytest.mark.asyncio
    async def test_de_applicatie_moet_eerst_bestaan_en_dan_gesynchroniseerd_zijn(self, monkeypatch: Any) -> None:
        """Verversen voordat ArgoCD de applicatie heeft aangemaakt levert niets op, dus de
        volgorde binnen de grendel doet ook mee."""
        sporen: list[str] = []
        connector = AsyncMock()
        connector.login = AsyncMock(return_value=True)

        async def _refresh(app_name: str) -> str:
            sporen.append(f"refresh:{app_name}")
            return "2026-09-25T09:32:17Z"

        connector.refresh_application = AsyncMock(side_effect=_refresh)
        manager = _project_manager(monkeypatch, connector, bestaat_al=False)

        async def _created(**kwargs: Any) -> bool:
            sporen.append(f"created:{kwargs['app_name']}")
            return True

        async def _synced(**kwargs: Any) -> bool:
            sporen.append(f"synced:{kwargs['app_name']}")
            return True

        manager._argo_manager.wait_for_application_created = AsyncMock(side_effect=_created)
        manager._argo_manager.wait_for_application_synced = AsyncMock(side_effect=_synced)

        await manager.wait_for_project_level_application(PROJECT_APP)

        assert sporen == [
            f"created:{PROJECT_APP}",
            f"refresh:{PROJECT_APP}",
            f"synced:{PROJECT_APP}",
        ]

    @pytest.mark.asyncio
    async def test_een_refresh_die_niets_oplevert_stopt_de_aanmaak(self, monkeypatch: Any) -> None:
        """Mislukken mag hier geen logger.warning zijn: een Deployment die zijn
        ServiceAccount mist haalt de 300s van de aanmaaktaak niet."""
        connector = AsyncMock()
        connector.login = AsyncMock(return_value=True)
        connector.refresh_application = AsyncMock(return_value=None)
        manager = _project_manager(monkeypatch, connector, bestaat_al=False)

        with pytest.raises(RuntimeError, match="refresh"):
            await manager.wait_for_project_level_application(PROJECT_APP)

        manager._argo_manager.wait_for_application_synced.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_een_bestaande_applicatie_kost_geen_umbrella_refresh(self, monkeypatch: Any) -> None:
        """Een refresh van user-applications hertekent circa 90 child-apps en kan minuten
        duren. Bij een herhaalrun bestaat de projectapplicatie al, en dan is die refresh puur
        verlies in het kritieke pad; de sync moet nog wel bewezen worden."""
        connector = AsyncMock()
        connector.login = AsyncMock(return_value=True)
        connector.refresh_application = AsyncMock(return_value="2026-09-25T09:32:17Z")
        manager = _project_manager(monkeypatch, connector, bestaat_al=True)

        await manager.wait_for_project_level_application(PROJECT_APP)

        manager._keep_umbrella_refreshed.assert_not_awaited()
        manager._argo_manager.wait_for_application_created.assert_not_awaited()
        manager._argo_manager.wait_for_application_synced.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_een_onbekend_antwoord_ververst_de_umbrella_alsnog(self, monkeypatch: Any) -> None:
        """Fail safe: ``None`` is "niet kunnen vaststellen", en dat is geen bewijs dat de
        applicatie bestaat. Dan toch verversen, anders wacht de grendel op een CR die niemand
        meer aanmaakt."""
        connector = AsyncMock()
        connector.login = AsyncMock(return_value=True)
        connector.refresh_application = AsyncMock(return_value="2026-09-25T09:32:17Z")
        manager = _project_manager(monkeypatch, connector, bestaat_al=False)
        manager._kubectl_connector.argocd_application_exists = AsyncMock(return_value=None)

        await manager.wait_for_project_level_application(PROJECT_APP)

        manager._argo_manager.wait_for_application_created.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_de_wacht_krijgt_de_afgesproken_bovengrens_mee(self, monkeypatch: Any) -> None:
        """``PROJECT_LEVEL_SYNC_TIMEOUT_SECONDEN`` is de grens waar de constante zijn
        onderbouwing bij draagt: ruimte voor de umbrella plus een sync. Een eigen getal hier
        zou die onderbouwing stil loskoppelen van wat er gebeurt."""
        connector = AsyncMock()
        connector.login = AsyncMock(return_value=True)
        connector.refresh_application = AsyncMock(return_value="2026-09-25T09:32:17Z")
        manager = _project_manager(monkeypatch, connector, bestaat_al=True)

        await manager.wait_for_project_level_application(PROJECT_APP)

        kwargs = manager._argo_manager.wait_for_application_synced.await_args.kwargs
        assert kwargs["timeout"] == PROJECT_LEVEL_SYNC_TIMEOUT_SECONDEN

    @pytest.mark.asyncio
    async def test_een_mislukte_login_stopt_voor_de_wacht(self, monkeypatch: Any) -> None:
        """Zonder login geeft `refresh_application` niets zinnigs terug en wacht de grendel
        op een sync die niemand meer bewijst. Dan liever meteen stoppen, en zeker niet
        alsnog gaan wachten."""
        connector = AsyncMock()
        connector.login = AsyncMock(return_value=False)
        connector.refresh_application = AsyncMock(return_value="2026-09-25T09:32:17Z")
        manager = _project_manager(monkeypatch, connector, bestaat_al=True)

        with pytest.raises(RuntimeError, match="login"):
            await manager.wait_for_project_level_application(PROJECT_APP)

        connector.refresh_application.assert_not_awaited()
        manager._argo_manager.wait_for_application_synced.assert_not_awaited()


class TestWaarDeGrendelZijnNaamVandaanHaalt:
    """``create_project_application`` geeft de naam terug waar de grendel aan hangt.

    Geeft hij ``None``, dan slaat `create_argocd_resources` het wachten over. Dat is juist
    zolang ``None`` echt "er is geen manifest geschreven" betekent: dan is er ook geen
    applicatie die ooit komt. Een naam bij een niet-geschreven manifest zou de aanmaak zijn
    volle timeout laten vollopen, en omgekeerd laat een ``None`` bij een wel geschreven
    manifest de deployment-applicaties er weer voor.
    """

    @pytest.mark.asyncio
    async def test_zonder_deployments_op_dit_cluster_is_er_niets_om_op_te_wachten(self) -> None:
        project_manager = MagicMock()
        project_manager.get_name = AsyncMock(return_value="demo")
        project_manager.get_deployments = AsyncMock(return_value=[])

        assert await ArgoManager(project_manager).create_project_application({"name": "demo"}) is None

    @pytest.mark.asyncio
    async def test_een_onbekende_repository_levert_geen_naam_op(self) -> None:
        """Er is dan geen manifest geschreven, dus wachten zou op een CR wachten die de
        umbrella nooit kan aanmaken."""
        project_manager = MagicMock()
        project_manager.get_name = AsyncMock(return_value="demo")
        project_manager.get_deployments = AsyncMock(
            return_value=[{"name": "productie", "cluster": "local", "namespace": "demo", "repository": "weg"}]
        )

        resultaat = await ArgoManager(project_manager).create_project_application(
            {"name": "demo", "repositories": [{"name": "main-repo", "url": "https://example.test/x.git"}]}
        )

        assert resultaat is None

    @pytest.mark.asyncio
    async def test_een_geschreven_manifest_levert_de_naam_op_waar_de_grendel_op_wacht(self, tmp_path: Any) -> None:
        """De naam moet dezelfde zijn als die de lezers elders uitrekenen, want de grendel
        wacht op de applicatie die de umbrella straks onder die naam aanmaakt."""
        git_connector = MagicMock()
        git_connector.get_working_dir = AsyncMock(return_value=str(tmp_path))
        project_manager = MagicMock()
        project_manager._manifest_generator = ManifestGenerator()
        project_manager.get_name = AsyncMock(return_value="demo")
        project_manager.get_deployments = AsyncMock(
            return_value=[
                {"name": "productie", "cluster": "local", "namespace": "demo", "repository": "main-repo"},
            ]
        )
        project_manager.get_git_connector_for_argocd = AsyncMock(return_value=git_connector)

        resultaat = await ArgoManager(project_manager).create_project_application(
            {
                "name": "demo",
                "repositories": [{"name": "main-repo", "url": "https://example.test/x.git", "path": "."}],
            }
        )

        assert resultaat == generate_argocd_project_application_name("demo")
        geschreven = list(tmp_path.rglob("*.yaml"))
        assert len(geschreven) == 1
        assert resultaat in geschreven[0].read_text()


class TestDeVerversbewaker:
    """De bewaker is een vangnet: valt hij zelf om, dan mag hij de wacht niet meeslepen.

    Vandaar de generieke ``except`` in ``_umbrella_verversbewaker``, tegen de afspraak in
    ``CLAUDE.md`` dat een nieuwe ``except`` specifiek is. Hij staat nu op een plek in plaats
    van drie, en dit is wat hij moet doen: een wacht op het bestaan van een applicatie hoort
    niet te stranden op een ververspoging die ernaast liep.
    """

    @pytest.mark.asyncio
    async def test_een_omgevallen_bewaker_sleept_de_wacht_niet_mee(self, caplog: Any) -> None:
        from opi.manager import project_manager as pm_module

        manager = pm_module.ProjectManager.__new__(pm_module.ProjectManager)
        manager._argo_manager = MagicMock()
        manager._argo_manager.last_pushed_argo_commit = "cafebabe"

        async def _valt_om(*_args: Any) -> None:
            raise ValueError("ArgoCD gaf iets terug wat de bewaker niet aankan")

        manager._keep_umbrella_refreshed = _valt_om  # type: ignore[method-assign]

        gewacht = False
        with caplog.at_level(logging.WARNING):
            async with manager._umbrella_verversbewaker(AsyncMock()):
                # De bewaker draait naast de wacht, dus hij moet wel aan de beurt komen.
                await asyncio.sleep(0)
                gewacht = True

        assert gewacht
        assert "Verversbewaker voor user-applications gestopt" in caplog.text

    @pytest.mark.asyncio
    async def test_de_bewaker_stopt_als_de_wacht_klaar_is(self) -> None:
        """Anders blijft hij de umbrella prikken terwijl er niemand meer op wacht."""
        from opi.manager import project_manager as pm_module

        manager = pm_module.ProjectManager.__new__(pm_module.ProjectManager)
        manager._argo_manager = MagicMock()
        manager._argo_manager.last_pushed_argo_commit = "cafebabe"

        gestopt = asyncio.Event()

        async def _blijft_hangen(*_args: Any) -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                gestopt.set()
                raise

        manager._keep_umbrella_refreshed = _blijft_hangen  # type: ignore[method-assign]

        async def _wacht() -> None:
            async with manager._umbrella_verversbewaker(AsyncMock()):
                await asyncio.sleep(0)

        # Met een timeout, want zonder de cancel wacht het afsluiten van de bewaker op een
        # taak die nooit eindigt: dat moet hier rood worden en niet blijven hangen.
        await asyncio.wait_for(_wacht(), timeout=5)

        assert gestopt.is_set()
