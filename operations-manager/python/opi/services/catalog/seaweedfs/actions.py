"""De deploymentknoppen van seaweedfs-storage: de grens op oude versies per bucket (RC-380)."""

from __future__ import annotations

from typing import Any

from opi.services.services import DeploymentAction, deployment_modal_action
from opi.services.services_enums import ServiceType


def seaweedfs_storage_actions(project_data: dict[str, Any], deployment_name: str) -> list[DeploymentAction]:
    # Zelfde afweging als bij minio: de knop staat er zodra het project de dienst gebruikt,
    # want of de bucket echt versioning heeft vraagt SeaweedFS zelf.
    action = deployment_modal_action(
        project_data,
        deployment_name,
        service=ServiceType.SEAWEEDFS_STORAGE,
        modal_prefix="modal-edit-seaweedfs-deployment-",
        label="Oude versies",
        icon="wolk",
    )
    return [action] if action else []
