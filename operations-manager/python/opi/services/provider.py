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
    from pydantic import BaseModel

    from opi.services.services_enums import ServiceType


class ServiceProvider(ABC):
    """One subclass per ``ServiceType``; the single declarative home for a service.

    A subclass sets ``service_type``; the matching ``ServiceDefinition`` is bound
    automatically from ``ServiceAdapter.SERVICE_DEFINITIONS`` (see
    ``__init_subclass__``) so the definition can never drift from the provider.
    ``SERVICE_DEFINITIONS`` remains the metadata source of truth during the
    migration -- the provider composes with it, it does not replace it.

    Config shape + versioning (RC-5 Phase 2)
    ----------------------------------------
    A configurable service owns its config as a self-contained, independently
    versioned unit, mirroring the Kubernetes CRD model (envelope + discriminator +
    hub-and-spoke conversion):

    * ``config_model`` -- a Pydantic model that is both the value guardrail and the
      source of the service's JSON-schema fragment. The service's config in the
      project file is validated against it; the global ``project_v2.json`` validates
      only the envelope and stays stable as service configs evolve.
    * ``config_schema_version`` -- the service's *current* ("hub"/storage) schema
      version as a ``major.minor`` string. Each service versions independently.
    * ``migrate_config`` -- forward-only conversion (spoke -> hub). ZAD never serves
      old versions to clients; it only ever reads a possibly-old file and writes the
      current version, so no down-conversion is needed. Convert-then-validate: an
      older config is migrated forward, then validated against ``config_model``.

    Services without config (publish-on-web, storage, platform, ...) leave
    ``config_model`` as ``None`` and inherit the no-op defaults.
    """

    #: The service this provider handles. Set by each concrete subclass.
    service_type: ClassVar[ServiceType]
    #: The existing metadata dataclass, bound automatically from service_type.
    definition: ClassVar[ServiceDefinition]

    #: Pydantic model for this service's config, or None if it takes no config.
    config_model: ClassVar[type[BaseModel] | None] = None
    #: Current config schema version (major.minor). Only meaningful with a config_model.
    config_schema_version: ClassVar[str] = "1.0"

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        # A concrete provider must declare which service it is; the definition is
        # then derived, not duplicated. Abstract intermediate subclasses (no
        # service_type) are allowed and simply skipped.
        service_type = cls.__dict__.get("service_type")
        if service_type is not None:
            cls.definition = ServiceAdapter.SERVICE_DEFINITIONS[service_type]

    def migrate_config(self, config: dict[str, object], from_version: str) -> dict[str, object]:
        """Convert an older config forward to ``config_schema_version`` (hub).

        Forward-only (spoke -> hub); the default is identity, correct for a service
        still at its first version. A service that bumps its version overrides this
        and applies the ordered steps ``from_version -> ... -> current``. Keep each
        step simple and lossless where possible (the Kubernetes conversion rule).
        """
        return config

    def validate_config(self, raw_config: dict[str, object] | None, from_version: str | None = None) -> BaseModel:
        """Migrate (if needed) then validate this service's config; fail closed.

        ``from_version`` is the version stamped on the project-file entry; ``None``
        means the entry predates versioning and is treated as this service's current
        version (the config already matches the current model). Raises
        ``pydantic.ValidationError`` on bad values, or ``TypeError`` if the service
        takes no config.
        """
        if self.config_model is None:
            raise TypeError(f"Service '{self.service_type.value}' takes no config")
        config = dict(raw_config or {})
        migrated = self.migrate_config(config, from_version or self.config_schema_version)
        return self.config_model.model_validate(migrated)
