"""sleep-mode service: scale idle preview deployments to zero, wake on request.

The package is self-contained: this module is the declaration hub (config model +
version), ``config.py`` owns the cluster-wide default and the project merge,
``state.py`` the per-deployment runtime state, and the remaining modules the token,
the state transitions, the waker manifests, the action button, the API router and the
sweeper. Only ``__init__.py`` is imported by the registry; the router and scheduler are
bound explicitly by ``server.py`` (they pull in FastAPI / managers, which the catalog
must stay free of).
"""

from __future__ import annotations

from opi.services.catalog.base import Service
from opi.services.catalog.sleep_mode.config_model import SleepModeConfig
from opi.services.services_enums import ServiceType


class SleepModeService(Service):
    service_type = ServiceType.SLEEP_MODE
    config_model = SleepModeConfig
    config_schema_version = "1.0"


# Bind the wake button onto the bound ServiceDefinition. Done here (not in services.py)
# so services.py never imports the catalog package -- the definition is a mutable
# dataclass, and the registry imports this module at startup, before any request.
from opi.services.catalog.sleep_mode.actions import sleep_actions  # noqa: E402

SleepModeService.definition.actions_provider = sleep_actions
