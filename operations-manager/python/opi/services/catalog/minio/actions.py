"""De deploymentknoppen van minio-storage: de grens op oude versies per bucket (RC-380)."""

from __future__ import annotations

from typing import Any

from opi.services.services import DeploymentAction, deployment_modal_action
from opi.services.services_enums import ServiceType


def minio_storage_actions(project_data: dict[str, Any], deployment_name: str) -> list[DeploymentAction]:
    # De knop staat er zodra het project de dienst gebruikt. Of de bucket echt versioning
    # heeft is hier niet te weten: het kan ook buiten OPI om aanstaan, en dat vraagt MinIO
    # zelf, wat een synchrone knoppenlijst niet kan. De helptekst zegt wat de grens doet.
    action = deployment_modal_action(
        project_data,
        deployment_name,
        service=ServiceType.MINIO_STORAGE,
        modal_prefix="modal-edit-minio-deployment-",
        label="Oude versies",
        icon="wolk",
    )
    return [action] if action else []
