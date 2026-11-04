"""The quota of a registry's proxy organization: per registry, approved above a threshold."""

from __future__ import annotations

import os
from typing import Any

import pytest
import yaml
from opi.generation.manifests import ManifestGenerator
from opi.services.approvals import apply_approval_verdicts, collect_approval_items
from opi.services.catalog.base import ConfigLayer, ProjectManifestContext
from opi.services.catalog.image_registries import ImageRegistriesService
from opi.services.catalog.image_registries.config_model import QUOTA_FREE_MAX_GIB, QUOTA_MAX_GIB, RegistryEntry
from opi.services.catalog.image_registries.quota import QUOTA_APPROVAL, effective_quota_gib
from opi.services.services import ServiceAdapter
from pydantic import ValidationError

CLUSTER_DEFAULT = 3


def _registry(name: str, quota: int | None = None) -> dict[str, Any]:
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


def _quota(project: dict[str, Any], name: str = "nldd") -> int:
    return effective_quota_gib(project, name, CLUSTER_DEFAULT)


class TestModel:
    def test_accepts_a_quota_within_bounds(self) -> None:
        assert RegistryEntry.model_validate(_registry("nldd", QUOTA_MAX_GIB)).quota_gib == QUOTA_MAX_GIB

    @pytest.mark.parametrize("quota", [0, QUOTA_MAX_GIB + 1])
    def test_refuses_a_quota_out_of_bounds(self, quota: int) -> None:
        with pytest.raises(ValidationError):
            RegistryEntry.model_validate(_registry("nldd", quota))

    def test_without_the_field_it_is_unset(self) -> None:
        assert RegistryEntry.model_validate(_registry("nldd")).quota_gib is None


class TestEffectiveQuota:
    def test_without_a_quota_the_cluster_default_applies(self) -> None:
        assert _quota(_project(_registry("nldd"))) == CLUSTER_DEFAULT

    def test_up_to_the_free_range_needs_no_approval(self) -> None:
        project = _project(_registry("nldd", QUOTA_FREE_MAX_GIB))
        QUOTA_APPROVAL.ensure_requested(project)
        assert _quota(project) == QUOTA_FREE_MAX_GIB
        assert "quota-gib-approval" not in _config(project)

    def test_above_the_free_range_is_a_request_and_holds_at_the_free_range(self) -> None:
        project = _project(_registry("nldd", 10))
        QUOTA_APPROVAL.ensure_requested(project)
        assert _config(project)["quota-gib-approval"]["nldd"]["status"] == "requested"
        assert _quota(project) == QUOTA_FREE_MAX_GIB

    def test_an_approved_request_applies(self) -> None:
        project = _project(_registry("nldd", 10))
        QUOTA_APPROVAL.ensure_requested(project)
        _decide(project, "approved")
        assert _quota(project) == 10

    def test_a_denied_request_holds_at_the_free_range(self) -> None:
        project = _project(_registry("nldd", 10))
        QUOTA_APPROVAL.ensure_requested(project)
        _decide(project, "denied")
        assert _quota(project) == QUOTA_FREE_MAX_GIB

    def test_lowering_within_an_approved_value_needs_no_new_request(self) -> None:
        project = _project(_registry("nldd", 10))
        QUOTA_APPROVAL.ensure_requested(project)
        _decide(project, "approved")
        _config(project)["registries"][0]["quota-gib"] = 8
        QUOTA_APPROVAL.ensure_requested(project)
        assert _quota(project) == 8
        item = next(i for i in collect_approval_items(project) if i["type"] == "quota-gib")
        assert item["current_status"] == "approved"

    def test_raising_above_an_approved_value_is_a_new_request_that_keeps_the_old_one(self) -> None:
        project = _project(_registry("nldd", 10))
        QUOTA_APPROVAL.ensure_requested(project)
        _decide(project, "approved")
        _config(project)["registries"][0]["quota-gib"] = 15
        QUOTA_APPROVAL.ensure_requested(project)
        assert _config(project)["quota-gib-approval"]["nldd"]["status"] == "requested"
        assert _quota(project) == 10


class TestPerRegistry:
    def test_each_registry_has_its_own_request(self) -> None:
        project = _project(_registry("nldd", 10), _registry("other", 12))
        QUOTA_APPROVAL.ensure_requested(project)
        items = [i for i in collect_approval_items(project) if i["type"] == "quota-gib"]
        assert sorted((i["name"], i["domain"]) for i in items) == [("demo/nldd", "10"), ("demo/other", "12")]

    def test_a_verdict_lands_on_its_own_registry_only(self) -> None:
        project = _project(_registry("nldd", 10), _registry("other", 12))
        QUOTA_APPROVAL.ensure_requested(project)
        items = [i for i in collect_approval_items(project) if i["type"] == "quota-gib"]
        for item in items:
            item["status"] = "approved" if item["name"] == "demo/nldd" else "skip"
        apply_approval_verdicts(project, items, admin_email="admin@test.nl")
        assert _quota(project, "nldd") == 10
        assert _quota(project, "other") == QUOTA_FREE_MAX_GIB

    def test_the_block_of_a_removed_registry_is_cleaned_up(self) -> None:
        project = _project(_registry("nldd", 10), _registry("other", 12))
        QUOTA_APPROVAL.ensure_requested(project)
        _decide(project, "approved")
        del _config(project)["registries"][1]
        QUOTA_APPROVAL.ensure_requested(project)
        assert list(_config(project)["quota-gib-approval"]) == ["nldd"]


class TestApprovalIsPlatformManaged:
    def test_a_project_cannot_write_its_own_approval(self) -> None:
        project = _project(_registry("nldd", 10))
        forged = {
            "registries": [_registry("nldd", 10)],
            "quota-gib-approval": {"nldd": {"status": "approved", "value": "10", "approved-value": "10"}},
        }
        ServiceAdapter.set_service_config(project, "image-registries", ConfigLayer.PROJECT, forged)
        assert "quota-gib-approval" not in _config(project)
        assert _quota(project) == QUOTA_FREE_MAX_GIB


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
        project = _project(_registry("nldd", 10))
        QUOTA_APPROVAL.ensure_requested(project)
        _decide(project, "approved")
        assert self._render(tmp_path, project)["spec"]["quota"]["limitGiB"] == 10

    def test_without_a_quota_the_limit_is_the_cluster_default(self, tmp_path: Any) -> None:
        assert self._render(tmp_path, _project(_registry("nldd")))["spec"]["quota"]["limitGiB"] == CLUSTER_DEFAULT

    def test_the_admin_team_can_manage_the_organization(self, tmp_path: Any) -> None:
        levels = self._render(tmp_path, _project(_registry("nldd")))["spec"]["authorization"]["teamSync"]["authLevels"]
        assert {level["name"]: level["role"] for level in levels} == {
            "admin": "Admin",
            "developer": "Member",
            "viewer": "Member",
        }
