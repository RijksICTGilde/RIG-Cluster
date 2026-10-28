"""Het regelregister: wat elke action opent, en voor wie.

Dit bestand lees je van boven naar beneden als de rechtenmatrix; de pagina op
``/admin/rollen`` toont precies wat hier staat. Een action zonder regel hier laat de
applicatie bij het opstarten falen, dus een nieuwe route kan niet stil onbeschermd zijn.

De rollen en scopes staan in :mod:`opi.core.authz.engine`.
"""

from __future__ import annotations

from opi.core.authz.actions import Action
from opi.core.authz.engine import (
    COMPONENT_IN_PRODUCTION,
    COMPONENT_NOT_IN_PRODUCTION,
    NON_PRODUCTION,
    PRODUCTION,
    PROJECT_WITH_PRODUCTION,
    PROJECT_WITHOUT_PRODUCTION,
    STEP_UP,
    Audience,
    Grant,
    Role,
    RuleError,
    all_rules,
    rule,
)

PROJECT = "Project"
DEPLOYMENTS = "Deployments"
COMPONENTS = "Componenten"
SERVICES = "Services"
DATA = "Data"
BACKUPS = "Back-up en herstel"
OBSERVATION = "Observatie en resources"
TASKS = "Taken"
MEMBERS = "Leden en tokens"
OPERATIONS = "Operationeel"
PORTAL = "Het portaal"
PLATFORM = "Platformbeheer"

# --- Lezen -------------------------------------------------------------------
#
# Rij 1 van de matrix: een viewer leest alles van het project, behalve de waarden van
# omgevingsvariabelen en secrets. Die staan achter env-vars.manage respectievelijk de
# projectsleutel.

for _actie, _label, _sectie in (
    (Action.PROJECT_READ, "Project lezen", PROJECT),
    (Action.PROJECT_LIST, "Projecten lijsten", PROJECT),
    (Action.DEPLOYMENT_READ, "Deployment lezen", DEPLOYMENTS),
    (Action.COMPONENT_READ, "Component lezen", COMPONENTS),
    (Action.SERVICE_READ, "Service lezen", SERVICES),
    (Action.DATABASE_SCHEMA_READ, "Databaseschema lezen", DATA),
    (Action.BACKUP_READ, "Back-ups lezen", BACKUPS),
    (Action.RESTORE_READ, "Herstel lezen", BACKUPS),
    (Action.LOGS_READ, "Logs lezen", OBSERVATION),
    (Action.SUBDOMAIN_READ, "Subdomein lezen", OBSERVATION),
    (Action.CLUSTER_READ, "Cluster lezen", OBSERVATION),
    (Action.TASK_READ, "Taak lezen", TASKS),
    (Action.SLEEP_READ, "Slaapstand lezen", TASKS),
    (Action.TOKEN_READ, "Tokens lezen", MEMBERS),
):
    rule(_actie, label=_label, section=_sectie, read=True, grants=[Grant(Role.VIEWER)])


# --- Deployments -------------------------------------------------------------
#
# Rij 2 tot en met 7. Een developer bouwt op alles wat niet productie is; ops doet
# hetzelfde op productie. De CI-rollen stapelen niet op elkaar en dragen alleen wat een
# pijplijn nodig heeft (B7).

for _actie, _label in (
    (Action.DEPLOYMENT_WRITE, "Deployment aanmaken of wijzigen"),
    (Action.DEPLOYMENT_REFRESH, "Deployment verwerken"),
):
    rule(
        _actie,
        label=_label,
        section=DEPLOYMENTS,
        grants=[
            Grant(Role.DEVELOPER, NON_PRODUCTION),
            Grant(Role.DEVELOPER_CI, NON_PRODUCTION),
            Grant(Role.OPS, PRODUCTION),
        ],
    )

rule(
    Action.DEPLOYMENT_DELETE,
    label="Deployment verwijderen",
    section=DEPLOYMENTS,
    grants=[
        Grant(Role.DEVELOPER, NON_PRODUCTION),
        Grant(Role.DEVELOPER_CI, NON_PRODUCTION),
        Grant(Role.OPS, PRODUCTION, requires=STEP_UP),
    ],
)

for _actie, _label, _sectie in (
    (Action.DEPLOYMENT_IMAGE_REPLACE, "Image vervangen", DEPLOYMENTS),
    (Action.IMAGE_PUSH, "Image pushen", DATA),
):
    rule(
        _actie,
        label=_label,
        section=_sectie,
        # ops-ci bewust zonder step-up, en op allebei de scopes: hij omvat geen andere
        # rol, en zonder step-up kan een pijplijn het uberhaupt (ontwerp 5.3).
        grants=[
            Grant(Role.DEVELOPER, NON_PRODUCTION),
            Grant(Role.DEVELOPER_CI, NON_PRODUCTION),
            Grant(Role.OPS_CI, NON_PRODUCTION),
            Grant(Role.OPS, PRODUCTION),
            Grant(Role.OPS_CI, PRODUCTION),
        ],
    )

# Op de COMPONENTscopes en niet op die van een deployment, anders dan de rij in ontwerp
# paragraaf 4 zegt: de env-vars hangen aan het component, en de route die ze bewerkt heeft
# geen deployment in het pad. Met een deploymentscope las die route als niet-productie, en
# dan opende de developer-grant de waarden van een component dat alleen op productie staat
# (RC-392 securityreview).
rule(
    Action.ENV_VARS_MANAGE,
    label="Env-vars bekijken en wijzigen",
    section=DEPLOYMENTS,
    grants=[
        Grant(Role.DEVELOPER, COMPONENT_NOT_IN_PRODUCTION, requires=STEP_UP),
        Grant(Role.OPS, COMPONENT_IN_PRODUCTION, requires=STEP_UP),
    ],
)

# De BRON van een kloon, naast de deployment die geschreven wordt. Klonen VANAF productie
# is ops, ongeacht waar het naartoe gaat: productiedata naar niet-productie kopieren laten
# we niet aan een developer of een pijplijn over (RC-392 securityreview).
rule(
    Action.DEPLOYMENT_CLONE_FROM,
    label="Klonen vanaf een deployment",
    section=DEPLOYMENTS,
    grants=[
        Grant(Role.DEVELOPER, NON_PRODUCTION),
        Grant(Role.DEVELOPER_CI, NON_PRODUCTION),
        Grant(Role.OPS, PRODUCTION),
    ],
)

# De hostnaam van een BESTAANDE deployment wijzigen is altijd ops. De domeingoedkeuring
# geldt per project en het format ``subdomain`` is ``{subdomain}.{domain}``, dus twee
# deployments van hetzelfde project kunnen dezelfde hostnaam vormen; wie de hostnaam van
# een draaiende deployment mag verzetten kan die van productie opeisen (RC-392
# securityreview). Een NIEUWE deployment krijgt ze mee uit deployment.write.
rule(
    Action.DEPLOYMENT_DOMAIN_SET,
    label="Hostnaam van een bestaande deployment wijzigen",
    section=DEPLOYMENTS,
    grants=[Grant(Role.OPS)],
)

# De markering staat bij ops met een step-up, omdat wie hem mag verlagen elke
# productieregel met een klik omzeilt (ontwerp paragraaf 4).
rule(
    Action.ENVIRONMENT_SET,
    label="OTAP-markering zetten of verlagen",
    section=DEPLOYMENTS,
    grants=[Grant(Role.OPS, requires=STEP_UP)],
)


# --- Op projectniveau ----------------------------------------------------------
#
# Rij 10 en 11 (B5): een wijziging die het hele project raakt vraagt ops zodra EEN
# deployment van dat project op productie staat.

for _actie, _label, _sectie in (
    (Action.PROJECT_EDIT, "Het projectbestand bewerken", PROJECT),
    (Action.PROJECT_REFRESH, "Project verwerken", PROJECT),
    (Action.COMPONENT_ADD, "Component toevoegen", COMPONENTS),
    (Action.COMPONENT_UPDATE, "Component wijzigen", COMPONENTS),
    (Action.COMPONENT_LINK, "Component aan een deployment koppelen", COMPONENTS),
    (Action.SERVICE_ADD, "Service toevoegen", SERVICES),
    (Action.SERVICE_CONFIGURE, "Service configureren", SERVICES),
    (Action.SERVICE_WRITE_VALUES, "Waarden van een service schrijven", SERVICES),
    (Action.SERVICE_ACTION, "Actie van een service uitvoeren", SERVICES),
    (Action.REGISTRY_ADD, "Registry toevoegen", DATA),
    (Action.ATTACHMENT_MANAGE, "Bijlagen beheren", PROJECT),
):
    rule(
        _actie,
        label=_label,
        section=_sectie,
        grants=[
            Grant(Role.DEVELOPER, PROJECT_WITHOUT_PRODUCTION),
            Grant(Role.OPS, PROJECT_WITH_PRODUCTION),
        ],
    )

# Rij 12 en 13: een component weegt of een PRODUCTIEdeployment hem gebruikt, niet of het
# project ergens productie heeft.
rule(
    Action.COMPONENT_DELETE,
    label="Component verwijderen",
    section=COMPONENTS,
    grants=[
        Grant(Role.DEVELOPER, COMPONENT_NOT_IN_PRODUCTION),
        Grant(Role.OPS, COMPONENT_IN_PRODUCTION, requires=STEP_UP),
    ],
)


# --- Operationeel --------------------------------------------------------------
#
# Rij 14 en 15: job, databaseconsole, terugzetten en wat er verder op EEN deployment
# gebeurt. Niet-productie is developer, productie is ops.

for _actie, _label, _sectie in (
    (Action.JOB_RUN, "Een job draaien", OPERATIONS),
    (Action.DATABASE_CONSOLE, "De databaseconsole openen", OPERATIONS),
    (Action.RESTORE_RUN, "Terugzetten", BACKUPS),
    (Action.BACKUP_CREATE, "Back-up maken", BACKUPS),
    (Action.BACKUP_DELETE, "Back-up verwijderen", BACKUPS),
    (Action.DATABASE_CLONE, "Database klonen", DATA),
    (Action.DATABASE_SCHEMA_MANAGE, "Databaseschema beheren", DATA),
    (Action.BUCKET_CLONE, "Bucket klonen", DATA),
    (Action.CLONE_VALIDATE, "Kloon valideren", DATA),
    (Action.RESOURCES_ADJUST, "Resources bijstellen", OBSERVATION),
    (Action.RESOURCES_CLEANUP, "Resources opschonen", OBSERVATION),
    (Action.TASK_CANCEL, "Taak annuleren", TASKS),
    (Action.SLEEP_WAKE, "Wekken uit de slaapstand", TASKS),
):
    rule(
        _actie,
        label=_label,
        section=_sectie,
        grants=[Grant(Role.DEVELOPER, NON_PRODUCTION), Grant(Role.OPS, PRODUCTION)],
    )


# --- Leden, tokens en het project zelf ------------------------------------------
#
# Rij 17 tot en met 19. Admin is gebruikersbeheer en geen bovenste trede (B1): hij leest
# het project en beheert wie erbij mag, maar bouwt er niet aan.

rule(
    Action.MEMBER_MANAGE,
    label="Leden toevoegen, verwijderen, rollen wijzigen",
    section=MEMBERS,
    grants=[Grant(Role.ADMIN, requires=STEP_UP)],
)
rule(
    Action.TOKEN_MANAGE,
    label="Tokens en service accounts beheren",
    section=MEMBERS,
    grants=[Grant(Role.ADMIN, requires=STEP_UP)],
)
rule(
    Action.PROJECT_DELETE,
    label="Project verwijderen",
    section=PROJECT,
    grants=[Grant(Role.ADMIN, requires=STEP_UP)],
)
# Geen leesactie: wie de projectsleutel uitleest heeft daarmee alles wat die sleutel
# opent. Zonder step-up, want de CLI leest hem bij elke lijstvraag op.
rule(Action.PROJECT_KEY_READ, label="Projectsleutel lezen", section=MEMBERS, grants=[Grant(Role.ADMIN)])
rule(Action.AUDIT_READ, label="De audit van dit project lezen", section=MEMBERS, read=True, grants=[Grant(Role.ADMIN)])


# --- Het portaal ------------------------------------------------------------
#
# Geen projectrecht: deze routes werken niet op een project, dus er is geen rol om te
# wegen. Ze dragen een action omdat een route zonder action stil onbewaakt zou zijn, en
# zodat de audit ze kan noemen.

rule(Action.PORTAL_READ, label="Portaalpagina's lezen", section=PORTAL, read=True, audience=Audience.SIGNED_IN)
rule(Action.WIZARD_USE, label="De aanmaakwizard gebruiken", section=PORTAL, audience=Audience.SIGNED_IN)
rule(Action.PROJECT_CREATE, label="Een project aanmaken", section=PORTAL, audience=Audience.SIGNED_IN)
rule(Action.ACCOUNT_OTP, label="De eigen tweede factor inrichten", section=PORTAL, audience=Audience.SIGNED_IN)
rule(Action.STEP_UP_VERIFY, label="Een step-up-code bevestigen", section=PORTAL, audience=Audience.SIGNED_IN)
rule(Action.TOOLS_USE, label="Het versleutelgereedschap gebruiken", section=PORTAL, audience=Audience.SIGNED_IN)


# --- Platformbeheer ---------------------------------------------------------
#
# Alleen een platformbeheerder, afgedwongen door ``require_platform_admin`` of door het
# platformbeheerderstoken. Geen projectrol opent deze, ook niet die van een admin. Een
# eigen publiek en geen zevende rol, zodat de matrix de zes kolommen van ontwerp
# paragraaf 4 houdt.

for _actie, _label in (
    (Action.PLATFORM_CLEANUP, "Opruiming en verwijdermarkeringen"),
    (Action.PLATFORM_RECONCILE, "Reconciliatie van het platform"),
    (Action.PLATFORM_USERS, "Gebruikersbeheer"),
    (Action.PLATFORM_APPROVALS, "Aanvragen beoordelen"),
    (Action.PLATFORM_USAGE, "Gebruik en kosten"),
    (Action.PLATFORM_SERVICES, "De toestand van de gedeelde services"),
    (Action.PLATFORM_ROLES, "De rolmatrix"),
    (Action.PLATFORM_METRICS, "De metrics-explorer"),
    (Action.PLATFORM_FEDERATION, "Federatie met andere clusters"),
    (Action.PLATFORM_BACKUP, "De back-uptoestand van het platform"),
    (Action.PLATFORM_PROJECT_KEY, "De projectsleutel van een project vervangen"),
):
    rule(_actie, label=_label, section=PLATFORM, audience=Audience.PLATFORM_ADMIN)


def check_register() -> None:
    """De controles uit ontwerp 5.4 die niet in :func:`rule` zelf passen.

    Draait bij het opstarten, naast de routecontrole. Wat hier faalt is een fout in dit
    bestand, en dan is niet starten beter dan een rol die stil niets opent.

    Raises:
        RuleError: Als een rol in geen enkele grant voorkomt, of als een CI-rol alleen
            grants heeft die een mens vragen (en dus voor een pijplijn niets opent).
    """
    gebruikt: set[Role] = set()
    bereikbaar: set[Role] = set()
    for regel in all_rules():
        for grant in regel.grants:
            gebruikt.add(grant.role)
            if not grant.needs_human:
                bereikbaar.add(grant.role)

    ongebruikt = sorted(rol.value for rol in Role if rol not in gebruikt)
    if ongebruikt:
        raise RuleError(f"deze rollen komen in geen enkele grant voor: {', '.join(ongebruikt)}")

    dode_ci = sorted(rol.value for rol in Role if rol.value.endswith("-ci") and rol not in bereikbaar)
    if dode_ci:
        raise RuleError(
            f"deze CI-rollen hebben alleen grants die een mens vragen en openen dus niets: {', '.join(dode_ci)}"
        )
