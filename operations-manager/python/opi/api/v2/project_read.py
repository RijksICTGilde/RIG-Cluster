"""Reading a project back out of its project file (RC-61).

The v2 API could change a project but not show one: there was no way to ask which
services it uses, what its components look like, or what a deployment runs, without
knowing the answer already. This module is the read side of that, in three layers that
build on one another:

* :func:`collect_project_services` -- which services this project uses, per layer;
* :func:`build_component_details` -- the component definitions;
* the whole-project view, which is those two plus the existing deployment reader and
  ``pending-rollout``, composed in the router without any data logic of its own.

Two rules run through all of it.

**The project file is the source, not the cluster.** Everything here reads what
``get_project_store()`` holds. A change saved with ``rollout=false`` is in here and not
on the cluster, which is why every response carries ``pending_rollout``: without it the
answer describes the file and silently claims to describe what runs.

**Nothing decrypted leaves this module.** Reading env-var *names* means decrypting the
block they live in, so the risk is real and it is handled per kind of content rather
than by one blanket filter: values never ship, alias values ship only when they were
stored in plain text, an attachment's coupling ships but its content never does, and any
value stored as an encrypted or ``plain:``-marked secret is replaced by ``***``.
"""

from __future__ import annotations

import logging
from typing import Any

from opi.services.catalog.base import ConfigLayer
from opi.services.project_env_vars import read_user_env_vars
from opi.services.services import service_entry_config, service_entry_data, service_entry_name
from opi.utils.age import carries_encrypted_value
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

#: What replaces a value that must not be shown.
REDACTED = "***"

#: Prefix marking a deliberately unencrypted password. Not an encrypted value, still a
#: secret, so it is redacted here too -- see ``carries_encrypted_value``.
PLAIN_PREFIX = "plain:"

#: The service whose component config is the attachment couplings.
ATTACHMENTS_SERVICE = "attachments"


def redact_secrets(value: Any) -> Any:
    """Replace every stored secret in a config tree with ``***``.

    A service config is free-form (it is the service's own model), so this walks it
    rather than naming fields. Both stored forms of an encrypted value count, and so
    does the ``plain:`` marker: it means "deliberately not encrypted", which is a
    password all the same and has no business in terminal scrollback.
    """
    if isinstance(value, dict):
        return {key: redact_secrets(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    if isinstance(value, str) and (carries_encrypted_value(value) or value.strip().startswith(PLAIN_PREFIX)):
        return REDACTED
    return value


def _shows_value(value: str) -> bool:
    """Whether an alias value may be shown as stored.

    An alias is normally a reference to a platform variable (``$DATABASE_SERVER_HOST``),
    and hiding that would defeat the point of asking. The model allows a secret there
    too, and a secret is recognisable by how it is stored, so that is the test.
    """
    return not carries_encrypted_value(value) and not value.strip().startswith(PLAIN_PREFIX)


class ServiceUsage(BaseModel):
    """One place a service is used, and what it is configured with there."""

    target: ConfigLayer = Field(
        ...,
        description="The layer this usage sits on: project, component, deployment or deployment-component.",
    )
    component: str | None = Field(default=None, description="Component this usage belongs to, if any.")
    deployment: str | None = Field(default=None, description="Deployment this usage belongs to, if any.")
    config: Any = Field(
        default=None,
        description=(
            "The service's own config at this usage, with stored secrets replaced by '***'. "
            "Null means the service is selected here without configuration, which still means it is on."
        ),
    )


class ProjectServiceUsages(BaseModel):
    """One service, and every place the project uses it."""

    name: str = Field(..., description="Service identifier, as used in the service endpoints.")
    usages: list[ServiceUsage] = Field(..., description="Every place this project uses the service.")


class ComponentAttachment(BaseModel):
    """One attachment coupling: which catalog attachment, and how the component gets it.

    The coupling only. An attachment's content lives in the project's catalog and is
    never part of a read response.
    """

    reference: str = Field(..., description="Id of the attachment in the project's catalog.")
    provide_as: str | None = Field(
        default=None, alias="provide-as", description="How the component receives it: 'file' or 'env-var'."
    )
    path: str | None = Field(default=None, description="Where the attachment is mounted, when provided as a file.")
    env_name: str | None = Field(
        default=None, alias="env-name", description="Variable holding the content, when provided as an env-var."
    )

    model_config = {"populate_by_name": True}


class ComponentDetail(BaseModel):
    """A component definition as it stands in the project file."""

    name: str = Field(..., description="Component name, as referenced by deployments.")
    type: str | None = Field(default=None, description="Component type, e.g. 'single' or 'frontend'.")
    ports: dict[str, list[int]] = Field(
        default_factory=dict, description="Inbound and outbound ports declared for this component."
    )
    path: Any = Field(default=None, description="Path routing for this component, in the form the project file uses.")
    resources: dict[str, Any] = Field(default_factory=dict, description="CPU and memory requests and limits.")
    services: list[str] = Field(
        default_factory=list,
        description=(
            "Services bound to this component, by name. A cross-reference only: the layered service "
            "list of GET /projects/{project_name}/services is the single authority on configuration, "
            "because only there is it visible whether a setting applies to the project, this component, "
            "a deployment or one component within a deployment."
        ),
    )
    env_var_names: list[str] | None = Field(
        default=None,
        description=(
            "Names of the component's own environment variables, sorted. Values are never returned. "
            "Null means the stored variables could not be read, which is not the same as having none."
        ),
    )
    aliases: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Alias name -> what it points at. A value stored as a secret is returned as '***'; a plain "
            "reference such as $DATABASE_SERVER_HOST is returned as stored."
        ),
    )
    attachments: list[ComponentAttachment] = Field(
        default_factory=list, description="Attachment couplings for this component. Never the file contents."
    )


def _usage(target: ConfigLayer, entry: Any, **ids: str | None) -> ServiceUsage:
    """Build one usage record from a services-list entry.

    The ``data`` of a definition is dropped before anything else happens. On the legacy
    single-key form ``service_entry_config`` falls back to the whole entry body, and for
    a DEFINE-side entry that body IS the definition -- for the attachments catalog, the
    base64 content of every uploaded file. A definition is not configuration, and its
    content has no place in any read response.
    """
    config = service_entry_config(entry)
    if service_entry_data(entry) is not None and isinstance(config, dict) and "data" in config:
        config = {key: value for key, value in config.items() if key != "data"} or None
    return ServiceUsage(target=target, config=redact_secrets(config) if config is not None else None, **ids)


def collect_project_services(project_data: dict[str, Any]) -> list[ProjectServiceUsages]:
    """Which services this project uses, per layer, with config where there is any.

    Deliberately a different rule than ``_collect_service_config`` in the router, which
    answers "what is this service configured with" and therefore skips a bare selection.
    Here the question is "which services does this project use", and a bare
    ``- publish-on-web`` is the answer to it: the service is on. Reporting it as absent
    would be the one thing this endpoint exists to prevent.

    The four layers stay apart, and every usage carries the component or deployment it
    belongs to. Flattening them into one entry per service would lose the only thing that
    makes the answer usable: whether ``tls: standard`` holds for the whole project or for
    a single component. This layered list is the authority on service configuration; the
    plain name list on a component is a cross-reference and carries no config of its own.
    """
    found: dict[str, list[ServiceUsage]] = {}

    def add(services: Any, target: ConfigLayer, **ids: str | None) -> None:
        for entry in services or []:
            name = service_entry_name(entry)
            if name is None:
                logger.warning(f"Skipping unrecognisable services entry at {target.value}: {type(entry).__name__}")
                continue
            found.setdefault(name, []).append(_usage(target, entry, **ids))

    add(project_data.get("services"), ConfigLayer.PROJECT)
    for component in project_data.get("components", []):
        add(component.get("services"), ConfigLayer.COMPONENT, component=component.get("name"))
    for deployment in project_data.get("deployments", []):
        add(deployment.get("services"), ConfigLayer.DEPLOYMENT, deployment=deployment.get("name"))
        for dep_component in deployment.get("components", []):
            add(
                dep_component.get("services"),
                ConfigLayer.DEPLOYMENT_COMPONENT,
                deployment=deployment.get("name"),
                component=dep_component.get("reference"),
            )

    return [ProjectServiceUsages(name=name, usages=found[name]) for name in sorted(found)]


def _component_attachments(component: dict[str, Any]) -> list[ComponentAttachment]:
    """The attachment couplings a component declares, without any file content."""
    for entry in component.get("services") or []:
        if service_entry_name(entry) != ATTACHMENTS_SERVICE:
            continue
        config = service_entry_config(entry)
        if not isinstance(config, list):
            return []
        return [
            ComponentAttachment(
                reference=use.get("reference", ""),
                **{
                    "provide-as": use.get("provide-as"),
                    "path": use.get("path"),
                    "env-name": use.get("env-name"),
                },
            )
            for use in config
            if isinstance(use, dict)
        ]
    return []


def _component_aliases(component: dict[str, Any]) -> dict[str, str]:
    """A component's aliases, with secret-stored values replaced by ``***``."""
    aliases = component.get("aliases")
    if not isinstance(aliases, dict):
        return {}
    return {str(key): (str(value) if _shows_value(str(value)) else REDACTED) for key, value in aliases.items()}


async def build_component_details(
    project_data: dict[str, Any],
    project_private_key: str,
) -> list[ComponentDetail]:
    """The project's component definitions, with env-var names but never their values."""
    details: list[ComponentDetail] = []
    for component in project_data.get("components", []):
        name = component.get("name", "")
        env_vars = await read_user_env_vars(
            component.get("user-env-vars"),
            project_private_key,
            where=f"component '{name}'",
        )
        ports = component.get("ports") or {}
        details.append(
            ComponentDetail(
                name=name,
                type=component.get("type"),
                ports={key: list(value or []) for key, value in ports.items()} if isinstance(ports, dict) else {},
                path=component.get("path"),
                resources=dict(component.get("resources") or {}),
                services=[
                    service_name
                    for service_name in (service_entry_name(entry) for entry in component.get("services") or [])
                    if service_name is not None
                ],
                # Names only. The values were just decrypted to get here and go no further.
                env_var_names=sorted(env_vars) if env_vars is not None else None,
                aliases=_component_aliases(component),
                attachments=_component_attachments(component),
            )
        )
    return details
