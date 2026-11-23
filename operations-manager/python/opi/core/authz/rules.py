"""The rule register: what every action opens, and for whom.

This file reads from top to bottom as the permission matrix; the page at "/rollen" shows
exactly what is here. An action without a rule here makes the application fail at startup,
so a new route cannot be quietly unprotected.

The roles and scopes are in :mod:`opi.core.authz.engine`.
"""

from __future__ import annotations

from opi.core.authz.actions import Action
from opi.core.authz.engine import (
    COMPONENT_IN_PRODUCTION,
    COMPONENT_NOT_IN_PRODUCTION,
    HUMAN_ONLY,
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

# Row 1 of the matrix: a viewer reads everything of the project, except the values of
# environment variables and secrets. Those sit behind env-vars.manage and the project key
# respectively.
#
# developer-ci reads as viewer (INCLUDES). ops-ci reads only what a pipeline asks for
# after a deploy: the task outcome, the deployment's URLs and whether changes are still
# waiting for a rollout; it is the role that reaches production.
_CI_READS = frozenset({Action.PROJECT_READ, Action.DEPLOYMENT_READ, Action.TASK_READ})

for _action, _label, _section in (
    (Action.PROJECT_READ, "Project bekijken", PROJECT),
    (Action.PROJECT_LIST, "Projectenoverzicht bekijken", PROJECT),
    (Action.DEPLOYMENT_READ, "Deployment bekijken", DEPLOYMENTS),
    (Action.COMPONENT_READ, "Component bekijken", COMPONENTS),
    (Action.SERVICE_READ, "Service bekijken", SERVICES),
    (Action.DATABASE_SCHEMA_READ, "Databaseschema bekijken", DATA),
    (Action.BACKUP_READ, "Back-ups bekijken", BACKUPS),
    (Action.RESTORE_READ, "Herstelacties bekijken", BACKUPS),
    (Action.LOGS_READ, "Logs bekijken", OBSERVATION),
    (Action.SUBDOMAIN_READ, "Subdomein bekijken", OBSERVATION),
    (Action.CLUSTER_READ, "Clustergegevens bekijken", OBSERVATION),
    (Action.TASK_READ, "Taken bekijken", TASKS),
    (Action.SLEEP_READ, "Slaapstand bekijken", TASKS),
    (Action.TOKEN_READ, "Tokens bekijken", MEMBERS),
):
    _ci = [Grant(Role.OPS_CI)] if _action in _CI_READS else []
    rule(_action, label=_label, section=_section, read=True, grants=[Grant(Role.VIEWER), *_ci])


# Rows 2 through 7. A developer builds on everything that is not production; ops does the
# same on production. The CI roles do not stack on each other and carry only what a pipeline
# needs (B7).

for _action, _label in (
    (Action.DEPLOYMENT_WRITE, "Deployment aanmaken of wijzigen"),
    (Action.DEPLOYMENT_REFRESH, "Deployment verwerken"),
):
    rule(
        _action,
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

for _action, _label, _section in (
    (Action.DEPLOYMENT_IMAGE_REPLACE, "Image vervangen", DEPLOYMENTS),
    (Action.IMAGE_PUSH, "Image pushen", DATA),
):
    rule(
        _action,
        label=_label,
        section=_section,
        # ops-ci deliberately without a step-up, and on both scopes: it covers no other
        # role, and without a step-up a pipeline can do it at all (design 5.3).
        grants=[
            Grant(Role.DEVELOPER, NON_PRODUCTION),
            Grant(Role.DEVELOPER_CI, NON_PRODUCTION),
            Grant(Role.OPS_CI, NON_PRODUCTION),
            Grant(Role.OPS, PRODUCTION),
            Grant(Role.OPS_CI, PRODUCTION),
        ],
    )

# On the COMPONENT scopes and not on a deployment's, unlike what the row in design section 4
# says: the env-vars hang off the component, and the route that opens them has no deployment
# in its path. A deployment scope reads that route as non-production, and the developer grant
# then shows the values of a component that only stands on production.
# Since RC-481 this action opens the window and no longer writes: saving the values weighs
# SERVICE_WRITE_VALUES on the project, like the values endpoints.
rule(
    Action.ENV_VARS_MANAGE,
    label="Env-vars bekijken",
    section=DEPLOYMENTS,
    grants=[
        Grant(Role.DEVELOPER, COMPONENT_NOT_IN_PRODUCTION, requires=STEP_UP),
        Grant(Role.OPS, COMPONENT_IN_PRODUCTION, requires=STEP_UP),
    ],
)

# The SOURCE of a clone, next to the deployment being written. Cloning FROM production is
# ops, wherever it goes: copying production data to non-production is not something we leave
# to a developer or a pipeline (RC-392 security review).
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

# Changing the host name of an EXISTING deployment is always ops. The domain approval holds
# per project and the format ``subdomain`` is ``{subdomain}.{domain}``, so two deployments of
# the same project can form the same host name; whoever may move the host name of a running
# deployment can claim the one of production (RC-392 security review). A NEW deployment gets
# it along out of deployment.write.
rule(
    Action.DEPLOYMENT_DOMAIN_SET,
    label="Hostnaam van een bestaande deployment wijzigen",
    section=DEPLOYMENTS,
    grants=[Grant(Role.OPS)],
)

# On or off production sits with ops plus a step-up, because whoever takes a deployment off
# production sidesteps every production rule with one click (design section 4). A transition
# between the other stages is writing a deployment.
rule(
    Action.ENVIRONMENT_SET,
    label="OTAP-markering van of naar productie zetten",
    section=DEPLOYMENTS,
    grants=[Grant(Role.OPS, requires=STEP_UP)],
)


# Rows 10 and 11 (B5): a change touching the whole project asks for ops as soon as ONE
# deployment of that project stands on production.

for _action, _label, _section in (
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
        _action,
        label=_label,
        section=_section,
        grants=[
            Grant(Role.DEVELOPER, PROJECT_WITHOUT_PRODUCTION),
            Grant(Role.OPS, PROJECT_WITH_PRODUCTION),
        ],
    )

# Rows 12 and 13: a component weighs whether a PRODUCTION deployment uses it, not whether
# the project has production somewhere.
rule(
    Action.COMPONENT_DELETE,
    label="Component verwijderen",
    section=COMPONENTS,
    grants=[
        Grant(Role.DEVELOPER, COMPONENT_NOT_IN_PRODUCTION),
        Grant(Role.OPS, COMPONENT_IN_PRODUCTION, requires=STEP_UP),
    ],
)


# Rows 14 and 15: a job, the database console, a restore and whatever else happens on ONE
# deployment. Non-production is developer, production is ops.

for _action, _label, _section in (
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
        _action,
        label=_label,
        section=_section,
        grants=[Grant(Role.DEVELOPER, NON_PRODUCTION), Grant(Role.OPS, PRODUCTION)],
    )


# Rows 17 through 19. Admin is user administration and not a top rung (B1): it reads the
# project and manages who may come in, but does not build on it.

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
# Not a read action: whoever reads the project key out has everything that key opens. Without
# a step-up, because the CLI reads it on every list request.
rule(Action.PROJECT_KEY_READ, label="Projectsleutel lezen", section=MEMBERS, grants=[Grant(Role.ADMIN)])
rule(Action.AUDIT_READ, label="De audit van dit project lezen", section=MEMBERS, read=True, grants=[Grant(Role.ADMIN)])


# Not a project permission: these routes do not work on a project, so there is no role to
# weigh. They carry an action because a route without an action would be quietly unguarded,
# and so the audit can name them.

rule(Action.PORTAL_READ, label="Portaalpagina's lezen", section=PORTAL, read=True, audience=Audience.SIGNED_IN)
rule(Action.WIZARD_USE, label="De aanmaakwizard gebruiken", section=PORTAL, audience=Audience.SIGNED_IN)
rule(Action.PROJECT_CREATE, label="Een project aanmaken", section=PORTAL, audience=Audience.SIGNED_IN)
rule(Action.ACCOUNT_OTP, label="De eigen 2-factor inrichten", section=PORTAL, audience=Audience.SIGNED_IN)
rule(Action.STEP_UP_VERIFY, label="Een step-up-code bevestigen", section=PORTAL, audience=Audience.SIGNED_IN)
# Humans only; service accounts lack 2FA. Read action: viewers can activate to access
# env-vars. One viewer grant is enough: INCLUDES puts developer, ops, admin and
# developer-ci on viewer.
rule(
    Action.TOKEN_STEP_UP,
    label="Een step-up activeren met een persoonlijk token",
    section=PORTAL,
    read=True,
    grants=[Grant(Role.VIEWER, requires=HUMAN_ONLY)],
)
rule(Action.TOOLS_USE, label="Het versleutelgereedschap gebruiken", section=PORTAL, audience=Audience.SIGNED_IN)


# A platform administrator only, enforced by ``require_platform_admin`` or by the platform
# administrator token. No project role opens these, not even an admin's. An audience of its
# own and not a seventh role, so the matrix keeps the six columns of design section 4.

for _action, _label in (
    (Action.PLATFORM_CLEANUP, "Opruiming en verwijdermarkeringen"),
    (Action.PLATFORM_RECONCILE, "Reconciliatie van het platform"),
    (Action.PLATFORM_USERS, "Gebruikersbeheer"),
    (Action.PLATFORM_APPROVALS, "Aanvragen beoordelen"),
    (Action.PLATFORM_USAGE, "Gebruik en kosten"),
    (Action.PLATFORM_SERVICES, "De toestand van de gedeelde services"),
    (Action.PLATFORM_METRICS, "De metrics-explorer"),
    (Action.PLATFORM_FEDERATION, "Federatie met andere clusters"),
    (Action.PLATFORM_BACKUP, "De back-uptoestand van het platform"),
    (Action.PLATFORM_PROJECT_KEY, "De projectsleutel van een project vervangen"),
    (Action.PLATFORM_SSO_OUTAGE, "De storingsstand van SSO Rijk aan- en uitzetten"),
):
    rule(_action, label=_label, section=PLATFORM, audience=Audience.PLATFORM_ADMIN)


def check_register() -> None:
    """The checks of design 5.4 that do not fit in :func:`rule` itself.

    Runs at startup, next to the route check. What fails here is an error in this file, and
    then not starting is better than a role that quietly opens nothing.

    Raises ``RuleError`` when a role appears in no grant at all, or when a CI role has only
    grants that ask for a human, and therefore opens nothing for a pipeline.
    """
    used: set[Role] = set()
    reachable: set[Role] = set()
    for registered in all_rules():
        for grant in registered.grants:
            used.add(grant.role)
            if not grant.needs_human:
                reachable.add(grant.role)

    unused = sorted(role.value for role in Role if role not in used)
    if unused:
        raise RuleError(f"deze rollen komen in geen enkele grant voor: {', '.join(unused)}")

    dead_ci = sorted(role.value for role in Role if role.value.endswith("-ci") and role not in reachable)
    if dead_ci:
        raise RuleError(
            f"deze CI-rollen hebben alleen grants die een mens vragen en openen dus niets: {', '.join(dead_ci)}"
        )
