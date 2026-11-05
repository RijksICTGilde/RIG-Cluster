"""Every step text the progress page can show, in one place.

A step name is a fixed sentence here; the variable part (a deployment, component or
repository name) travels as ``subject`` on the progress manager, so one wording serves
every run. Texts are Dutch because they are what the user reads.

Two rules decide what belongs here. A step text does: anything passed as the NAME of a
step to ``add_task``, ``add_subtask``, ``update_task`` or ``update_current_step``. An
error does not: the ``error`` argument of ``fail_task``, ``fail_project`` and
``_end_step`` comes from an exception and says what went wrong, not which step ran.

``tests/test_progress_step_texts.py`` fails when a literal step name appears anywhere
else under ``opi/``.
"""

# ----------------------------------------------------------------------
# Rollout: reading the project file, provisioning, publishing, deploying
# ----------------------------------------------------------------------
PROJECT_FILE_FETCH = "Projectbestand ophalen"
PROJECT_FILE_VALIDATE = "Projectbestand valideren"
PROJECT_FILE_MIGRATE = "Projectbestand bijwerken naar de nieuwste versie"
PROJECT_FILE_UPDATE = "Projectbestand bijwerken"

ENVIRONMENT_SETUP = "Omgeving inrichten"
PROJECT_DATABASE_SETUP = "Projectdatabase inrichten"
PROJECT_DATABASE_ROLLOUT = "Projectdatabase uitrollen"
PROJECT_DATABASE_WAIT_READY = "Wachten tot de projectdatabase klaar is"
PROJECT_DATABASE_WAIT_CONNECTIONS = "Wachten tot de projectdatabase verbindingen aanneemt"

DATABASE_CONFIG_APPLY = "Databaseconfiguratie toepassen"
STORAGE_CONFIG_APPLY = "Opslagconfiguratie toepassen"
LOGIN_CONFIG_APPLY = "Inlogconfiguratie toepassen"
CACHE_CONFIG_APPLY = "Cacheconfiguratie toepassen"
# "Bootstrap" blijft staan: dat is de term die het team gebruikt.
BOOTSTRAP_ACTIONS = "Bootstrap-acties uitvoeren"
CONNECTION_LIMIT = "Connectielimiet"
CLONE = "Kloon"

CONFIG_FETCH = "Configuratie ophalen"
DEPLOYMENT_CONFIG_BUILD = "Deploymentconfiguratie opstellen"
PROJECT_CONFIG_BUILD = "Projectbrede configuratie opstellen"
CONFIG_PUBLISH = "Configuratie publiceren"

ROLLOUT_PREPARE = "Uitrol klaarzetten"
ROLLOUT = "Uitrollen"
ROLLOUT_CHECK = "Uitrol controleren"

IMAGE_SAVE = "Nieuwe image opslaan in het projectbestand"

# Shown as a step so the progress view says what did not happen, instead of just
# missing a step.
SKIPPED_STEP_LABEL = "Uitrol overgeslagen (rollout=false)"
SKIPPED_STEP_DETAIL = (
    "Wijziging staat in het projectbestand. Verwerk hem met 'Project herverwerken' wanneer je zover bent."
)


def database_not_ready(summary: str) -> str:
    """The database cluster accepts connections, but CNPG does not call it Ready."""
    return f"Projectdatabase is nog niet klaar{summary}"


def retried_step(name: str) -> str:
    """A step that hit a conflict and goes again, so it shows no red."""
    return f"{name}: botsing, opnieuw"


def deleted_deployments_cleanup(count: int) -> str:
    return f"Verwijderde deployments opruimen ({count})"


def waiting_for_applications(count: int) -> str:
    return f"Wachten tot {count} applicatie(s) gesynchroniseerd zijn"


def running_sync_terminated(deployment_names: list[str]) -> str:
    return "Lopende uitrol afgebroken: " + ", ".join(deployment_names)


def tuning_memory(component_names: str, app_name: str) -> str:
    return f"Te weinig geheugen voor {component_names} in {app_name}, geheugenlimiet bijstellen..."


def deployed_but_unhealthy(count_phrase: str, verb: str) -> str:
    return f"Uitgerold, maar {count_phrase} {verb} niet gezond"


def unhealthy_after_rollout(detail: str) -> str:
    """The deployment is out there, but its own pods are not healthy."""
    return (
        f"Let op: de deployment is uitgerold, maar draait niet gezond: {detail}. "
        "Dat ligt aan de applicatie zelf (bijvoorbeeld een image dat niet gepulld kan "
        "worden of een pod die crasht), niet aan het uitrollen."
    )


def sync_not_finished(detail: str) -> str:
    """ArgoCD had not caught up within the wait window, which is not a failure."""
    return (
        f"ArgoCD was nog niet klaar met synchroniseren binnen de wachttijd: {detail}. "
        "De wijziging is wel gepubliceerd; ArgoCD rondt dit zelf af."
    )


# ----------------------------------------------------------------------
# Creating a project
# ----------------------------------------------------------------------
PROJECT_NAME_CHECK = "Projectnaam controleren"
NAMES_CHECK = "Namen controleren"
PROJECT_FILE_BUILD = "Projectbestand opstellen"
PROJECT_FILE_SAVE = "Projectbestand opslaan"
PROJECT_FILE_READ = "Projectbestand lezen"
PROJECT_DATA_FETCH = "Projectgegevens ophalen"
PROJECT_LOOKUP = "Project opzoeken"
PROJECT_DEPLOYED = "Project uitgerold"
PROJECT_SAVED = "Project opgeslagen"

NEW_PROJECT_NOTICE = (
    "Een nieuw project wordt in twee stappen uitgerold: eerst het projectniveau, dat "
    "elke deployment nodig heeft, en daarna de deployments zelf. Daarom duurt dit "
    "langer dan een wijziging op een bestaand project. Loopt het wachten op het "
    "projectniveau in een time-out, dan stopt het uitrollen daar: het project is "
    "opgeslagen, de deployments zijn niet uitgerold."
)

# ----------------------------------------------------------------------
# Components, services and other project-file changes
# ----------------------------------------------------------------------
COMPONENT_ADD = "Component toevoegen"
COMPONENT_UPDATE = "Component bijwerken"
COMPONENT_ADD_TO_DEPLOYMENT = "Component aan deployment toevoegen"
COMPONENT_REMOVE = "Component verwijderen"
SERVICE_ADD = "Service toevoegen"
SERVICE_CONFIG_SAVE = "Serviceconfiguratie opslaan"
VALUES_SAVE = "Waarden opslaan"
DATABASE_SCHEMAS_UPDATE = "Databaseschema's bijwerken"
DEPLOYMENT_SAVE = "Deployment opslaan"
ATTACHMENT_REMOVE = "Bijlage verwijderen"
IMAGE_UPDATE = "Image bijwerken"
DATABASE_CLONE = "Database kopiëren"
BUCKET_CLONE = "Bucket kopiëren"
PROJECT_KEY_ROTATE = "Nieuwe projectsleutel maken, de velden omzetten en opslaan"
BACKUP_PASSWORD_CARRY = "Backupwachtwoord meeverhuizen"  # noqa: S105  # staptekst, geen secret

# ----------------------------------------------------------------------
# Backup and restore
# ----------------------------------------------------------------------
BACKUP_RUN = "Backup uitvoeren"
BACKUP_RUN_LOOKUP = "Backup-run opzoeken"
PROJECT_DEPLOYMENT_LOOKUP = "Project en deployment opzoeken"
# PVC is a vakterm, so it stays.
PVC_BACKUP = "PVC backup"
DATABASE_BACKUP = "Database backup"
OBJECT_STORAGE_BACKUP = "Objectopslag backup"
DEPLOYMENT_CREATE = "Deployment aanmaken"
PVC_RESTORE = "PVC data herstellen"
BACKUP_RESTORE = "Backup herstellen"
RESTORE_ITEM = "Herstellen"

# ----------------------------------------------------------------------
# Deleting
# ----------------------------------------------------------------------
PROJECT_DELETE = "Project verwijderen"
PROJECT_FILE_DELETE = "Projectbestand verwijderen"
PROJECT_INFRA_CLEANUP = "Projectinfrastructuur opruimen"
KEYCLOAK_REALM_DELETE = "Keycloak-realm verwijderen"
PROJECT_ARGOCD_DELETE = "ArgoCD-resources van het project verwijderen"
NAMESPACES_DELETE = "Namespaces verwijderen"
NAMESPACE_DELETE = "Namespace verwijderen"
SUBDOMAINS_CLEANUP = "Subdomeinen opruimen"
SUBDOMAIN_CLEANUP = "Subdomein opruimen"
ARGOCD_APP_DELETE = "ArgoCD-toepassing verwijderen"
SERVICES_CLEANUP = "Services opruimen"
MANIFESTS_DELETE = "Manifesten verwijderen"
DEPLOYMENT_REMOVE_FROM_PROJECT_FILE = "Deployment uit het projectbestand verwijderen"
DELETE_VERIFY = "Controleren dat alles verwijderd is"

# ----------------------------------------------------------------------
# Sleep mode
# ----------------------------------------------------------------------
SLEEP_HEADING = "Deployment in slaapstand zetten"
SLEEP_DONE = "Deployment slaapt"
WAKE_HEADING = "Deployment wekken"
WAKE_DONE = "Deployment is gewekt"
SLEEP_STATE_COMMIT = "Slaaptoestand vastleggen in git"
WAKE_STATE_COMMIT = "Wektoestand vastleggen in git"
WAKE_STATE_FINISH = "Wektoestand afronden in git"
WAIT_FOR_APP = "Wachten tot de applicatie opgestart is"
SLEEP_OUT_OF_SCOPE = "Slaapstand geldt niet voor deze deployment, er is niets gewijzigd"
PROJECT_FILE_CHANGED_CONCURRENTLY = "Het projectbestand werd gelijktijdig gewijzigd"


def sleep_no_change(state: str) -> str:
    return f"Geen wijziging nodig, de deployment is al {state}"


def deployment_not_in_project(deployment_name: str) -> str:
    return f"Deployment '{deployment_name}' bestaat niet in dit project"


# ----------------------------------------------------------------------
# Waiting on another task
# ----------------------------------------------------------------------
def waiting_for_task(task_type: str, subject: str) -> str:
    return f"Wacht op {task_type} ({subject})"


WAITING_WHOLE_PROJECT = "hele project"
