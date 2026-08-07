"""ProjectManager.remove_attachment: what leaves the project file, and what refuses (RC-52).

The endpoint layer decides the status code; this is the layer that decides whether the
deletion may happen at all and, when it does, what else has to go with it.

The rule the whole design turns on: a reference left pointing at a deleted id is worse
than an attachment left lying around. Today attachments pile up, which is untidy but safe;
half a deletion makes the project file invalid. So the refusal is the default, and the
confirmed variant is one save that takes the couplings with it -- never two saves that can
half-succeed.

Git is mocked at ``save_and_commit_project``: the subject is the mutation and the refusal.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.handlers.project_file_handler import (
    attachment_usage_sites,
    extract_attachment_catalog,
    validate_attachment_references,
)

CERT = "server-cert"


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


def _coupling(reference: str = CERT) -> dict:
    return {"reference": reference, "provide-as": "file", "path": "/etc/ssl/server.pem"}


def _project(
    *, catalog: tuple[str, ...] = (CERT,), components: list | None = None, deployments: list | None = None
) -> dict:
    return {
        "schema-version": 2,
        "name": "demo",
        "services": [
            {
                "attachments": {
                    "data": [
                        {"id": i, "filename": f"{i}.pem", "content": "-----BEGIN AGE ENCRYPTED FILE-----\nx\n"}
                        for i in catalog
                    ]
                }
            }
        ],
        "components": components if components is not None else [],
        "deployments": deployments if deployments is not None else [],
    }


def _coupled_component(name: str = "backend") -> dict:
    return {"name": name, "services": [{"attachments": {"config": [_coupling()]}}]}


# ---------------------------------------------------------------------------
# The free case: no confirmation needed
# ---------------------------------------------------------------------------


class TestAnAttachmentNothingUses:
    async def test_it_simply_goes(self) -> None:
        project = _project(components=[{"name": "backend", "services": ["publish-on-web"]}])
        manager, save = _wire(project)

        result = await manager.remove_attachment(CERT)

        assert result["success"]
        assert result["changed"] is True
        assert extract_attachment_catalog(project) == {}
        save.assert_awaited_once()

    async def test_it_needs_no_confirmation(self) -> None:
        """The normal case stays cheap: only a deletion with consequences has to be
        acknowledged, and this one has none."""
        project = _project()
        manager, _ = _wire(project)

        result = await manager.remove_attachment(CERT)

        assert result["success"]
        assert result["uncoupled_from"] == []

    async def test_an_id_that_is_not_there_changes_nothing(self) -> None:
        project = _project()
        manager, save = _wire(project)

        result = await manager.remove_attachment("nope")

        assert result == {"success": True, "changed": False}
        save.assert_not_awaited()


# ---------------------------------------------------------------------------
# The refusal, and what it tells you
# ---------------------------------------------------------------------------


class TestAnAttachmentInUse:
    async def test_it_is_refused(self) -> None:
        project = _project(components=[_coupled_component()])
        manager, save = _wire(project)

        result = await manager.remove_attachment(CERT)

        assert result["success"] is False
        assert result["error_type"] == "in_use"
        save.assert_not_awaited()
        assert CERT in extract_attachment_catalog(project)

    async def test_the_refusal_says_where_it_is_used(self) -> None:
        """Not just 'no': the caller gets the places, so it can show what it would break
        instead of sending the user hunting."""
        project = _project(
            components=[_coupled_component("backend"), _coupled_component("frontend")],
            deployments=[
                {
                    "name": "staging",
                    "components": [{"reference": "backend", "services": {"attachments": {"config": [_coupling()]}}}],
                }
            ],
        )
        manager, _ = _wire(project)

        result = await manager.remove_attachment(CERT)

        assert [u["component"] for u in result["used_by"]] == ["backend", "frontend", "backend"]
        assert [u["deployment"] for u in result["used_by"]] == [None, None, "staging"]
        assert {u["kind"] for u in result["used_by"]} == {"coupling"}
        assert "backend (staging)" in result["error"]

    async def test_nothing_is_written_before_the_refusal(self) -> None:
        """The refusal has to leave the project exactly as it was -- a guard that ran after
        a mutation would be a half-deletion by another name."""
        project = _project(components=[_coupled_component()])
        before = str(project)
        manager, _ = _wire(project)

        await manager.remove_attachment(CERT)

        assert str(project) == before


# ---------------------------------------------------------------------------
# The confirmed variant
# ---------------------------------------------------------------------------


class TestWithTheConfirmation:
    async def test_the_couplings_go_with_it(self) -> None:
        project = _project(components=[_coupled_component()])
        manager, save = _wire(project)

        result = await manager.remove_attachment(CERT, confirm_in_use=True)

        assert result["success"]
        assert result["changed"] is True
        assert extract_attachment_catalog(project) == {}
        assert attachment_usage_sites(project) == {}
        assert [u["label"] for u in result["uncoupled_from"]] == ["backend"]

    async def test_deployment_components_are_cleaned_too(self) -> None:
        project = _project(
            components=[_coupled_component()],
            deployments=[
                {
                    "name": "staging",
                    "components": [{"reference": "backend", "services": {"attachments": {"config": [_coupling()]}}}],
                }
            ],
        )
        manager, _ = _wire(project)

        result = await manager.remove_attachment(CERT, confirm_in_use=True)

        assert result["success"]
        assert [u["label"] for u in result["uncoupled_from"]] == ["backend", "backend (staging)"]
        assert attachment_usage_sites(project) == {}

    async def test_it_is_one_save_and_not_two(self) -> None:
        """The catalog entry and the couplings leave together. Two saves could half-succeed
        and leave exactly the dangling reference this is all trying to avoid."""
        project = _project(components=[_coupled_component()])
        manager, save = _wire(project)

        await manager.remove_attachment(CERT, confirm_in_use=True)

        assert save.await_count == 1

    async def test_the_result_still_validates(self) -> None:
        project = _project(
            components=[_coupled_component("backend"), _coupled_component("frontend")],
            deployments=[
                {
                    "name": "staging",
                    "components": [{"reference": "backend", "services": {"attachments": {"config": [_coupling()]}}}],
                }
            ],
        )
        manager, _ = _wire(project)

        await manager.remove_attachment(CERT, confirm_in_use=True)

        # The check the plan asks for: no reference anywhere to an id that is gone.
        assert validate_attachment_references(project) == []

    async def test_an_empty_block_goes_but_the_selection_stays(self) -> None:
        project = _project(
            components=[{"name": "backend", "services": ["publish-on-web", {"attachments": {"config": [_coupling()]}}]}]
        )
        manager, _ = _wire(project)

        await manager.remove_attachment(CERT, confirm_in_use=True)

        assert project["components"][0]["services"] == ["publish-on-web", "attachments"]

    async def test_other_attachments_keep_their_couplings(self) -> None:
        project = _project(
            catalog=[CERT, "ca"],
            components=[{"name": "backend", "services": [{"attachments": {"config": [_coupling(), _coupling("ca")]}}]}],
        )
        manager, _ = _wire(project)

        await manager.remove_attachment(CERT, confirm_in_use=True)

        assert project["components"][0]["services"][0]["attachments"]["config"] == [_coupling("ca")]
        assert set(extract_attachment_catalog(project)) == {"ca"}

    async def test_confirming_a_free_attachment_is_harmless(self) -> None:
        project = _project()
        manager, _ = _wire(project)

        result = await manager.remove_attachment(CERT, confirm_in_use=True)

        assert result["success"]
        assert result["uncoupled_from"] == []


# ---------------------------------------------------------------------------
# The one case the confirmation does not cover
# ---------------------------------------------------------------------------


class TestACertificateIsRefusedEvenWhenConfirmed:
    """There is no reference to remove there without deciding how the site is served.

    A coupling can be dropped and the component simply stops receiving a file. A
    publish-on-web ``tls: provided`` reference cannot: the config would be left saying
    "terminate a certificate I supply" with no certificate, which the model rejects
    outright, and silently moving a site onto the platform certificate instead is a
    decision about how the site is served -- not about a file nobody needs any more.
    """

    def _with_certificate(self) -> dict:
        return _project(
            components=[
                {
                    "name": "backend",
                    "services": [{"publish-on-web": {"config": {"tls": "provided", "attachment": CERT}}}],
                }
            ]
        )

    async def test_it_is_refused_without_the_confirmation(self) -> None:
        manager, _ = _wire(self._with_certificate())

        result = await manager.remove_attachment(CERT)

        assert result["error_type"] == "in_use"

    async def test_it_is_still_refused_with_the_confirmation(self) -> None:
        project = self._with_certificate()
        manager, save = _wire(project)

        result = await manager.remove_attachment(CERT, confirm_in_use=True)

        assert result["success"] is False
        assert result["error_type"] == "in_use"
        save.assert_not_awaited()
        assert CERT in extract_attachment_catalog(project)

    async def test_the_refusal_says_what_to_do_instead(self) -> None:
        manager, _ = _wire(self._with_certificate())

        result = await manager.remove_attachment(CERT, confirm_in_use=True)

        assert "TLS-modus" in result["error"]
        assert [u["kind"] for u in result["used_by"]] == ["certificate"]

    async def test_a_project_wide_certificate_is_refused_too(self) -> None:
        project = _project()
        project["services"].append({"publish-on-web": {"config": {"tls": "provided", "attachment": CERT}}})
        manager, _ = _wire(project)

        result = await manager.remove_attachment(CERT, confirm_in_use=True)

        assert result["error_type"] == "in_use"
        assert "publicatie (project-breed)" in result["error"]

    async def test_a_coupling_alongside_a_certificate_is_refused_as_a_whole(self) -> None:
        """Not "clean the couplings and refuse the rest": that would be half a deletion,
        with the attachment still there and its couplings gone."""
        project = _project(
            components=[
                _coupled_component("backend"),
                {
                    "name": "frontend",
                    "services": [{"publish-on-web": {"config": {"tls": "provided", "attachment": CERT}}}],
                },
            ]
        )
        manager, save = _wire(project)

        result = await manager.remove_attachment(CERT, confirm_in_use=True)

        assert result["success"] is False
        save.assert_not_awaited()
        assert attachment_usage_sites(project)[CERT]  # the coupling is still there


# ---------------------------------------------------------------------------
# The save is the last line of defence
# ---------------------------------------------------------------------------


async def test_a_rejected_save_is_reported_rather_than_swallowed() -> None:
    """If a cleanup ever missed a site, the reference check at save is what catches it --
    and the caller must hear that rather than an 'internal error'."""
    from opi.core.project_schema import ProjectSchemaError

    project = _project(components=[_coupled_component()])
    manager, save = _wire(project)
    save.side_effect = ProjectSchemaError("Onbekende bijlage-referentie 'server-cert' gebruikt door: backend")

    result = await manager.remove_attachment(CERT, confirm_in_use=True)

    assert result["success"] is False
    assert result["error_type"] == "validation_error"
    assert "Onbekende bijlage-referentie" in result["error"]


@pytest.mark.parametrize("confirm", [False, True])
async def test_the_task_path_keeps_its_signature(confirm: bool) -> None:
    """The portal's delete task calls this positionally and must keep working; the
    confirmation is keyword-only so it can never be passed by accident."""
    project = _project()
    manager, _ = _wire(project)

    result = await manager.remove_attachment(CERT, confirm_in_use=confirm)

    assert result["success"]
