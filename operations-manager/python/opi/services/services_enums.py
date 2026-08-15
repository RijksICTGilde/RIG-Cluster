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

    # Outgoing mail: an SMTP account on the platform relay, which authenticates once
    # towards the upstream mail server. Named after what it does (like publish-on-web),
    # not after the protocol or the product behind it.
    SEND_EMAIL = "send-email"

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
    #: Niet gebonden: de dienst geldt voor het project als geheel en verschijnt dus niet in
    #: de keuze per component of per deployment. Toegevoegd omdat die keuze er niet was:
    #: invite koos noodgedwongen COMPONENT ("binding is not meaningful here, but the field
    #: is required"), waarna de UI meldde dat je hem per component kiest en de dienst ook
    #: echt in de componentkeuze verscheen, terwijl een uitnodiging bij het project hoort.
    PROJECT = "project"


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
    MAIL = "mail"


class ServiceKind(Enum):
    """Whether a project chooses this service, or the platform always runs it.

    On the service, not in the cluster config: a system service declares everything
    about itself, and "system" is not a per-cluster property (which would allow drift
    in something that must be identical everywhere).
    """

    USER = "user"  # appears in the project file's services list
    SYSTEM = "system"  # always runs, never in the list


class HookLevel(Enum):
    """What an event iterates over.

    A bare ``Enum`` (not ``StrEnum``), like ``ServiceType`` and ``ConfigLayer``: a
    stray string can never masquerade as a level. Naming the axis costs one line and
    makes it explicit what generic code must iterate when an event fires.
    """

    PROJECT = "project"
    DEPLOYMENT = "deployment"
    COMPONENT = "component"


class ActionEvent(Enum):
    """Something happened; a service changes state (RC-39).

    The writing family. A service gets context and mutates it, and the family carries one
    contract the UI family does not have:

    **An action handler never commits.** It mutates ``payload.project_data`` in place and
    the caller does ONE ``save_and_commit_project()`` after the scan, for every outcome
    together. Two services that each commit give two commits and a lost update. The
    contract sits on the family rather than in a note next to one handler, because that
    is exactly the kind of thing that quietly dies when a second inhabitant arrives.

    An action handler is ``async`` (a UI handler is not): acting on the world is the side
    that may await, and the split keeps the two dispatches free of "await it if it happens
    to be awaitable".

    ``AFTER_SYNC`` is a moment in the deploy lifecycle. ``REDEPLOY`` is the writing
    counterpart of ``UIEvent.DEPLOYMENT_STATE``: a service that put a deployment in a
    state gets the moment new content is deliberately rolled out onto it, and clears the
    state that described the old content. It is named after the action, not after one
    trigger of it: an image update and a deployment upsert both replace what runs there,
    and an event called "image replaced" would have left the upsert to be bolted on as an
    exception. Without it every state needs its own ``if`` in ``project_manager``, which
    is how a component disabled for anything other than an image-pull error stayed
    switched off after its image was fixed (RC-37).
    """

    AFTER_SYNC = "after-sync"
    REDEPLOY = "redeploy"

    @property
    def level(self) -> HookLevel:
        return _EVENT_LEVELS[self]


class UIEvent(Enum):
    """Where am I visible; a service returns something to show (RC-39).

    The reading family. A service gets context and returns contributions -- sections,
    facts -- and mutates nothing. It fails visibly: a service that returns nothing simply
    has no section, so there is no state left half-written behind the answer.

    A UI handler is synchronous. A block that needs data from a connector gets it from the
    caller through its payload (``DeploymentPageContext.backend_available``) or lazy-loads
    it over its own route, so rendering a page never fans out into cluster calls.

    ``DEPLOYMENT_STATE`` is in this family and not in ``ActionEvent`` for exactly that
    reason: it is a question ("what do you know about this deployment"), answered from the
    project file, mutating nothing -- even though its most important reader is the health
    check rather than a template.
    """

    PROJECT_SECTIONS = "project-sections"
    DEPLOYMENT_SECTIONS = "deployment-sections"
    DEPLOYMENT_STATE = "deployment-state"

    @property
    def level(self) -> HookLevel:
        return _EVENT_LEVELS[self]


#: A service event: one of the two families. Generic code that only indexes listeners
#: (the registry) is written against this union; anything that dispatches picks a family,
#: because the two have different contracts.
ServiceEvent = ActionEvent | UIEvent


#: The level each event iterates over. ``AFTER_SYNC`` fires once per deployment, after the
#: sync; ``DEPLOYMENT_STATE`` and ``DEPLOYMENT_SECTIONS`` are asked about one deployment;
#: ``REDEPLOY`` names the components the rollout put new content on, which is why it is the
#: only component-level event. ``PROJECT_SECTIONS`` is the one project-level event.
_EVENT_LEVELS: dict[ServiceEvent, HookLevel] = {
    ActionEvent.AFTER_SYNC: HookLevel.DEPLOYMENT,
    ActionEvent.REDEPLOY: HookLevel.COMPONENT,
    UIEvent.PROJECT_SECTIONS: HookLevel.PROJECT,
    UIEvent.DEPLOYMENT_SECTIONS: HookLevel.DEPLOYMENT,
    UIEvent.DEPLOYMENT_STATE: HookLevel.DEPLOYMENT,
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
