"""The deployment buttons of seaweedfs-storage: the limit on noncurrent versions per bucket."""

from __future__ import annotations

from typing import Any

from opi.services.services import DeploymentAction, deployment_modal_action
from opi.services.services_enums import ServiceType


def seaweedfs_storage_actions(project_data: dict[str, Any], deployment_name: str) -> list[DeploymentAction]:
    # The same weighing as with minio: the button is there as soon as the project uses the
    # service, because whether the bucket really has versioning means asking SeaweedFS itself.
    action = deployment_modal_action(
        project_data,
        deployment_name,
        service=ServiceType.SEAWEEDFS_STORAGE,
        modal_prefix="modal-edit-seaweedfs-deployment-",
        label="Oude versies",
        icon="wolk",
    )
    return [action] if action else []
