"""What ``persistent-storage`` and ``temp-storage`` share as a service: the size rule.

Both mount volumes, both let a deployment override the size per volume, and both hang that
size on the same approval. Those hooks are the rule, not the service, so they live here and
each service mixes them in: the same shape as ``BackupsPageMixin`` next door. What stays in
each package is what differs -- the definition, the editables and the visualizers.
"""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING, Any, ClassVar

from opi.services.catalog.base import ConfigLayer, ConfigWrite

if TYPE_CHECKING:
    from pydantic import BaseModel

    from opi.forms.layout import LayoutChild
    from opi.services.catalog.approval import ApprovalSpec
    from opi.services.services_enums import ServiceType


class StorageSizeServiceMixin:
    """The size rule of a storage service: where its config lives and who approves it."""

    service_type: ClassVar[ServiceType]

    def config_model_for(self, layer: ConfigLayer) -> type[BaseModel] | None:
        # Mount specs on the component, per-mount state on the deployment-component, and on
        # the project nothing but the verdicts on the sizes.
        from opi.services.catalog.shared.storage import StorageApprovalConfig, StorageCloneState

        if layer is ConfigLayer.DEPLOYMENT_COMPONENT:
            return StorageCloneState
        if layer is ConfigLayer.PROJECT:
            return StorageApprovalConfig
        return self.config_model  # type: ignore[attr-defined]

    def config_api_body_model(self, layer: ConfigLayer) -> type[BaseModel] | None:
        from opi.services.catalog.shared.storage import StorageSizeOverrides

        if layer is ConfigLayer.DEPLOYMENT_COMPONENT:
            return StorageSizeOverrides
        return self.config_model_for(layer)

    def validate_config_write(self, write: ConfigWrite) -> list[str]:
        """An override has to name a volume the component actually mounts."""
        from opi.services.catalog.shared.storage_size import unknown_override_references
        from opi.services.services import ServiceAdapter

        if write.layer is not ConfigLayer.DEPLOYMENT_COMPONENT:
            return []
        probe = deepcopy(write.project_data)
        ServiceAdapter.set_service_config(
            probe,
            self.service_type.value,
            write.layer,
            write.config,
            component_name=write.component_name,
            deployment_name=write.deployment_name,
        )
        return unknown_override_references(probe, self.service_type)

    def config_approvals(self, layer: ConfigLayer) -> list[ApprovalSpec]:
        from opi.services.catalog.shared.storage_size import COMPONENT_SIZE_APPROVALS, OVERRIDE_SIZE_APPROVALS

        if layer is ConfigLayer.PROJECT:
            return [COMPONENT_SIZE_APPROVALS[self.service_type].spec]
        if layer is ConfigLayer.DEPLOYMENT_COMPONENT:
            return [OVERRIDE_SIZE_APPROVALS[self.service_type].spec]
        return []

    def ensure_approval_requests(self, project_data: dict[str, Any]) -> None:
        from opi.services.catalog.shared.storage_size import COMPONENT_SIZE_APPROVALS, OVERRIDE_SIZE_APPROVALS

        COMPONENT_SIZE_APPROVALS[self.service_type].ensure_requested(project_data)
        OVERRIDE_SIZE_APPROVALS[self.service_type].ensure_requested(project_data)

    def config_deployment_component_layout(self) -> list[LayoutChild]:
        from opi.forms.layout import Sequence

        return [Sequence(field_name=f"services/{self.service_type.value}")]
