from enum import Enum


class ServiceType(Enum):
    """Enumeration of available service types."""

    # Web services
    PUBLISH_ON_WEB = "publish-on-web"
    KEYCLOAK = "keycloak"
    AUTHORIZATION_WALL = "authorization-wall"
    METRICS_SCRAPER = "metrics-scraper"
    HEALTH_CHECK = "health-check"

    # Storage services
    PERSISTENT_STORAGE = "persistent-storage"
    TEMP_STORAGE = "temp-storage"

    # Database services
    POSTGRESQL_DATABASE = "postgresql-database"
    NAMESPACE_POSTGRESQL_DATABASE = "namespace-postgresql-database"

    # Object storage services
    MINIO_STORAGE = "minio-storage"

    # Cache services
    REDIS = "redis"
    NAMESPACE_REDIS = "namespace-redis"

    # Platform services (always-on, not user-selectable)
    PLATFORM = "platform"

    # File attachments (uploaded files mounted into a pod or exposed as env-var)
    ATTACHMENTS = "attachments"

    # Sleep mode: scale idle preview deployments to zero after a deadline, wake on request
    SLEEP_MODE = "sleep-mode"

    # Invitations: onboard users into the project's Keycloak realm via a shared link
    INVITE = "invite"

    # Resource tuning: platform-owned system service that observes running deployments
    # after a sync and raises memory for a component that OOM'd (not user-selectable).
    RESOURCE_TUNING = "resource-tuning"

    # Deployment health: platform-owned system service that judges what the observed pod
    # state of a running deployment means, weighing what other services report about it.
    DEPLOYMENT_HEALTH = "deployment-health"

    # Cross-domain network access: allow this project's pods to reach, and be reached by,
    # named deployments/components of other projects on explicit ports (NetworkPolicy).
    CROSS_DOMAIN_ACCESS = "cross-domain-access"

    # A component's own environment variables and its alias map. System services: every
    # component has them, so they are never in the picker and never in the services list.
    # They own the plain component properties of the same name (see their packages).
    USER_ENV_VARS = "user-env-vars"
    ALIASES = "aliases"


class ServiceBinding(Enum):
    """Whether a service is chosen per component or shared per deployment.

    This is about *selection*: does an individual component tick this service, or does
    the whole deployment get it at once. It says nothing about where the service's
    settings live -- that is ``ConfigLayer`` (``opi/services/catalog/base.py``), and the
    two genuinely differ: keycloak binds per component (each component decides whether it
    sits behind login) while its configuration is one realm for the whole project, so its
    config lives at ``ConfigLayer.PROJECT``. Named ``binding`` rather than ``scope``
    because "scope" read like an answer to "where do I configure this", which it never
    was; ``instructions/services.md`` states the split.

    A closed set, so a typo is a pyright error, not a runtime surprise. Rendered
    values go through ``.value`` (a bare Enum renders as ``ServiceBinding.COMPONENT``).
    """

    COMPONENT = "component"
    DEPLOYMENT = "deployment"


class CleanupStrategy(Enum):
    """How a service's server-side resources are cleaned up on removal.

    - ``NONE``: nothing to clean up (the default).
    - ``IMMEDIATE``: ephemeral/recreatable resources deleted right away.
    - ``DEFERRED``: persistent data marked for deferred deletion (recoverable).
    """

    NONE = "none"
    IMMEDIATE = "immediate"
    DEFERRED = "deferred"


class ManagerKey(Enum):
    """The manager a service delegates server-side cleanup to (``cleanup_manager_key``).

    A closed set resolved by ``DeleteProjectManager._get_manager_for_service``; an enum
    turns a typo into a pyright error instead of a runtime failure mid-teardown.
    """

    DATABASE = "database"
    MINIO = "minio"
    REDIS = "redis"
    KEYCLOAK = "keycloak"
    PVC = "pvc"


class ServiceKind(Enum):
    """Whether a project chooses this service, or the platform always runs it.

    On the service, not in the cluster config: a system service declares everything
    about itself, and "system" is not a per-cluster property (which would allow drift
    in something that must be identical everywhere).
    """

    USER = "user"  # appears in the project file's services list
    SYSTEM = "system"  # always runs, never in the list


class HookLevel(Enum):
    """What a hook point iterates over.

    A bare ``Enum`` (not ``StrEnum``), like ``ServiceType`` and ``ConfigLayer``: a
    stray string can never masquerade as a level. Naming the axis costs one line and
    makes it explicit what generic code must iterate when a hook fires.
    """

    PROJECT = "project"
    DEPLOYMENT = "deployment"
    COMPONENT = "component"


class HookPoint(Enum):
    """A moment at which generic code scans the registry.

    ``AFTER_SYNC`` is a moment in the deploy lifecycle; ``DEPLOYMENT_STATE`` is a
    question asked whenever someone needs to know what the services did to a deployment
    (the health check before it judges, the deployment page before it renders). Hook
    points are an enum, never strings, so ``hook == "after-sync"`` is simply ``False``
    and a loose string cannot slip in anywhere.

    ``REDEPLOY`` is the counterpart of ``DEPLOYMENT_STATE`` on the writing side: a service
    that put a deployment in a state gets the moment new content is deliberately rolled
    out onto it, and clears the state that described the old content. It is named after
    the action, not after one trigger of it: an image update and a deployment upsert both
    replace what runs there, and a hook called "image replaced" would have left the upsert
    to be bolted on as an exception -- which is exactly the shape being removed here.
    Without it every state needs its own ``if`` in ``project_manager``, which is how a
    component disabled for anything other than an image-pull error stayed switched off
    after its image was fixed (RC-37).
    """

    AFTER_SYNC = "after-sync"
    DEPLOYMENT_STATE = "deployment-state"
    REDEPLOY = "redeploy"

    @property
    def level(self) -> HookLevel:
        return _HOOK_LEVELS[self]


#: The level each hook point iterates over. ``AFTER_SYNC`` fires once per deployment,
#: after the sync; ``DEPLOYMENT_STATE`` is asked about one deployment; ``REDEPLOY`` names
#: the components the rollout put new content on, which is why it is the first
#: component-level hook. Project-level machinery is still intentionally not built: it
#: would be code with no caller.
_HOOK_LEVELS: dict[HookPoint, HookLevel] = {
    HookPoint.AFTER_SYNC: HookLevel.DEPLOYMENT,
    HookPoint.DEPLOYMENT_STATE: HookLevel.DEPLOYMENT,
    HookPoint.REDEPLOY: HookLevel.COMPONENT,
}


class CloneFromType(Enum):
    """Type of clone-from source for deployment cloning."""

    DEPLOYMENT = "deployment"
    REMOTE_SOURCE = "remote-source"
    BACKUP = "backup"


class RestoreMode(Enum):
    """Restore target mode: existing deployment or new deployment."""

    EXISTING = "existing"
    NEW = "new"
