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

Each provider carries its existing ``ServiceDefinition`` (unchanged) plus, for
configurable services, a typed ``config_model`` (Phase 2). ``database_manager``
already validates namespace-postgres config through its provider; the remaining
config models are wired into the read path in Phase 3.
"""

from __future__ import annotations

from abc import ABC
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

from opi.services.services import ServiceAdapter, ServiceDefinition

if TYPE_CHECKING:
    from pydantic import BaseModel

    from opi.services.services_enums import ServiceType

#: A service's raw config as it appears in the project file: a dict for most
#: services, or a list for sequence configs (e.g. storage mounts).
ServiceConfigData = dict[str, Any] | list[Any]


@dataclass
class ProvisionContext:
    """Inputs a provider needs to provision one deployment's resources (RC-5 Phase 4).

    Carries the *already-resolved* managers so a provider delegates to its manager
    without importing it -- keeping provider.py free of manager imports. The managers
    keep their own self-guards (e.g. ``_deployment_uses_postgresql``) and are
    replay-safe, so dispatching through providers stays byte-identical to the old
    fixed sequence.
    """

    project_data: dict[str, Any]
    deployment: dict[str, Any]
    force_clone: bool
    database_manager: Any
    minio_manager: Any
    keycloak_manager: Any
    redis_manager: Any


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

    Services without config (publish-on-web, minio, redis, platform, shared
    postgresql-database, ...) leave ``config_model`` as ``None`` and inherit the
    no-op defaults.
    """

    #: The service this provider handles. Set by each concrete subclass.
    service_type: ClassVar[ServiceType]
    #: The existing metadata dataclass, bound automatically from service_type.
    definition: ClassVar[ServiceDefinition]

    #: Pydantic model for this service's config, or None if it takes no config.
    config_model: ClassVar[type[BaseModel] | None] = None
    #: Current config schema version (major.minor). Only meaningful with a config_model.
    config_schema_version: ClassVar[str] = "1.0"

    #: Wizard/edit config-section id for this service (RC-5 Phase 3), or None if the
    #: service has no config UI. The FormSection object itself lives in the forms
    #: layer (wizard_sections); this is only the declarative link, so provider.py
    #: stays free of forms imports. The forms layer derives SERVICE_CONFIG_SECTIONS /
    #: EDIT_SECTIONS by iterating the registry instead of a hand-synced dict.
    config_section_id: ClassVar[str | None] = None
    #: Modal-edit flow id for this service's config, or None. SERVICE_CONFIG_MODAL_FLOWS
    #: is derived from this.
    modal_flow_id: ClassVar[str | None] = None

    #: Order in the generic provisioning loop (RC-5 Phase 4); lower runs first. Only
    #: meaningful for providers that override ``provision``. The defaults on the four
    #: provisioning providers reproduce today's fixed db -> minio -> keycloak -> redis
    #: sequence.
    provision_order: ClassVar[int] = 100

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        # A concrete provider must declare which service it is; the definition is
        # then derived, not duplicated. Abstract intermediate subclasses (no
        # service_type) are allowed and simply skipped.
        service_type = cls.__dict__.get("service_type")
        if service_type is not None:
            cls.definition = ServiceAdapter.SERVICE_DEFINITIONS[service_type]

    def migrate_config(self, config: ServiceConfigData, from_version: str) -> ServiceConfigData:
        """Convert an older config forward to ``config_schema_version`` (hub).

        Forward-only (spoke -> hub); the default is identity, correct for a service
        still at its first version. A service that bumps its version overrides this
        and applies the ordered steps ``from_version -> ... -> current``. Keep each
        step simple and lossless where possible (the Kubernetes conversion rule).

        ``config`` is a dict for most services, or a list for services whose config
        is a sequence (e.g. storage mounts).
        """
        return config

    def validate_config(
        self, raw_config: ServiceConfigData | None = None, from_version: str | None = None
    ) -> BaseModel:
        """Migrate (if needed) then validate this service's config; fail closed.

        ``from_version`` is the version stamped on the project-file entry; ``None``
        means the entry predates versioning and is treated as this service's current
        version (the config already matches the current model). Raises
        ``pydantic.ValidationError`` on bad values, or ``TypeError`` if the service
        takes no config.

        ``raw_config`` may be a dict (most services) or a list (sequence configs
        such as storage mounts). ``None`` defaults to an empty dict, which suits
        dict-config services; list-config services are always passed their list.
        """
        if self.config_model is None:
            raise TypeError(f"Service '{self.service_type.value}' takes no config")
        config: ServiceConfigData = {} if raw_config is None else raw_config
        migrated = self.migrate_config(config, from_version or self.config_schema_version)
        return self.config_model.model_validate(migrated)

    async def provision(self, ctx: ProvisionContext) -> None:
        """Provision this service's deployment-level resources (RC-5 Phase 4).

        Default no-op -- for services with no deployment-level provisioning (storage,
        publish-on-web, ...). The four provisioning services override this to delegate
        to their manager's ``create_resources_for_deployment`` (self-guarded and
        replay-safe), so the generic loop is byte-identical to the old fixed sequence.
        """
        return
