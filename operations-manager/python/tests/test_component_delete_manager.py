"""ProjectManager.delete_component: what leaves the project file, and what refuses (RC-73).

The endpoint layer decides the status code; this is the layer that decides whether the
deletion may happen at all and, when it does, what else has to go with it.

The rule the design turns on is the one RC-52 already settled for attachments: a reference
left pointing at something that no longer exists is worse than the thing itself lingering.
A deployment referencing a deleted component makes the project file invalid, so the refusal
is the default and the confirmed variant is ONE save that takes the references with it --
never two saves that can half-succeed.

Before this, deleting a component that any deployment deployed simply hit the reference
check at save time and came back as "An internal error occurred", which told the caller
nothing about what was wrong.

Git is mocked at ``save_and_commit_project``: the subject is the mutation and the refusal.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from opi.core.project_schema import ProjectIntegrityError
from opi.handlers.project_file_handler import component_usage_sites

WEB = "web"


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
    """A manager reading this project, with the commit mocked. Returns (manager, save)."""
    manager = _manager()
    manager.get_contents = AsyncMock(return_value=project_data)
    manager.get_name = AsyncMock(return_value="demo")
    save = AsyncMock()
    manager.save_and_commit_project = save
    return manager, save


def _project(*, components: list | None = None, deployments: list | None = None) -> dict:
    return {
        "schema-version": 2,
        "name": "demo",
        "components": [{"name": WEB}] if components is None else components,
        "deployments": deployments if deployments is not None else [],
    }


def _deployment(name: str = "staging", *refs: str, config: dict | None = None) -> dict:
    deployment: dict = {
        "name": name,
        "cluster": "local",
        "namespace": "demo",
        "components": [{"reference": r} for r in refs],
    }
    if config is not None:
        deployment["services"] = [{"reference": "publish-on-web", "config": config}]
    return deployment


def _names(project: dict) -> list[str]:
    return [c["name"] for c in project["components"]]


# ---------------------------------------------------------------------------
# The free case: no confirmation needed
# ---------------------------------------------------------------------------


class TestAComponentNothingUses:
    async def test_it_simply_goes(self) -> None:
        project = _project()
        manager, save = _wire(project)

        result = await manager.delete_component(WEB)

        assert result["success"]
        assert _names(project) == []
        save.assert_awaited_once()

    async def test_it_needs_no_confirmation(self) -> None:
        """The normal case stays cheap: only a deletion with consequences has to be
        acknowledged, and this one has none."""
        project = _project()
        manager, _ = _wire(project)

        result = await manager.delete_component(WEB)

        assert result["success"]
        assert result["uncoupled_from"] == []

    async def test_a_component_that_is_not_there_is_said_so(self) -> None:
        """Not a silent success: reporting one would tell the caller their name was right."""
        project = _project()
        manager, save = _wire(project)

        result = await manager.delete_component("nope")

        assert result["success"] is False
        assert result["error_type"] == "not_found"
        save.assert_not_awaited()


# ---------------------------------------------------------------------------
# The refusal, and what it tells you
# ---------------------------------------------------------------------------


class TestAComponentInUse:
    async def test_a_deployed_component_is_refused(self) -> None:
        project = _project(deployments=[_deployment("staging", WEB)])
        manager, save = _wire(project)

        result = await manager.delete_component(WEB)

        assert result["success"] is False
        assert result["error_type"] == "in_use"
        save.assert_not_awaited()
        assert _names(project) == [WEB]

    async def test_a_component_something_depends_on_is_refused(self) -> None:
        project = _project(components=[{"name": WEB}, {"name": "worker", "uses-components": [WEB]}])
        manager, _ = _wire(project)

        result = await manager.delete_component(WEB)

        assert result["error_type"] == "in_use"
        assert [u["label"] for u in result["used_by"]] == ["component 'worker'"]

    async def test_the_refusal_says_where_it_is_used(self) -> None:
        """Not just 'no': the caller gets the places, so it can show what it would break
        instead of sending the user hunting."""
        project = _project(
            components=[{"name": WEB}, {"name": "worker", "uses-components": [WEB]}],
            deployments=[_deployment("staging", WEB), _deployment("production", WEB)],
        )
        manager, _ = _wire(project)

        result = await manager.delete_component(WEB)

        assert [u["label"] for u in result["used_by"]] == [
            "component 'worker'",
            "deployment 'staging'",
            "deployment 'production'",
        ]
        assert "deployment 'production'" in result["error"]

    async def test_nothing_is_written_before_the_refusal(self) -> None:
        """The refusal has to leave the project exactly as it was -- a guard that ran after
        a mutation would be a half-deletion by another name."""
        project = _project(deployments=[_deployment("staging", WEB)])
        before = str(project)
        manager, _ = _wire(project)

        await manager.delete_component(WEB)

        assert str(project) == before


# ---------------------------------------------------------------------------
# The confirmed variant
# ---------------------------------------------------------------------------


class TestWithTheConfirmation:
    async def test_the_deployment_entries_go_with_it(self) -> None:
        project = _project(deployments=[_deployment("staging", WEB, "worker")])
        manager, save = _wire(project)

        result = await manager.delete_component(WEB, confirm_in_use=True)

        assert result["success"]
        assert _names(project) == []
        assert project["deployments"][0]["components"] == [{"reference": "worker"}]
        assert [u["label"] for u in result["uncoupled_from"]] == ["deployment 'staging'"]
        save.assert_awaited_once()

    async def test_the_dependency_declarations_go_with_it(self) -> None:
        project = _project(components=[{"name": WEB}, {"name": "worker", "uses-components": [WEB]}])
        manager, _ = _wire(project)

        result = await manager.delete_component(WEB, confirm_in_use=True)

        assert project["components"][0]["uses-components"] == []
        assert [u["label"] for u in result["uncoupled_from"]] == ["component 'worker'"]

    async def test_it_is_one_save_and_leaves_nothing_pointing_at_the_component(self) -> None:
        """The property the whole shape exists for: after the single commit, no reference
        to the deleted component survives anywhere."""
        project = _project(
            components=[{"name": WEB}, {"name": "worker", "uses-components": [WEB]}],
            deployments=[_deployment("staging", WEB, "worker"), _deployment("production", WEB)],
        )
        manager, save = _wire(project)

        await manager.delete_component(WEB, confirm_in_use=True)

        assert save.await_count == 1
        assert WEB not in component_usage_sites(project)
        assert _names(project) == ["worker"]

    async def test_the_commit_message_says_what_went_with_it(self) -> None:
        project = _project(deployments=[_deployment("staging", WEB)])
        manager, save = _wire(project)

        await manager.delete_component(WEB, confirm_in_use=True)

        assert "deployment 'staging'" in save.await_args.args[1]


# ---------------------------------------------------------------------------
# The one case the confirmation does not open
# ---------------------------------------------------------------------------


class TestAComponentAWebAddressIsBuiltAround:
    async def test_it_is_refused_even_with_the_confirmation(self) -> None:
        """Dropping it would change how the site is served, and nothing in the request says
        how it should be served instead."""
        project = _project(deployments=[_deployment("staging", WEB, config={"root-component": WEB})])
        manager, save = _wire(project)

        result = await manager.delete_component(WEB, confirm_in_use=True)

        assert result["error_type"] == "in_use"
        assert "webadres" in result["error"]
        save.assert_not_awaited()
        assert _names(project) == [WEB]

    async def test_the_bare_domain_component_is_refused_too(self) -> None:
        project = _project(deployments=[_deployment("staging", WEB, config={"expose-component-on-bare-domain": WEB})])
        manager, _ = _wire(project)

        result = await manager.delete_component(WEB, confirm_in_use=True)

        assert result["error_type"] == "in_use"

    async def test_the_refusal_names_the_deployment_whose_address_it_is(self) -> None:
        project = _project(deployments=[_deployment("staging", WEB, config={"root-component": WEB})])
        manager, _ = _wire(project)

        result = await manager.delete_component(WEB, confirm_in_use=True)

        assert "het webadres van deployment 'staging'" in result["error"]


# ---------------------------------------------------------------------------
# What the save says back
# ---------------------------------------------------------------------------


class TestWhenTheSaveRefuses:
    async def test_a_validation_refusal_is_reported_as_one(self) -> None:
        """It used to arrive as 'An internal error occurred', which said nothing about a
        reference the cleanup missed. The save is the last check that it did not."""
        project = _project()
        manager, save = _wire(project)
        save.side_effect = ProjectIntegrityError("component 'web' is nog in gebruik")

        result = await manager.delete_component(WEB)

        assert result["error_type"] == "validation_error"
        assert "nog in gebruik" in result["error"]

    async def test_an_unexpected_failure_still_says_nothing_it_should_not(self) -> None:
        project = _project()
        manager, save = _wire(project)
        save.side_effect = RuntimeError("git remote hung up: token=abcdef")

        result = await manager.delete_component(WEB)

        assert result["error_type"] == "internal_error"
        assert "abcdef" not in result["error"]
