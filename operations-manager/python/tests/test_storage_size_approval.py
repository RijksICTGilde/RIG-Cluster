"""A volume above 2Gi needs approval, and a deployment may deviate from its component.

What this file pins, in three layers:

1. The rule itself (``catalog/shared/storage_size.py``): up to and including 2Gi the project
   picks, above it a platform administrator decides, and until then the volume renders on what
   it was already allowed. Sizes are compared as quantities, so ``5Gi`` and ``5120Mi`` are one
   request.
2. The two layers: the component sets the size for all its deployments, a deployment may
   deviate per volume with a decision of its own, and an approved deviation keeps standing
   when the component's own size goes down later.
3. The guards around it: the block is platform-managed, an ``approved`` that did not come from
   the approval route is refused, and a persistent volume cannot shrink.
"""

from __future__ import annotations

import asyncio
import copy
from typing import Any

import pytest
from opi.services.approvals import apply_approval_verdicts, collect_approval_items, collect_deployment_approval_notices
from opi.services.catalog.base import ConfigLayer
from opi.services.catalog.shared.storage_size import (
    COMPONENT_SIZE_APPROVALS,
    OVERRIDE_SIZE_APPROVALS,
    RESTORE_UNKNOWN_VOLUME_SIZE,
    effective_component_size,
    effective_mount_size,
    effective_persistent_size_by_name,
    mount_key,
    mount_key_for_name,
    refused_shrinks,
    resolve_component_storage,
    unknown_override_references,
)
from opi.services.project_validation import (
    ProjectIntegrityError,
    validate_approval_decisions,
    validate_project_structure,
)
from opi.services.registry import get_service
from opi.services.services import ServiceAdapter
from opi.services.services_enums import ServiceType

PERSISTENT = ServiceType.PERSISTENT_STORAGE
TEMP = ServiceType.TEMP_STORAGE

#: The volume nearly every test measures: component ``loep``, deployment ``meten``.
DATA = ("meten", "loep", "data")


def _project(size: str = "1Gi", *, temp: str | None = None) -> dict[str, Any]:
    services: list[Any] = [
        {"reference": "persistent-storage", "config": [{"name": "data", "size": size, "mount-path": "/data"}]}
    ]
    if temp is not None:
        services.append({"reference": "temp-storage", "config": [{"name": "tmp", "size": temp, "mount-path": "/tmp"}]})
    return {
        "name": "demo",
        "services": ["persistent-storage", "temp-storage"],
        "components": [{"name": "loep", "type": "single", "services": services}],
        "deployments": [{"name": "meten", "cluster": "local", "components": [{"reference": "loep"}]}],
    }


def _requests(project: dict[str, Any]) -> None:
    """Record what the project asks for and has not been granted as an open request."""
    for service in (PERSISTENT, TEMP):
        get_service(service).ensure_approval_requests(project)


def _pending(project: dict[str, Any]) -> list[str]:
    """The sizes still waiting for a decision.

    A DECIDED block stays on the approvals page, so the question is never whether an item
    exists but whether anything is open.
    """
    return [item["domain"] for item in collect_approval_items(project) if item["current_status"] == "requested"]


def _decide(project: dict[str, Any], status: str, *, spec_type: str | None = None) -> None:
    items = [item for item in collect_approval_items(project) if spec_type is None or item["type"] == spec_type]
    assert items, "nothing was open to decide on"
    for item in items:
        item["status"] = status
    apply_approval_verdicts(project, items, admin_email="admin@test.nl")


def _set_override(project: dict[str, Any], size: str | None, *, service: ServiceType = PERSISTENT) -> None:
    ServiceAdapter.set_service_config(
        project,
        service.value,
        ConfigLayer.DEPLOYMENT_COMPONENT,
        [{"reference": "data" if service is PERSISTENT else "tmp", "config": {} if size is None else {"size": size}}],
        deployment_name="meten",
        component_name="loep",
    )


class TestTheKeyOfAVolume:
    def test_the_key_is_the_one_the_render_path_uses(self) -> None:
        """The override, its approval and the PVC name must name the same volume."""
        assert mount_key({"mount-path": "/app/logs"}, 1) == "applogs"
        assert mount_key({"mount-path": ""}, 2) == "storage2"

    def test_backup_and_restore_name_the_same_volume_by_its_name_field(self) -> None:
        """``name`` is the identity backup and restore carry, and it is not the render key.

        A component with ``name: logs`` on ``/var/log`` has them differ, so the translation
        has to happen: without it the size of that volume cannot be found at all.
        """
        project = _project()
        project["components"][0]["services"][0]["config"] = [{"name": "logs", "size": "1Gi", "mount-path": "/var/log"}]
        component = project["components"][0]

        assert mount_key_for_name(component, PERSISTENT, "logs") == "varlog"
        assert mount_key_for_name(component, PERSISTENT, "varlog") is None
        assert effective_persistent_size_by_name(project, "meten", "loep", "logs") == "1Gi"

    def test_a_volume_the_component_lost_keeps_the_size_restore_always_used(self) -> None:
        project = _project()

        assert effective_persistent_size_by_name(project, "meten", "loep", "weg") == RESTORE_UNKNOWN_VOLUME_SIZE

    def test_an_approved_size_reaches_a_restore_of_a_renamed_volume(self) -> None:
        """The failure this guards: a restore asking for a PVC of 100Mi for a volume of 5Gi."""
        project = _project("5Gi")
        project["components"][0]["services"][0]["config"] = [{"name": "logs", "size": "5Gi", "mount-path": "/var/log"}]
        _requests(project)
        _decide(project, "approved", spec_type="size")

        assert effective_persistent_size_by_name(project, "meten", "loep", "logs") == "5Gi"

    def test_a_key_the_component_does_not_mount_is_an_error_not_a_default(self) -> None:
        """A silent 100Mi there is what hid the restore bug; the caller gets to hear it."""
        with pytest.raises(KeyError):
            effective_component_size(_project(), PERSISTENT, "loep", "weg")


class TestUpToTwoGigabytes:
    def test_within_the_free_range_nothing_is_requested(self) -> None:
        project = _project("2Gi")
        _requests(project)
        assert collect_approval_items(project) == []
        assert effective_component_size(project, PERSISTENT, "loep", "data") == "2Gi"

    def test_above_it_a_request_appears(self) -> None:
        project = _project("5Gi")
        _requests(project)
        items = collect_approval_items(project)
        assert [item["domain"] for item in items] == ["5Gi"]
        assert items[0]["current_status"] == "requested"

    def test_an_open_request_never_lowers_what_already_held(self) -> None:
        project = _project("5Gi")
        _requests(project)
        assert effective_component_size(project, PERSISTENT, "loep", "data") == "2Gi"

    def test_after_approval_the_requested_size_applies(self) -> None:
        project = _project("5Gi")
        _requests(project)
        _decide(project, "approved")
        assert effective_component_size(project, PERSISTENT, "loep", "data") == "5Gi"

    def test_a_denial_leaves_the_free_maximum_standing(self) -> None:
        project = _project("5Gi")
        _requests(project)
        _decide(project, "denied")
        assert effective_component_size(project, PERSISTENT, "loep", "data") == "2Gi"

    def test_the_same_quantity_spelled_differently_is_the_same_approval(self) -> None:
        """Comparing text would turn 5120Mi into a second request."""
        project = _project("5Gi")
        _requests(project)
        _decide(project, "approved")
        project["components"][0]["services"][0]["config"][0]["size"] = "5120Mi"
        _requests(project)
        assert _pending(project) == []
        assert effective_component_size(project, PERSISTENT, "loep", "data") == "5120Mi"

    def test_going_back_within_the_approved_size_requests_nothing(self) -> None:
        project = _project("5Gi")
        _requests(project)
        _decide(project, "approved")
        project["components"][0]["services"][0]["config"][0]["size"] = "3Gi"
        _requests(project)
        assert _pending(project) == []
        assert effective_component_size(project, PERSISTENT, "loep", "data") == "3Gi"

    def test_each_of_the_two_services_has_its_own_decision(self) -> None:
        project = _project("5Gi", temp="5Gi")
        _requests(project)
        _decide(project, "approved", spec_type="size")
        assert effective_component_size(project, PERSISTENT, "loep", "data") == "5Gi"
        assert effective_component_size(project, TEMP, "loep", "tmp") == "5Gi"


class TestTheOverridePerDeployment:
    def test_without_an_override_the_deployment_inherits(self) -> None:
        project = _project("5Gi")
        _requests(project)
        _decide(project, "approved")
        assert effective_mount_size(project, PERSISTENT, DATA) == "5Gi"

    def test_an_override_within_what_was_allowed_requests_nothing(self) -> None:
        project = _project("1Gi")
        _set_override(project, "2Gi")
        _requests(project)
        assert _pending(project) == []
        assert effective_mount_size(project, PERSISTENT, DATA) == "2Gi"

    def test_a_smaller_override_requests_nothing(self) -> None:
        project = _project("2Gi")
        _set_override(project, "500Mi")
        _requests(project)
        assert _pending(project) == []
        assert effective_mount_size(project, PERSISTENT, DATA) == "500Mi"

    def test_an_override_above_it_needs_a_decision_of_its_own(self) -> None:
        project = _project("1Gi")
        _set_override(project, "10Gi")
        _requests(project)
        items = collect_approval_items(project)
        assert [item["type"] for item in items] == ["deployment-component-size"]
        assert items[0]["deployment"] == "meten"
        assert effective_mount_size(project, PERSISTENT, DATA) == "2Gi"

    def test_after_approval_the_override_applies(self) -> None:
        project = _project("1Gi")
        _set_override(project, "10Gi")
        _requests(project)
        _decide(project, "approved")
        assert effective_mount_size(project, PERSISTENT, DATA) == "10Gi"

    def test_an_approved_override_stands_when_the_component_goes_down(self) -> None:
        project = _project("5Gi")
        _requests(project)
        _decide(project, "approved")
        _set_override(project, "10Gi")
        _requests(project)
        _decide(project, "approved", spec_type="deployment-component-size")
        project["components"][0]["services"][0]["config"][0]["size"] = "500Mi"
        _requests(project)
        assert effective_mount_size(project, PERSISTENT, DATA) == "10Gi"

    def test_the_override_applies_to_that_deployment_only(self) -> None:
        project = _project("1Gi")
        project["deployments"].append({"name": "acceptatie", "cluster": "local", "components": [{"reference": "loep"}]})
        _set_override(project, "10Gi")
        _requests(project)
        _decide(project, "approved")
        assert effective_mount_size(project, PERSISTENT, DATA) == "10Gi"
        assert effective_mount_size(project, PERSISTENT, ("acceptatie", "loep", "data")) == "1Gi"

    def test_removing_the_override_makes_the_deployment_inherit_again(self) -> None:
        project = _project("1Gi")
        _set_override(project, "2Gi")
        _requests(project)
        _set_override(project, None)
        _requests(project)
        assert effective_mount_size(project, PERSISTENT, DATA) == "1Gi"

    def test_an_override_on_a_volume_that_does_not_exist_is_reported(self) -> None:
        project = _project("1Gi")
        component = project["deployments"][0]["components"][0]
        component["services"] = {"persistent-storage": [{"reference": "bestaat-niet", "config": {"size": "1Gi"}}]}
        assert unknown_override_references(project, PERSISTENT)


class TestTheBlockBelongsToThePlatform:
    def test_a_project_write_cannot_set_it(self) -> None:
        project = _project("5Gi")
        ServiceAdapter.set_service_config(
            project,
            PERSISTENT.value,
            ConfigLayer.PROJECT,
            {"size-approval": {"loep/data": {"status": "approved", "approved-value": "5Gi"}}},
        )
        assert effective_component_size(project, PERSISTENT, "loep", "data") == "2Gi"

    def test_an_override_write_cannot_set_it(self) -> None:
        project = _project("1Gi")
        ServiceAdapter.set_service_config(
            project,
            PERSISTENT.value,
            ConfigLayer.DEPLOYMENT_COMPONENT,
            [
                {
                    "reference": "data",
                    "config": {"size": "10Gi", "size-approval": {"status": "approved", "approved-value": "10Gi"}},
                }
            ],
            deployment_name="meten",
            component_name="loep",
        )
        assert effective_mount_size(project, PERSISTENT, DATA) == "2Gi"

    def test_the_generation_of_a_volume_survives_an_override(self) -> None:
        project = _project("1Gi")
        project["deployments"][0]["components"][0]["services"] = {
            "persistent-storage": [{"reference": "data", "config": {"generation": 3}}]
        }
        _set_override(project, "2Gi")
        records = project["deployments"][0]["components"][0]["services"]["persistent-storage"]
        assert records == [{"reference": "data", "config": {"size": "2Gi", "generation": 3}}]

    def test_an_approved_value_that_skipped_the_route_is_refused(self) -> None:
        before = _project("5Gi")
        _requests(before)
        after = copy.deepcopy(before)
        config = after["services"][0]["config"]["size-approval"]["loep/data"]
        config["status"] = "approved"
        config["approved-value"] = "5Gi"
        with pytest.raises(ProjectIntegrityError, match="alleen van de goedkeuringsroute"):
            validate_approval_decisions(before, after)

    def test_a_vanished_volume_cleans_up_its_block(self) -> None:
        project = _project("5Gi")
        _set_override(project, "10Gi")
        _requests(project)
        assert project["services"][0]["config"]["size-approval"]
        project["components"][0]["services"][0]["config"] = []
        _requests(project)
        assert "size-approval" not in project["services"][0]["config"]
        # The block goes; the override itself is user data and stays, inert, because the
        # resolver only reads volumes the component still has.
        records = project["deployments"][0]["components"][0]["services"]["persistent-storage"]
        assert records == [{"reference": "data", "config": {"size": "10Gi"}}]


class TestWhatTheApplicantSees:
    def test_the_notice_names_what_applies_meanwhile(self) -> None:
        project = _project("5Gi")
        _requests(project)
        notices = collect_deployment_approval_notices(project, project["deployments"][0])
        teksten = [notice["text"] for notice in notices if notice["type"] == "size"]
        assert teksten
        assert "2Gi" in teksten[0]
        assert "5Gi" in teksten[0]

    def test_the_request_names_the_volume_and_what_applies_now(self) -> None:
        project = _project("5Gi")
        _requests(project)
        subject = collect_approval_items(project)[0]["subject"]
        assert "data" in subject
        assert "loep" in subject
        assert "nu 2Gi" in subject

    def test_an_override_request_names_the_deployment(self) -> None:
        project = _project("1Gi")
        _set_override(project, "10Gi")
        _requests(project)
        assert "meten" in collect_approval_items(project)[0]["subject"]


class TestTheResolver:
    def test_the_volume_list_carries_the_effective_size(self) -> None:
        project = _project("5Gi", temp="5Gi")
        _requests(project)
        _decide(project, "approved", spec_type="size")
        project["components"][0]["services"][0]["config"][0]["size"] = "20Gi"
        _requests(project)
        sizes = {entry["name"]: entry["size"] for entry in resolve_component_storage(project, "meten", "loep")}
        # 20Gi is open, so the volume keeps the approved 5Gi; tmp is approved.
        assert sizes == {"data": "5Gi", "tmp": "5Gi"}


class TestShrinking:
    def test_a_smaller_component_size_is_refused(self) -> None:
        before = _project("5Gi")
        _requests(before)
        _decide(before, "approved")
        after = copy.deepcopy(before)
        after["components"][0]["services"][0]["config"][0]["size"] = "1Gi"
        assert refused_shrinks(before, after)

    def test_a_smaller_override_is_refused(self) -> None:
        before = _project("1Gi")
        _set_override(before, "10Gi")
        _requests(before)
        _decide(before, "approved")
        after = copy.deepcopy(before)
        _set_override(after, "5Gi")
        assert refused_shrinks(before, after)

    def test_temp_storage_may_shrink(self) -> None:
        before = _project("1Gi", temp="5Gi")
        _requests(before)
        _decide(before, "approved")
        after = copy.deepcopy(before)
        after["components"][0]["services"][1]["config"][0]["size"] = "1Gi"
        assert refused_shrinks(before, after) == []

    def test_a_new_generation_is_not_a_shrink(self) -> None:
        """A recreate through the storage actions makes a NEW PVC, so it may be smaller."""
        before = _project("5Gi")
        _requests(before)
        _decide(before, "approved")
        _set_override(before, "5Gi")
        after = copy.deepcopy(before)
        records = after["deployments"][0]["components"][0]["services"]["persistent-storage"]
        records[0]["config"]["generation"] = 1
        records[0]["config"]["size"] = "1Gi"
        assert refused_shrinks(before, after) == []

    def test_an_approver_may_take_a_standing_approval_back(self) -> None:
        """Denying a standing approval is the way back, so the shrink it causes passes.

        Measured through the whole chain, because the rule runs on every save and this one
        carries the verdict itself.
        """
        before = _project("10Gi")
        _requests(before)
        _decide(before, "approved")
        after = copy.deepcopy(before)
        _decide(after, "denied")

        assert effective_mount_size(before, PERSISTENT, DATA) == "10Gi"
        assert effective_mount_size(after, PERSISTENT, DATA) == "2Gi"
        assert refused_shrinks(before, after, approval_decision=True) == []
        asyncio.run(validate_project_structure(after, previous=before, approval_decision=True))

    def test_an_approver_may_take_a_standing_override_back(self) -> None:
        """The same on the deployment layer, where the decision is that deployment's own."""
        before = _project("1Gi")
        _set_override(before, "10Gi")
        _requests(before)
        _decide(before, "approved")
        after = copy.deepcopy(before)
        _decide(after, "denied")

        assert effective_mount_size(after, PERSISTENT, DATA) == "2Gi"
        assert refused_shrinks(before, after, approval_decision=True) == []

    def test_the_same_withdrawal_as_a_project_write_is_refused(self) -> None:
        """Only the approval route may lower what a volume is allowed."""
        before = _project("10Gi")
        _requests(before)
        _decide(before, "approved")
        after = copy.deepcopy(before)
        _decide(after, "denied")

        assert refused_shrinks(before, after)

    def test_a_shrink_the_verdict_does_not_explain_stays_refused(self) -> None:
        """A save on the approval route is not a free pass for every other size."""
        before = _project("10Gi")
        _requests(before)
        _decide(before, "approved")
        after = copy.deepcopy(before)
        after["components"][0]["services"][0]["config"][0]["size"] = "5Gi"

        assert refused_shrinks(before, after, approval_decision=True)

    def test_the_service_refuses_the_shrink_through_its_own_rule(self) -> None:
        before = _project("5Gi")
        _requests(before)
        _decide(before, "approved")
        after = copy.deepcopy(before)
        after["components"][0]["services"][0]["config"][0]["size"] = "1Gi"
        assert get_service(PERSISTENT).validate_project_change(before, after)


class TestApprovalPerVolume:
    def test_every_volume_has_its_own_decision(self) -> None:
        project = _project("5Gi")
        project["components"][0]["services"][0]["config"].append(
            {"name": "gesprekken", "size": "10Gi", "mount-path": "/gesprekken"}
        )
        _requests(project)
        items = {item["domain"] for item in collect_approval_items(project)}
        assert items == {"5Gi", "10Gi"}
        _decide(project, "approved")
        assert effective_component_size(project, PERSISTENT, "loep", "data") == "5Gi"
        assert effective_component_size(project, PERSISTENT, "loep", "gesprekken") == "10Gi"

    def test_the_last_approved_size_stays_known(self) -> None:
        project = _project("5Gi")
        _requests(project)
        _decide(project, "approved")
        assert COMPONENT_SIZE_APPROVALS[PERSISTENT].last_approved_value(project, "loep/data") == "5Gi"
        _set_override(project, "10Gi")
        _requests(project)
        _decide(project, "approved", spec_type="deployment-component-size")
        assert OVERRIDE_SIZE_APPROVALS[PERSISTENT].last_approved_value(project, DATA) == "10Gi"
