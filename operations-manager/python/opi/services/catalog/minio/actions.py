"""The deployment buttons of minio-storage: the limit on noncurrent versions per bucket."""

from __future__ import annotations

from typing import Any

from opi.services.services import DeploymentAction, deployment_modal_action
from opi.services.services_enums import ServiceType


def minio_storage_actions(project_data: dict[str, Any], deployment_name: str) -> list[DeploymentAction]:
    # The button is there as soon as the project uses the service. Whether the bucket really
    # has versioning cannot be known here: it may also be on outside OPI, and answering that
    # means asking MinIO, which a synchronous button list cannot. The help text says what the
    # limit does.
    action = deployment_modal_action(
        project_data,
        deployment_name,
        service=ServiceType.MINIO_STORAGE,
        modal_prefix="modal-edit-minio-deployment-",
        label="Versiebeheer voor objectstorage",
        icon="wolk",
    )
    return [action] if action else []
