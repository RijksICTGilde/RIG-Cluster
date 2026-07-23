"""ServiceProvider base class (RC-5 Phase 1).

The RC-5 migration ("Uniform, Declarative Platform Services") replaces the ~14
hand-maintained per-service edit sites with a single declarative unit per service:
a ``ServiceProvider`` subclass, registered once in
``opi.services.registry.SERVICE_PROVIDERS``. Generic code then iterates the registry
instead of hand-synced wizard/flow/provisioning/cleanup/manifest lists.

This module is intentionally dependency-light -- it imports only the service
metadata (``ServiceDefinition``) and the ``ServiceType`` enum, never forms,
managers or connectors. That keeps the provider protocol free of the circular
imports the plan warns about (forms reference providers; providers must not, at
import time, reference forms or managers). Behaviour hooks (config shape,
provisioning, cleanup, manifest contribution) are added to this base class in later
phases, when generic code actually consumes them, so their context types can be
imported lazily / under ``TYPE_CHECKING`` at that point.

Phase 1 is metadata-only: each provider carries its existing ``ServiceDefinition``
(unchanged) and nothing consumes providers yet beyond the coverage guard in
``tests/test_service_providers.py``.
"""

from __future__ import annotations

from abc import ABC
from typing import TYPE_CHECKING, ClassVar

from opi.services.services import ServiceAdapter, ServiceDefinition

if TYPE_CHECKING:
    from opi.services.services_enums import ServiceType


class ServiceProvider(ABC):
    """One subclass per ``ServiceType``; the single declarative home for a service.

    A subclass sets ``service_type``; the matching ``ServiceDefinition`` is bound
    automatically from ``ServiceAdapter.SERVICE_DEFINITIONS`` (see
    ``__init_subclass__``) so the definition can never drift from the provider.
    ``SERVICE_DEFINITIONS`` remains the metadata source of truth during the
    migration -- the provider composes with it, it does not replace it.
    """

    #: The service this provider handles. Set by each concrete subclass.
    service_type: ClassVar[ServiceType]
    #: The existing metadata dataclass, bound automatically from service_type.
    definition: ClassVar[ServiceDefinition]

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        # A concrete provider must declare which service it is; the definition is
        # then derived, not duplicated. Abstract intermediate subclasses (no
        # service_type) are allowed and simply skipped.
        service_type = cls.__dict__.get("service_type")
        if service_type is not None:
            cls.definition = ServiceAdapter.SERVICE_DEFINITIONS[service_type]
