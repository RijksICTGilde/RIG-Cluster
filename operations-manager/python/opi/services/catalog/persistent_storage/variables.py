"""Environment variables the persistent-storage service provides.

Lives in the service's own package (RC-36). The mount path is handed to the pod
directly (no secret), so a component knows where its permanent data lives.
"""

from enum import Enum

from opi.services.services import VariableDefinition


class PersistentStorageVariables(Enum):
    """Permanent storage variable definitions - single source of truth."""

    DATA_PATH = VariableDefinition(
        name="DATA_PATH", description="Mount pad voor permanente data opslag (/data)", source="direct"
    )
