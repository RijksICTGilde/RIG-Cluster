"""The quota of a registry's proxy organization: per registry, approved above a threshold."""

from __future__ import annotations

import copy
import os
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml
from fastapi.responses import HTMLResponse
from opi.forms.editables.processor import EditableFormProcessor
from opi.generation.manifests import ManifestGenerator
from opi.services.approvals import apply_approval_verdicts, collect_approval_items
from opi.services.catalog.base import ConfigLayer, ProjectManifestContext
from opi.services.catalog.image_registries import ImageRegistriesService
from opi.services.catalog.image_registries.config_model import (
    QUOTA_FREE_MAX_GIB,
    QUOTA_MAX_GIB,
    ImageRegistriesConfig,
    RegistryEntry,
)
from opi.services.catalog.image_registries.quota import QUOTA_APPROVAL, effective_quota_gib
from opi.services.catalog.image_registries.visualizers import REGISTRIES_SEQUENCE
from opi.services.config_lists import patchable_lists
from opi.services.project_utils import deployments_touched
from opi.services.project_validation import (
    ProjectIntegrityError,
    validate_approval_decisions,
    validate_service_configs,
)
from opi.services.services import ServiceAdapter
from pydantic import ValidationError

CLUSTER_DEFAULT = 10

#: Since schema version 2.18 the token field only takes an encrypted form.
ENCRYPTED_TOKEN = "base64+age:dG9rZW4="


def _registry(name: str, quota: int | None = None) -> dict[str, Any]:
    # A readable token: the render tests below DECRYPT it, and the question here is the
    # quota. ``TestModel`` judges the stored form and sets it itself.
    registry: dict[str, Any] = {"name": name, "upstream": f"ghcr.io/{name}", "password": "plain:token"}
    if quota is not None:
        registry["quota-gib"] = quota
    return registry


def _project(*registries: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": "demo",
        "services": [{"name": "image-registries", "config": {"registries": list(registries)}}],
    }


def _config(project: dict[str, Any]) -> dict[str, Any]:
    return project["services"][0]["config"]


def _decide(project: dict[str, Any], status: str) -> None:
    items = [i for i in collect_approval_items(project) if i["type"] == "quota-gib"]
    for item in items:
        item["status"] = status
    apply_approval_verdicts(project, items, admin_email="admin@test.nl")


def _back_to_the_approved_value() -> dict[str, Any]:
    """A project whose block holds nothing but the last approved value.

    The legitimate state: 15 approved, then 20 requested, then back to 15. The open
    request for 20 drops and only what was granted stays behind.
    """
    project = _project(_registry("nldd", 15))
    QUOTA_APPROVAL.ensure_requested(project)
    _decide(project, "approved")
    _config(project)["registries"][0]["quota-gib"] = QUOTA_MAX_GIB
    QUOTA_APPROVAL.ensure_requested(project)
    _config(project)["registries"][0]["quota-gib"] = 15
    QUOTA_APPROVAL.ensure_requested(project)
    return project


def _quota(project: dict[str, Any], name: str = "nldd") -> int:
    return effective_quota_gib(project, name, CLUSTER_DEFAULT)


class TestModel:
    @staticmethod
    def _entry(name: str, quota: int | None = None) -> dict[str, Any]:
        """The stored shape: a token in a project file is encrypted (schema version 2.18)."""
        return {**_registry(name, quota), "password": ENCRYPTED_TOKEN}

    def test_accepts_a_quota_within_bounds(self) -> None:
        assert RegistryEntry.model_validate(self._entry("nldd", QUOTA_MAX_GIB)).quota_gib == QUOTA_MAX_GIB

    @pytest.mark.parametrize("quota", [0, QUOTA_MAX_GIB + 1])
    def test_refuses_a_quota_out_of_bounds(self, quota: int) -> None:
        with pytest.raises(ValidationError):
            RegistryEntry.model_validate(self._entry("nldd", quota))

    def test_without_the_field_it_is_unset(self) -> None:
        assert RegistryEntry.model_validate(self._entry("nldd")).quota_gib is None


class TestEffectiveQuota:
    def test_without_a_quota_the_cluster_default_applies(self) -> None:
        assert _quota(_project(_registry("nldd"))) == CLUSTER_DEFAULT

    def test_up_to_the_free_range_needs_no_approval(self) -> None:
        project = _project(_registry("nldd", QUOTA_FREE_MAX_GIB))
        QUOTA_APPROVAL.ensure_requested(project)
        assert _quota(project) == QUOTA_FREE_MAX_GIB
        assert "quota-gib-approval" not in _config(project)

    def test_above_the_free_range_is_a_request_and_holds_at_the_free_range(self) -> None:
        project = _project(_registry("nldd", 15))
        QUOTA_APPROVAL.ensure_requested(project)
        assert _config(project)["quota-gib-approval"]["nldd"]["status"] == "requested"
        assert _quota(project) == QUOTA_FREE_MAX_GIB

    def test_an_approved_request_applies(self) -> None:
        project = _project(_registry("nldd", 15))
        QUOTA_APPROVAL.ensure_requested(project)
        _decide(project, "approved")
        assert _quota(project) == 15

    def test_a_denied_request_holds_at_the_free_range(self) -> None:
        project = _project(_registry("nldd", 15))
        QUOTA_APPROVAL.ensure_requested(project)
        _decide(project, "denied")
        assert _quota(project) == QUOTA_FREE_MAX_GIB

    def test_lowering_within_an_approved_value_needs_no_new_request(self) -> None:
        project = _project(_registry("nldd", 15))
        QUOTA_APPROVAL.ensure_requested(project)
        _decide(project, "approved")
        _config(project)["registries"][0]["quota-gib"] = 12
        QUOTA_APPROVAL.ensure_requested(project)
        assert _quota(project) == 12
        item = next(i for i in collect_approval_items(project) if i["type"] == "quota-gib")
        assert item["current_status"] == "approved"

    def test_raising_above_an_approved_value_is_a_new_request_that_keeps_the_old_one(self) -> None:
        project = _project(_registry("nldd", 15))
        QUOTA_APPROVAL.ensure_requested(project)
        _decide(project, "approved")
        _config(project)["registries"][0]["quota-gib"] = 19
        QUOTA_APPROVAL.ensure_requested(project)
        assert _config(project)["quota-gib-approval"]["nldd"]["status"] == "requested"
        assert _quota(project) == 15


class TestPerRegistry:
    def test_each_registry_has_its_own_request(self) -> None:
        project = _project(_registry("nldd", 15), _registry("other", 18))
        QUOTA_APPROVAL.ensure_requested(project)
        items = [i for i in collect_approval_items(project) if i["type"] == "quota-gib"]
        assert sorted((i["name"], i["domain"]) for i in items) == [("demo/nldd", "15"), ("demo/other", "18")]

    def test_a_verdict_lands_on_its_own_registry_only(self) -> None:
        project = _project(_registry("nldd", 15), _registry("other", 18))
        QUOTA_APPROVAL.ensure_requested(project)
        items = [i for i in collect_approval_items(project) if i["type"] == "quota-gib"]
        for item in items:
            item["status"] = "approved" if item["name"] == "demo/nldd" else "skip"
        apply_approval_verdicts(project, items, admin_email="admin@test.nl")
        assert _quota(project, "nldd") == 15
        assert _quota(project, "other") == QUOTA_FREE_MAX_GIB

    def test_a_registry_without_a_name_leaves_the_others_alone(self) -> None:
        """A block is keyed by name, so a registry the resolver skips can orphan nothing."""
        project = _project(_registry("nldd", 15))
        QUOTA_APPROVAL.ensure_requested(project)
        _decide(project, "approved")
        _config(project)["registries"].append({"display-name": "Geen slug", "upstream": "ghcr.io/x"})
        QUOTA_APPROVAL.ensure_requested(project)
        assert _quota(project) == 15

    def test_the_block_of_a_removed_registry_is_cleaned_up(self) -> None:
        project = _project(_registry("nldd", 15), _registry("other", 18))
        QUOTA_APPROVAL.ensure_requested(project)
        _decide(project, "approved")
        del _config(project)["registries"][1]
        QUOTA_APPROVAL.ensure_requested(project)
        assert list(_config(project)["quota-gib-approval"]) == ["nldd"]


#: The block a project would write to grant itself a quota it never asked for.
FORGED_BLOCK = {"nldd": {"status": "approved", "value": "15", "approved-value": "15"}}


class TestApprovalIsPlatformManaged:
    """One test per write path, because a hole in any one of them is the whole gate."""

    def test_the_api_cannot_write_its_own_approval(self) -> None:
        project = _project(_registry("nldd", 15))
        forged = {"registries": [_registry("nldd", 15)], "quota-gib-approval": FORGED_BLOCK}
        ServiceAdapter.set_service_config(project, "image-registries", ConfigLayer.PROJECT, forged)
        assert "quota-gib-approval" not in _config(project)
        assert _quota(project) == QUOTA_FREE_MAX_GIB

    def test_the_form_path_cannot_write_its_own_approval(self) -> None:
        stored = _project(_registry("nldd", 15))
        submitted = _project(_registry("nldd", 15))
        _config(submitted)["quota-gib-approval"] = FORGED_BLOCK
        ServiceAdapter.enforce_platform_managed_fields(submitted, stored)
        assert "quota-gib-approval" not in _config(submitted)
        assert _quota(submitted) == QUOTA_FREE_MAX_GIB

    def test_the_form_path_keeps_the_approval_the_platform_wrote(self) -> None:
        stored = _project(_registry("nldd", 15))
        QUOTA_APPROVAL.ensure_requested(stored)
        _decide(stored, "approved")
        submitted = _project(_registry("nldd", 15))
        ServiceAdapter.enforce_platform_managed_fields(submitted, stored)
        assert _quota(submitted) == 15

    def test_a_registry_entry_cannot_carry_an_approval(self) -> None:
        """The block lives beside the registries; inside one the model refuses it."""
        project = _project({**_registry("nldd", 15), "quota-gib-approval": FORGED_BLOCK})
        with pytest.raises(ProjectIntegrityError):
            validate_service_configs(project)

    def test_a_hand_edited_file_cannot_claim_an_approval(self) -> None:
        """The decision guard on every save: an approval appears only via the approval route."""
        stored = _project(_registry("nldd", 15))
        QUOTA_APPROVAL.ensure_requested(stored)
        forged = _project(_registry("nldd", 15))
        _config(forged)["quota-gib-approval"] = FORGED_BLOCK
        with pytest.raises(ProjectIntegrityError, match="Registry-quota"):
            validate_approval_decisions(stored, forged)

    def test_a_hand_edited_file_cannot_claim_an_approved_value_only(self) -> None:
        """A block with nothing but ``approved-value`` counts as approved but has no item,
        so the status guard never sees it. The value itself is what may not appear."""
        stored = _project(_registry("nldd", 15))
        QUOTA_APPROVAL.ensure_requested(stored)
        forged = _project(_registry("nldd", 15))
        _config(forged)["quota-gib-approval"] = {"nldd": {"approved-value": "15", "history": []}}
        with pytest.raises(ProjectIntegrityError, match="goedgekeurde waarde"):
            validate_approval_decisions(stored, forged)
        assert _quota(forged) == 15, "the forged block grants the quota, hence the guard"

    def test_a_hand_edited_file_cannot_raise_an_approved_value_only(self) -> None:
        stored = _back_to_the_approved_value()
        forged = yaml.safe_load(yaml.safe_dump(stored))
        _config(forged)["quota-gib-approval"]["nldd"]["approved-value"] = str(QUOTA_MAX_GIB)
        with pytest.raises(ProjectIntegrityError, match="goedgekeurde waarde"):
            validate_approval_decisions(stored, forged)

    def test_going_back_to_the_last_approved_value_stays_allowed(self) -> None:
        """The platform writes an approved-value-only block itself when a registry returns
        to a value that was approved before. That save may not be refused."""
        stored = _project(_registry("nldd", 15))
        QUOTA_APPROVAL.ensure_requested(stored)
        _decide(stored, "approved")
        back = _back_to_the_approved_value()

        assert _config(back)["quota-gib-approval"]["nldd"] == {"approved-value": "15", "history": []}
        validate_approval_decisions(stored, back)
        assert _quota(back) == 15

    def test_the_registries_have_no_patch_route_of_their_own(self) -> None:
        """No ``ITEM_KEYS``, so there is no PATCH onto the list to forge a block through.

        Gets one, then it writes through ``set_service_config`` and the first test covers it.
        """
        assert patchable_lists(ImageRegistriesConfig) == []


class TestTheVerdictReachesTheCluster:
    """The approval route reads its rollout scope off the two states, like every save.

    Since RC-483 (P2) that is ``deployments_touched`` and no longer a scope of its own: the
    approval state of every spec lives under the service config, a top-level key the project
    manifests and every deployment read, so a verdict is project-wide. A domain verdict used
    to narrow to the deployments publishing on it; that narrowing needs a spec that can say
    which deployments its item affects, and there is no such declaration.
    """

    def test_a_quota_verdict_redeploys_the_whole_project(self) -> None:
        """The organization is a PROJECT manifest, and those are only generated in a round
        that does at least one deployment. Scoped to zero it never left the project file."""
        project = _project(_registry("nldd", 15))
        project["deployments"] = [{"name": "prod", "base-domain": "voorbeeld.nl", "subdomain": "app"}]
        QUOTA_APPROVAL.ensure_requested(project)
        before = copy.deepcopy(project)
        _decide(project, "approved")

        assert deployments_touched(before, project) is None

    def test_a_domain_verdict_is_project_wide_as_well(self) -> None:
        project: dict[str, Any] = {
            "name": "demo",
            "deployments": [{"name": "prod", "base-domain": "voorbeeld.nl", "subdomain": "app"}],
            "services": [{"name": "publish-on-web", "config": {"domains": {}}}],
        }
        before = copy.deepcopy(project)
        project["services"][0]["config"]["domains"] = {"voorbeeld.nl": {"status": "approved"}}

        assert deployments_touched(before, project) is None

    def test_an_undecided_item_scopes_nothing(self) -> None:
        """Nothing was written, so no deployment has anything to do."""
        project = _project(_registry("nldd", 15))
        QUOTA_APPROVAL.ensure_requested(project)

        assert deployments_touched(copy.deepcopy(project), project) == []


class TestTheFormWritesTheQuota:
    """RC-497: the field is on the form, so a consumer can set the quota without the API."""

    @staticmethod
    async def _submit(row: dict[str, Any], *, stored: int | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
        project = _project(_registry("nldd", stored))
        submitted = {"services": {"image-registries": {"config": {"registries": [row]}}}}
        result, errors = await EditableFormProcessor().process_json_submission(
            submitted, [REGISTRIES_SEQUENCE], project, edit_mode=True
        )
        return result, errors

    @staticmethod
    def _row(**extra: Any) -> dict[str, Any]:
        return {"name": "nldd", "upstream": "ghcr.io/other", "username": "u", **extra}

    async def test_a_submitted_quota_lands_as_an_integer(self) -> None:
        result, errors = await self._submit(self._row(**{"quota-gib": "15"}))
        assert errors == {}
        (registry,) = _config(result)["registries"]
        assert registry["quota-gib"] == 15
        assert registry["upstream"] == "ghcr.io/other"

    @pytest.mark.parametrize("quota", ["0", str(QUOTA_MAX_GIB + 5)])
    async def test_a_quota_out_of_bounds_is_a_field_error(self, quota: str) -> None:
        _result, errors = await self._submit(self._row(**{"quota-gib": quota}))
        assert list(errors) == ["services/image-registries/config/registries[0]/quota-gib"]

    async def test_an_empty_quota_falls_back_to_the_cluster_default(self) -> None:
        """The field is on screen now, so clearing it means "the standard", not "unchanged"."""
        result, errors = await self._submit(self._row(**{"quota-gib": ""}), stored=15)
        assert errors == {}
        (registry,) = _config(result)["registries"]
        assert "quota-gib" not in registry
        assert _quota(result) == CLUSTER_DEFAULT

    async def test_a_row_without_the_field_drops_the_quota_as_well(self) -> None:
        """Measured, and the same outcome as an empty field: ``remove_when_none`` on a
        sequence child does not tell an absent key from a cleared one, and the form always
        submits the field it shows."""
        result, errors = await self._submit(self._row(), stored=15)
        assert errors == {}
        (registry,) = _config(result)["registries"]
        assert "quota-gib" not in registry

    async def test_the_approval_the_platform_wrote_survives_the_form(self) -> None:
        """The form gets no editable on ``quota-gib-approval``, so saving may not touch it."""
        project = _project(_registry("nldd", 15))
        QUOTA_APPROVAL.ensure_requested(project)
        _decide(project, "approved")
        submitted = {"services": {"image-registries": {"config": {"registries": [self._row(**{"quota-gib": "15"})]}}}}
        result, errors = await EditableFormProcessor().process_json_submission(
            submitted, [REGISTRIES_SEQUENCE], project, edit_mode=True
        )
        assert errors == {}
        assert _config(result)["quota-gib-approval"]["nldd"]["status"] == "approved"
        assert _quota(result) == 15


class TestTheModalPathRecordsTheRequest:
    """The request has to arise on the form path too, not just through the API.

    ``modal-edit-image-registries-config`` saves through ``update_project`` with
    ``ALL_GUARDS - {HIDDEN_MEMBERS}``, so ``EditGuard.APPROVAL_REQUESTS`` and with it
    ``ensure_approval_requests`` is the step between the merge and the store. This holds
    that wiring: without it a quota set in the dialog never reaches the approval page.
    """

    @staticmethod
    async def _modal_save(project: dict[str, Any]) -> dict[str, Any]:
        from opi.web.router_detail_edit import _process_and_save_modal_edit

        flow = MagicMock()
        flow.flow_id = "modal-edit-image-registries-config"
        manager = MagicMock()
        manager.get_contents = AsyncMock(return_value=copy.deepcopy(project))
        manager.save_and_commit_project = AsyncMock()

        with (
            patch("opi.web.router_detail_edit.apply_modal_edit", new=AsyncMock(return_value=copy.deepcopy(project))),
            patch("opi.web.router_detail_edit.extract_attachment_catalog", return_value={}),
        ):
            result = await _process_and_save_modal_edit(
                request=MagicMock(),
                project_manager=manager,
                project_name="demo",
                flow=flow,
                wizard_token="token",
                merged_data={},
                active_sections=[],
                state=MagicMock(),
            )
        assert not isinstance(result, HTMLResponse)
        manager.save_and_commit_project.assert_awaited_once()
        return result.after

    async def test_a_quota_above_the_free_range_lands_on_the_approval_page(self) -> None:
        saved = await self._modal_save(_project(_registry("nldd", 15)))

        items = [item for item in collect_approval_items(saved) if item["type"] == "quota-gib"]
        assert len(items) == 1
        assert items[0]["current_status"] == "requested"
        assert _quota(saved) == QUOTA_FREE_MAX_GIB

    async def test_a_quota_within_the_free_range_asks_nothing(self) -> None:
        saved = await self._modal_save(_project(_registry("nldd", 8)))

        assert [item for item in collect_approval_items(saved) if item["type"] == "quota-gib"] == []
        assert _quota(saved) == 8


class TestRenderedOrganization:
    def _render(self, tmp_path: Any, project: dict[str, Any]) -> dict[str, Any]:
        ctx = ProjectManifestContext(
            project_name="demo", project_data=project, cluster="odcn-production", namespace="rig-prd-demo"
        )
        spec = ImageRegistriesService().contribute_project_manifests(ctx)[1]
        template_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "manifests")
        path = ManifestGenerator().create_manifest_file(
            template_path=os.path.join(template_dir, spec.template_path),
            values=spec.values,
            output_dir=str(tmp_path),
            output_filename=spec.filename,
        )
        with open(path) as f:
            return yaml.safe_load(f)

    def test_limit_comes_from_the_registry(self, tmp_path: Any) -> None:
        project = _project(_registry("nldd", 15))
        QUOTA_APPROVAL.ensure_requested(project)
        _decide(project, "approved")
        assert self._render(tmp_path, project)["spec"]["quota"]["limitGiB"] == 15

    def test_without_a_quota_the_limit_is_the_cluster_default(self, tmp_path: Any) -> None:
        assert self._render(tmp_path, _project(_registry("nldd")))["spec"]["quota"]["limitGiB"] == CLUSTER_DEFAULT

    def test_the_time_machine_is_off(self, tmp_path: Any) -> None:
        """Omitted, Quay keeps its own window and an expired tag counts towards the quota
        for weeks. A proxy cache simply fetches the image again."""
        assert self._render(tmp_path, _project(_registry("nldd")))["spec"]["timeMachineSeconds"] == 0

    def test_the_tag_expiration_stays_on_the_quay_default(self, tmp_path: Any) -> None:
        """Setting it explicitly rebuilds the proxy cache config of every organization, and
        the default (one day) is what we want anyway."""
        assert (
            "defaultTagExpirationSeconds"
            not in self._render(tmp_path, _project(_registry("nldd")))["spec"]["proxyCache"]
        )

    def test_the_admin_team_can_manage_the_organization(self, tmp_path: Any) -> None:
        levels = self._render(tmp_path, _project(_registry("nldd")))["spec"]["authorization"]["teamSync"]["authLevels"]
        assert {level["name"]: level["role"] for level in levels} == {
            "admin": "Admin",
            "developer": "Member",
            "viewer": "Member",
        }
