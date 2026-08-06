"""Environment variables the temp-storage service provides.

Lives in the service's own package (RC-36). The mount path is handed to the pod
directly (no secret), so a component knows where its scratch space lives.
"""

from enum import Enum

from opi.services.services import VariableDefinition


class TempStorageVariables(Enum):
    """Temporary storage variable definitions - single source of truth."""

    TEMP_PATH = VariableDefinition(
        name="TEMP_PATH", description="Mount pad voor tijdelijke/tijdelijke opslag (/tmp)", source="direct"
    )
