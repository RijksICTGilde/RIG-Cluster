"""V2 API response models for async task endpoints."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from opi.core.errors import ErrorCategory
from opi.services.catalog.approval import ApprovalStatus
from opi.services.project_environment import Omgeving


class DeploymentStatus(StrEnum):
    """Overall deployment state.

    A single enum covering everything a caller wants to switch on. Argo's
    two orthogonal dimensions (sync, health) are collapsed using a
    worst-of-both priority: Degraded/Suspended/Missing > OutOfSync >
    Progressing > Healthy. Pending and Unavailable are *our* states for
    "we have no data," distinct from Argo's own Unknown.

    ``Disabled`` is ours too, and it is not an Argo verdict at all: a deployment
    whose components are all switched off runs zero replicas, which Argo reports as
    Healthy because nothing is failing. Reporting that as Healthy is untrue, so the intent
    recorded in the project file replaces it, and replaces only it.
    """

    Healthy = "Healthy"
    Disabled = "Disabled"  # every component switched off on purpose (replicas: 0)
    Degraded = "Degraded"
    Progressing = "Progressing"
    OutOfSync = "OutOfSync"  # cluster is running, but drifted from git
    Suspended = "Suspended"
    Missing = "Missing"
    Pending = "Pending"  # cluster has no Application yet (not yet reconciled)
    Unavailable = "Unavailable"  # status fetch failed (only in list endpoint)
    Unknown = "Unknown"  # status backend itself reports Unknown


#: ``error_type`` values that mean "the request itself was wrong". The field is a free
#: string written at a couple of dozen call sites, so this is the translation into the
#: closed set a client can switch on. Everything not in here stays ``Unknown``: a category
#: is a promise about attribution, and guessing one is worse than admitting we do not know.
#: Deliberately absent: ``conflict`` (two writers raced, which is nobody's mistake and may
#: well succeed on a retry), ``processing_failed`` (the rollout itself did not come up
#: healthy, which can be the user's image or the cluster; ``component_failures`` carries
#: which one, so a category here would be a guess) and ``internal_error``, which is not a
#: caller mistake. The ``InternalError`` member that names it belongs to the HTTP error
#: envelope (:class:`ProblemDetail`); putting a task's ``internal_error`` on it would change
#: an ``error_category`` value clients already switch on, which is an API change of its own.
_CALLER_ERROR_TYPES: frozenset[str] = frozenset(
    {
        "already_exists",
        "ambiguous_cluster",
        "ambiguous_repository",
        "component_not_found",
        "deployment_not_found",
        "domain_validation",
        "duplicate_component",
        "duplicate_component_in_deployment",
        "in_use",
        "invalid_component_references",
        "invalid_deployments",
        "invalid_project_name",
        "invalid_request",
        "invalid_services",
        "invalid_values",
        "not_found",
        "validation_error",
    }
)


def error_category_for(error_type: str | None) -> ErrorCategory:
    """Translate a task's free-form ``error_type`` into a category a client can act on.

    ``invalid_target`` maps to the member that already exists for it, the caller-supplied
    restore destination; the rest of the caller's mistakes land on ``InvalidInput``.
    """
    if not error_type:
        return ErrorCategory.Unknown
    if error_type == "invalid_target":
        return ErrorCategory.InvalidTarget
    return ErrorCategory.InvalidInput if error_type in _CALLER_ERROR_TYPES else ErrorCategory.Unknown


def category_for_status(status_code: int) -> ErrorCategory:
    """The category of a failed HTTP answer, derived from its status.

    Only a 5xx gets the envelope, and a 5xx means exactly one thing to the caller: it was not
    their request and a retry may help. Anything outside that is a promise we cannot keep, so
    ``Unknown``.
    """
    return ErrorCategory.InternalError if status_code >= 500 else ErrorCategory.Unknown


#: Three things about :class:`ProblemDetail` that do not fit in a field description, because
#: those land in the OpenAPI document:
#:
#: * ``detail`` is a plain sentence and never the raw exception. A raw one carried the
#:   internal IP address and the database port to the outside.
#: * ``reference`` is the mark from :mod:`opi.core.flow_id`, the same one on the screen and on
#:   every log line of this request, which is what makes a message usable without giving
#:   anything away.
#: * there is NO ``explanation`` beside ``detail``, unlike :class:`StatusError`, which keeps
#:   the raw cluster text in ``message``. There is no raw text here, so an ``explanation``
#:   would be a second copy of ``detail``.
class ProblemDetail(BaseModel):
    """What a failed HTTP answer carries: RFC 7807, plus a category and a mark.

    Only on a 5xx. A 4xx keeps the bare ``{"detail": ...}`` that clients read today.
    """

    type: str = Field(
        default="about:blank",
        description="URI die het fouttype benoemt; 'about:blank' als er geen specifieker type is",
    )
    title: str = Field(..., description="Korte samenvatting van het fouttype (de HTTP-statusomschrijving)")
    status: int = Field(..., description="HTTP-statuscode, herhaald in de body zoals RFC 7807 voorschrijft")
    detail: str = Field(..., description="Wat er misging en wat de aanroeper eraan kan doen, in gewone taal")
    instance: str | None = Field(default=None, description="Het pad waarop de fout optrad")
    category: ErrorCategory = Field(
        ..., description="Categorie waarop een client kan sturen; 5xx is altijd InternalError"
    )
    reference: str | None = Field(
        default=None,
        description="Het kenmerk waarmee een beheerder deze fout in de log terugvindt (op het scherm: 'kenmerk')",
    )


class AsyncTaskAcceptedResponse(BaseModel):
    """Standard 202 Accepted response for all V2 async endpoints."""

    status: str = Field(default="accepted", description="Always 'accepted' for async operations")
    task_id: str = Field(..., description="Unique task identifier (UUID)")
    task_type: str = Field(..., description="Type of operation being performed")
    poll_url: str = Field(..., description="URL to poll for task status, e.g. /api/tasks/{task_id}")

    model_config = {
        "json_schema_extra": {
            "example": {
                "status": "accepted",
                "task_id": "550e8400-e29b-41d4-a716-446655440000",
                "task_type": "upsert_deployment",
                "poll_url": "/api/tasks/550e8400-e29b-41d4-a716-446655440000",
            }
        }
    }


class CreateProjectRequest(BaseModel):
    """Everything needed to create a project from outside the browser.

    **The technical name is not an input.** It is derived from the display name, by the same
    function the portal uses: initials or the first few characters, plus a random suffix.
    That suffix is what makes the name unique by construction; letting a caller choose the
    name would turn uniqueness into first-come-first-served, would let short names be
    squatted, and would make the two roads produce differently shaped names.

    The generated name is in the response, as ``project_name``.
    """

    display_name: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description=(
            "Human-readable name of the project, as shown in the portal. The technical name is "
            "derived from it and returned in the response; it cannot be chosen."
        ),
        examples=["Mijn Project"],
    )
    description: str = Field(..., max_length=1024, description="What this project is for", examples=["Nog een test"])


class CreateProjectAcceptedResponse(BaseModel):
    """202 Accepted response for project creation.

    Carries the project's API key, which exists nowhere else in plaintext: every
    later call for this project authenticates with it. It is returned in the
    response body and never in a URL.
    """

    status: str = Field(default="accepted", description="Always 'accepted' for async operations")
    task_id: str = Field(..., description="Unique task identifier (UUID)")
    task_type: str = Field(default="create_project", description="Type of operation being performed")
    poll_url: str = Field(..., description="URL to poll for task status, e.g. /api/tasks/{task_id}")
    project_name: str = Field(..., description="The technical name of the created project")
    api_key: str = Field(..., description="The project's API key, for the X-API-Key header on every later call")

    model_config = {
        "json_schema_extra": {
            "example": {
                "status": "accepted",
                "task_id": "550e8400-e29b-41d4-a716-446655440000",
                "task_type": "create_project",
                "poll_url": "/api/tasks/550e8400-e29b-41d4-a716-446655440000",
                "project_name": "mijn-project",
                "api_key": "Xk3mQ9vP2rT7wY1bN5cL8hJ4gF6dS0aZ",
            }
        }
    }


class ProjectListItem(BaseModel):
    """One project a caller may see, with what they need to act on it.

    Carries the project's API key only when the caller's role in the project is ``admin`` or
    ``owner``. That is the same gate the project detail page puts in front of the same secret,
    and the same gate the web UI puts in front of every project mutation: the key itself knows
    no roles, so a ``developer`` holding it could do through the API what the UI refuses them.
    A caller holds a secret after reading this and should treat the response accordingly.
    """

    name: str = Field(..., description="The technical project name", examples=["mijn-project"])
    description: str = Field(default="", description="What this project is for", examples=["Nog een test"])
    roles: list[str] = Field(
        default_factory=list,
        description=(
            "De rollen van de aanroeper op dit project. Wat elke rol opent staat op /rollen. "
            "Een platformbeheerder krijgt ['admin', 'ops'] op elk project"
        ),
        examples=[["admin", "ops"]],
    )
    api_key: str | None = Field(
        default=None,
        deprecated=True,
        description=(
            "SECRET. The project's API key, for the X-API-Key header on every per-project call. "
            "Alleen aanwezig voor een lid met de rol 'admin'; null voor de rest. DEPRECATED: ask for one project's key "
            "with GET /api/v2/projects/{project_name}/api-key, which logs who got it. This field "
            "disappears from the list once the CLI uses that route"
        ),
        examples=["Xk3mQ9vP2rT7wY1bN5cL8hJ4gF6dS0aZ"],
    )


class ProjectListResponse(BaseModel):
    """The projects this caller may see."""

    projects: list[ProjectListItem] = Field(
        default_factory=list, description="Projects the caller is a member of, sorted by name"
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "projects": [
                    {
                        "name": "mijn-project",
                        "description": "Nog een test",
                        "roles": ["admin", "ops"],
                        "api_key": "Xk3mQ9vP2rT7wY1bN5cL8hJ4gF6dS0aZ",
                    },
                    {
                        "name": "project-van-het-team",
                        "description": "Waar ik in meewerk",
                        "roles": ["developer"],
                        "api_key": None,
                    },
                ]
            }
        }
    }


class ProjectApiKeyResponse(BaseModel):
    """The API key of one project, for a caller that may change that project."""

    api_key: str = Field(
        ...,
        description="SECRET. The project's API key, for the X-API-Key header on every per-project call",
        examples=["Xk3mQ9vP2rT7wY1bN5cL8hJ4gF6dS0aZ"],
    )


class DeploymentComponentDetail(BaseModel):
    """Component within a deployment, including image reference."""

    reference: str = Field(..., description="Component name reference")
    image: str = Field(..., description="Container image URL")


class StatusError(BaseModel):
    """A single error or warning entry surfaced from the cluster."""

    resource: str = Field(..., description="Kind/name (e.g. 'Pod/frontend-abc') or 'Event/<obj>' for events")
    message: str = Field(..., description="Raw cluster message, for automation, regex matching, correlation")
    category: ErrorCategory = Field(..., description="Programmatic category for filtering, grouping, colorizing")
    explanation: str | None = Field(
        default=None,
        description=(
            "Human-friendly explanation of the category and what to do next; "
            "null when the category has no canned guidance (e.g. Unknown)"
        ),
    )
    timestamp: str | None = Field(default=None, description="ISO timestamp if known")


class StatusWarning(BaseModel):
    """A resource ArgoCD does not roll out because its kind is not on the allowlist.

    Not an error: the rest of the deployment is rolled out and ArgoCD keeps the app Synced
    and Healthy. See ``features/argocd-resource-allowlist.md``.
    """

    type: str = Field(..., description="The ArgoCD condition type, e.g. 'ExcludedResourceWarning'")
    severity: str = Field(..., description="Always 'warning': the rest of the deployment is rolled out")
    category: ErrorCategory = Field(..., description="Programmatic category for filtering, grouping, colorizing")
    group: str = Field(..., description="API group of the excluded resource; empty for a core kind")
    kind: str = Field(..., description="Kubernetes kind of the excluded resource, e.g. 'HTTPRoute'")
    name: str = Field(..., description="Name of the excluded resource")
    message: str = Field(..., description="Nederlandse melding voor de gebruiker")
    raw_message: str = Field(..., description="Raw ArgoCD condition message, for automation and correlation")


class StatusDeviation(BaseModel):
    """A resource that keeps the deployment away from Synced/Healthy, with the reason.

    Deviations are not application problems (those are :class:`StatusError`): they explain
    an OutOfSync or Progressing status. Example: a resource that was removed from git and
    whose deletion the cluster cannot finish keeps the deployment OutOfSync forever, while
    every workload runs exactly what git prescribes.
    """

    resource: str = Field(..., description="Kind/name (e.g. 'Job/production-backend-migrate-171')")
    kind: str = Field(..., description="Kubernetes kind, for filtering/grouping")
    reason: str = Field(..., description="Human-readable reason why this resource deviates (Dutch)")


class PendingRolloutResponse(BaseModel):
    """Changes that were saved but deliberately not rolled out."""

    project: str = Field(..., description="Technical name of the project.")
    count: int = Field(..., description="Number of saved changes that have not been rolled out yet. 0 means in sync.")
    since: str | None = Field(
        default=None,
        description=(
            "ISO timestamp of the OLDEST change still waiting, so a caller can tell a change "
            "made minutes ago from one that has been waiting a week. Null when count is 0."
        ),
    )
    task_types: list[str] = Field(
        default_factory=list,
        description="Which kinds of change are waiting (e.g. 'configure_service'), deduplicated and sorted.",
    )
    rollout_in_progress: bool = Field(
        default=False,
        description=(
            "True when a task that will process the project, for every deployment the waiting "
            "changes touch, is queued or running right now. 'count' only drops once that "
            "processing has run, so a non-zero count with this flag set means the changes are "
            "being rolled out at this moment, not that they are sitting untouched. Defaults to "
            "false, so an older caller reads it as before."
        ),
    )


class ApprovalNoticeStatus(StrEnum):
    """The state of an approval this deployment does NOT have yet.

    An enum and not a bare string, because this field is meant to be branched on and the
    whole set therefore belongs in ``/openapi.json``. What each value means for a client:

    * ``requested``: the request is open and waiting for an admin. Nothing is wrong, waiting
      is the right behaviour, and it can take days.
    * ``denied``: an admin refused the request. This will not come good by itself, so a
      pipeline belongs to fail here. ``by``, ``date`` and ``message`` carry the verdict.
    * ``none``: nothing has been requested for this value yet. This is not approved either,
      so the consequence in ``text`` holds in full, but nobody is waiting on it.

    ``approved`` is deliberately absent: this list holds only what a deployment does not have
    yet. The members derive from :class:`ApprovalStatus`, the lifecycle in the project file,
    so the spelling here cannot drift from the stored value.
    """

    NONE = ApprovalStatus.NONE
    REQUESTED = ApprovalStatus.REQUESTED
    DENIED = ApprovalStatus.DENIED


class ApprovalNoticeResponse(BaseModel):
    """An approval this deployment needs and does not have (yet).

    The counterpart of ``pending_rollout``: that one says a saved change is not on the cluster
    yet, this one says a deployment is waiting on an admin's verdict. Domains and subdomains
    are on request, so a write that claims one creates the request and reports it back here;
    without that, "no ingress appears" is the first a client hears of it.

    The list is empty when everything the deployment asks for is approved.
    """

    service: str = Field(
        ...,
        description="De dienst die deze goedkeuring bezit, zoals in de servicecatalogus.",
        examples=["publish-on-web"],
    )
    type: str = Field(..., description="Wat er goedgekeurd moet worden binnen die dienst.", examples=["domain"])
    label: str = Field(..., description="Hoe de portal dit soort goedkeuring noemt.", examples=["Domein"])
    subject: str = Field(..., description="Wat er is aangevraagd.", examples=["mijn-app.nl"])
    status: ApprovalNoticeStatus = Field(
        ...,
        description=(
            "De stand van de aanvraag, om op te vertakken: 'requested' (aangevraagd, wacht op een "
            "beheerder -- wachten of zwijgen), 'denied' (afgewezen door een beheerder -- dit komt "
            "vanzelf niet goed, hier hoort een pijplijn op te falen) of 'none' (nog niets "
            "aangevraagd). 'approved' komt hier niet voor: wat is goedgekeurd staat niet in deze "
            "lijst."
        ),
        examples=["requested"],
    )
    text: str = Field(
        ...,
        description="Wat dit betekent voor deze deployment, inclusief het gevolg, in gewone taal.",
        examples=[
            "Het domein mijn-app.nl is aangevraagd en wacht op goedkeuring. Deze deployment is daarom bereikbaar op het standaard clusteradres."
        ],
    )
    by: str | None = Field(default=None, description="Wie het laatste oordeel gaf; leeg zolang er geen oordeel is.")
    date: str | None = Field(default=None, description="Wanneer dat oordeel viel, ISO 8601.")
    message: str | None = Field(default=None, description="De toelichting die bij dat oordeel is gegeven.")


#: Shared description, so the field promises the same on a read answer and on a task answer.
APPROVALS_DESCRIPTION = (
    "Goedkeuringen die deze deployment nog niet heeft. Leeg wanneer alles is goedgekeurd. "
    "Een niet-goedgekeurd domein of subdomein blokkeert de deployment niet: die publiceert "
    "dan op het standaard clusteradres, dus dit veld is de enige plek waar je ziet dat het "
    "gevraagde adres nog niet in gebruik is."
)


class RunningSync(BaseModel):
    """The sync ArgoCD is running right now, or last ran without finishing.

    This is ArgoCD's own rollout and NOT ZAD's queue: that one lives in
    ``pending_rollout.rollout_in_progress`` and is about tasks that still have to write to
    git. Here the commit is in git already and the cluster is applying it. The two can be on
    independently of each other.
    """

    phase: str = Field(..., description="ArgoCD's operation phase: Running of Terminating.")
    started_at: str | None = Field(
        default=None,
        description="ISO timestamp waarop ArgoCD deze sync begon. Null als ArgoCD hem niet meldt.",
    )
    revision: str | None = Field(
        default=None,
        description=(
            "De git-revisie die deze sync UITROLT. Kan een andere zijn dan 'sync_revision': een "
            "lopende operation zit vast aan de revisie waarmee hij begon en ziet nieuwere "
            "commits niet, terwijl 'sync_revision' de revisie is waartegen vergeleken wordt."
        ),
    )
    message: str | None = Field(
        default=None,
        description="Waar ArgoCD op wacht, bijvoorbeeld 'waiting for healthy state of apps/Deployment/x'.",
    )
    blocks_next_sync: bool = Field(
        ...,
        description=(
            "True wanneer deze sync elke volgende sync op deze deployment tegenhoudt: een "
            "nieuwe commit rolt dan niet uit. False betekent dat de stand alleen nog "
            "gerapporteerd wordt."
        ),
    )


class DeploymentDetail(BaseModel):
    """Full deployment state as returned by the GET endpoints."""

    name: str = Field(..., description="Deployment name")
    project: str = Field(..., description="Project name")
    cluster: str = Field(..., description="Target cluster")
    namespace: str = Field(..., description="Kubernetes namespace")
    subdomain: str | None = Field(default=None, description="DNS subdomain override")
    environment: Omgeving | None = Field(
        default=None,
        description=(
            "De OTAP-markering zoals ze in het projectbestand staat, of null als er geen staat. "
            "Schrijfbaar via POST /api/v2/projects/{project_name}/:upsert-deployment."
        ),
    )
    effective_environment: Omgeving = Field(
        ...,
        description=(
            "De markering die voor deze deployment GELDT: de eigen, anders 'ontwikkeling'. Alleen-lezen, afgeleid."
        ),
    )
    components: list[DeploymentComponentDetail] = Field(default_factory=list, description="Component references")
    urls: dict[str, str] = Field(
        default_factory=dict,
        description="Computed public URLs, keyed by component name",
    )
    status: DeploymentStatus = Field(
        ...,
        description="Overall deployment state. Always present; check value to render.",
    )
    sync_revision: str | None = Field(
        default=None,
        description="Git revision (full SHA) the cluster last reconciled; null if never reconciled",
    )
    last_synced_at: str | None = Field(
        default=None,
        description=(
            "ISO timestamp waarop de laatste sync AFRONDDE, wat de uitkomst ook was. Combineer "
            "met status om te weten of die poging lukte; bij een Degraded deployment kan dit het "
            "moment van een mislukte sync zijn. Null wanneer er nooit een sync afrondde, en ook "
            "zolang er een loopt die nog niet klaar is: kijk dan naar 'running_sync'."
        ),
    )
    rolled_out_revision: str | None = Field(
        default=None,
        description=(
            "De git-revisie die de laatste sync werkelijk heeft UITGEROLD, uit "
            "'operationState.syncResult.revision'. Niet hetzelfde als 'sync_revision', dat de "
            "revisie is waartegen de cluster vergelijkt. Lopen die twee uiteen, dan is er een "
            "commit die nog niet is toegepast."
        ),
    )
    running_sync: RunningSync | None = Field(
        default=None,
        description="De sync die ArgoCD op dit moment uitvoert, of null als er geen loopt.",
    )
    errors: list[StatusError] = Field(
        default_factory=list,
        description=(
            "Cluster-side error entries; populated only when status indicates a problem "
            "(Degraded, OutOfSync, Suspended, Missing). Empty otherwise."
        ),
    )
    warnings: list[StatusWarning] = Field(
        default_factory=list,
        description=(
            "Resources die het cluster niet uitrolt omdat hun kind niet op de allowlist staat. "
            "Gevuld bij elke status, ook een gezonde: ArgoCD houdt de app Synced en Healthy "
            "terwijl het object ontbreekt."
        ),
    )
    deviations: list[StatusDeviation] = Field(
        default_factory=list,
        description=(
            "Resources that keep the deployment away from Synced, each with the reason. "
            "Populated in the same problem states as 'errors'; an OutOfSync deployment "
            "with empty 'errors' and only deviations is running fine - the deviations "
            "tell what is still off (e.g. leftovers awaiting cleanup)."
        ),
    )
    source: str = Field(
        default="project-file",
        description=(
            "Where the DESCRIPTION comes from: always 'project-file'. Dit antwoord mengt "
            "twee bronnen, en dat is de reden dat dit veld hier staat. 'components', "
            "'urls' en 'subdomain' komen uit het projectbestand en zijn dus de GEWENSTE "
            "toestand; 'status', 'sync_revision', 'last_synced_at' en 'errors' komen uit "
            "de cluster. Een component dat met rollout=false is opgeslagen heeft daarom "
            "meteen een URL, terwijl er nog niets draait dat hem bedient. Kijk naar "
            "'pending_rollout' om te zien of de twee uit elkaar lopen."
        ),
    )
    approvals: list[ApprovalNoticeResponse] = Field(
        default_factory=list,
        description=APPROVALS_DESCRIPTION,
    )
    pending_rollout: PendingRolloutResponse | None = Field(
        default=None,
        description=(
            "Saved changes that are not on the cluster yet. Gevuld op GET "
            "/projects/{project}/deployments/{deployment}; null in een lijst, waar het "
            "omhullende antwoord het draagt. Het is een eigenschap van het PROJECT, dus "
            "hem per deployment herhalen zou hetzelfde getal zo vaak neerzetten als er "
            "deployments zijn. Ook null als de takenservice niet bereikbaar is: het etiket "
            "ontbreekt dan, de beschrijving niet."
        ),
    )


class DeploymentListResponse(BaseModel):
    """Response for GET /projects/{project_name}/deployments."""

    project: str
    cluster: str
    source: str = Field(
        default="project-file",
        description="Zie DeploymentDetail.source: de beschrijving komt uit het projectbestand.",
    )
    pending_rollout: PendingRolloutResponse | None = Field(
        default=None,
        description="Saved changes that are not on the cluster yet, voor het hele project.",
    )
    deployments: list[DeploymentDetail] = Field(default_factory=list)


class ClusterDomainOption(BaseModel):
    """One choice for ``base-domain`` on a deployment."""

    value: str = Field(
        description=(
            "Wat je in base-domain zet. Leeg betekent het standaarddomein van het cluster. "
            "Een eigen domein staat hier niet tussen: dat zet je door de domeinnaam zelf in "
            "base-domain te schrijven (bijvoorbeeld 'mijn-app.nl')."
        ),
        examples=["rijksapp.nl"],
    )
    label: str = Field(description="Hoe de portal deze keuze toont.", examples=["rijksapp.nl"])
    supports_dots: bool = Field(
        default=False,
        alias="supports-dots",
        description=(
            "Of dit domein losse subdomeinen met punten aankan. Dat bepaalt welke waarden van "
            "'domain-format' hier passen: de streepjes-varianten kunnen altijd, de punt-varianten "
            "alleen als dit waar is. Zonder dit veld was die regel wel beschreven en nergens uit "
            "af te leiden."
        ),
        examples=[True],
    )

    model_config = ConfigDict(populate_by_name=True)


class ClusterInfo(BaseModel):
    """A cluster this project can run on, with the domains it offers."""

    name: str = Field(description="Naam van het cluster, zoals in deployments[].cluster.", examples=["odcn-production"])
    manager: bool = Field(description="Of deze OPI-instantie dit cluster beheert.", examples=[True])
    default_domain: str = Field(
        default="",
        alias="default-domain",
        description=(
            "Het eigen domein van dit cluster. Dit is het ENIGE domein dat zonder goedkeuring "
            "in gebruik gaat: een deployment met base-domain leeg of met precies deze waarde "
            "krijgt meteen het adres dat hij vraagt. Elke andere waarde in base-domain -- ook "
            "een domein dat gewoon in 'base-domains' staat -- levert een goedkeuringsaanvraag "
            "op, en tot iemand die toekent draait de deployment op dit domein. De twee gevallen "
            "zien er in 'base-domains' identiek uit, dus dit veld is waarop je ze uit elkaar houdt."
        ),
        examples=["apps.odcn-production.nl"],
    )
    base_domains: list[ClusterDomainOption] = Field(
        default_factory=list,
        alias="base-domains",
        description=(
            "De domeinen die dit cluster zelf aanbiedt voor base-domain op een deployment. Dit "
            "is geen gesloten verzameling: een eigen domein schrijf je als domeinnaam in "
            "base-domain, en 'custom-domain-certificates' zegt wat dat hier oplevert."
        ),
    )
    custom_domain_certificates: bool = Field(
        default=True,
        alias="custom-domain-certificates",
        description=(
            "Of dit cluster een certificaat kan aanvragen voor een eigen domein, dus voor een "
            "base-domain dat niet in 'base-domains' staat. Is dit false, dan wordt de deployment "
            "gewoon aangemaakt en bereikbaar, maar krijgt hij geen geldig certificaat en ziet een "
            "bezoeker een certificaatfout: lever dan een eigen certificaat mee (tls: provided met "
            "het certificaat als attachment) of kies een domein uit 'base-domains'. Dit zegt niets "
            "over DNS of eigendom, alleen over wat het platform hier kan uitgeven."
        ),
        examples=[True],
    )

    model_config = ConfigDict(populate_by_name=True)


class SubdomainCheckResponse(BaseModel):
    """Response for GET /projects/{project_name}/subdomains/check/{subdomain}."""

    subdomain: str = Field(description="The subdomain that was checked", examples=["myapp"])
    base_domain: str = Field(description="The base domain it was checked under", examples=["rijksapp.nl"])
    available: bool = Field(
        description=(
            "Whether this subdomain is still free WITHIN ZAD, under the given base domain. "
            "It says nothing about who owns that base domain or whether DNS for it exists: "
            "any syntactically valid domain is accepted here, because a project may bring "
            "its own. Read 'cluster_domain' alongside it to know whether the address can go "
            "live without a domain request."
        ),
        examples=[True],
    )
    cluster_domain: bool = Field(
        default=False,
        description=(
            "Whether the base domain is one this cluster serves itself. True means a free "
            "name here is an address you can use straight away; false means it is a domain "
            "of your own, so claiming the name is only the first half and the domain still "
            "has to be requested and approved."
        ),
        examples=[True],
    )
    validation_error: str | None = Field(
        default=None,
        description="Why the answer is 'not available' when it is the value itself that is wrong.",
        examples=[None],
    )


class ClusterListResponse(BaseModel):
    """Response for GET /projects/{project_name}/clusters.

    The choices for ``base-domain`` differ per cluster, which the OpenAPI document cannot
    enumerate (that would be one arbitrary cluster's snapshot), so it points at this endpoint.
    """

    project: str
    clusters: list[ClusterInfo] = Field(default_factory=list)
