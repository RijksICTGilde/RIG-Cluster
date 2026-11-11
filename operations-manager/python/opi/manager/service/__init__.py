"""The use cases for changing what a service holds on a project.

``configure_service`` writes a service's config block; ``set_component_values`` writes the
key/value properties a service owns on a component. Every entrance, HTTP or task, goes
through one of these two.
"""

from opi.manager.service.configure import (
    ConfigOperation,
    ServiceConfigChange,
    ServiceConfigResult,
    configure_service,
)
from opi.manager.service.errors import (
    ChangeConflictError,
    ChangeValidationError,
    InvalidProjectNameError,
    InvalidTargetError,
    InvalidValuesError,
    ServiceChangeError,
)
from opi.manager.service.progress import NullProgress, ProgressReporter
from opi.manager.service.rollout import RolloutRequest, roll_out
from opi.manager.service.values import (
    ComponentValuesChange,
    ComponentValuesResult,
    set_component_values,
)

__all__ = [
    "ChangeConflictError",
    "ChangeValidationError",
    "ComponentValuesChange",
    "ComponentValuesResult",
    "ConfigOperation",
    "InvalidProjectNameError",
    "InvalidTargetError",
    "InvalidValuesError",
    "NullProgress",
    "ProgressReporter",
    "RolloutRequest",
    "ServiceChangeError",
    "ServiceConfigChange",
    "ServiceConfigResult",
    "configure_service",
    "roll_out",
    "set_component_values",
]
