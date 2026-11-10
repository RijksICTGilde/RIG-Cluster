"""Validation and project-file generation shared by the creation paths.

Sits apart from the managers that use it to avoid a circular import: the wizard, the
creation API and the add-component API all reach these functions.
"""

import logging
from types import SimpleNamespace
from typing import Any

from fastapi import HTTPException
from ruamel.yaml.scalarstring import LiteralScalarString

from opi.core.config import settings
from opi.services import ServiceAdapter
from opi.services.catalog.publish_on_web.domain_config import DomainSetting, set_domain_setting
from opi.services.project_leden import hash_van, nieuw_token, vervaldatum
from opi.services.project_roles import CREATOR_ROLES, roles_from_value
from opi.services.project_service import get_project_service
from opi.services.schema_migration import PROJECT_API_ACCOUNT, PROJECT_API_ROLLEN
from opi.utils.age import (
    decrypt_password_smart_sync,
    encrypt_age_content,
    encrypt_to_base64_age_sync,
    generate_age_key_pair,
    parse_password_with_prefix,
    wrap_project_key,
)
from opi.utils.naming import ROOT_COMPONENT_FORMAT_IDS
from opi.utils.yaml_util import YAMLError, dump_yaml_to_string, load_yaml_from_string, load_yaml_round_trip

logger = logging.getLogger(__name__)


class ComponentValidationError(ValueError):
    """Raised when component configuration validation fails."""


class ProjectApiKeyError(RuntimeError):
    """Raised when a generated project API key cannot be read back in plaintext."""


def _apply_web_address_settings(
    deployment_config: dict[str, Any],
    project_data: Any,
    deployment_name: str,
    root_component: str | None,
) -> None:
    """Write the web-address settings of a freshly created deployment (RC-60).

    Every write goes through ``set_domain_setting``, so the location is decided in one
    place (``catalog/publish_on_web/domain_config.py``) and creation cannot disagree with
    the migration about where a value belongs.

    Only settings the user actually chose are written: an absent setting means "inherit",
    and writing an empty one would turn that into an explicit empty value.
    """
    for setting, value in (
        (DomainSetting.SUBDOMAIN, getattr(project_data, "subdomain", None)),
        (DomainSetting.BASE_DOMAIN, getattr(project_data, "base_domain", None)),
        (DomainSetting.DOMAIN_FORMAT, getattr(project_data, "domain_format", None)),
        (DomainSetting.ISSUER, getattr(project_data, "issuer", None)),
        (DomainSetting.ROOT_COMPONENT, root_component),
    ):
        if value:
            set_domain_setting(deployment_config, setting, value)


def validate_root_component(
    root_component_name: str | None,
    deployment_component_names: list[str],
    domain_format: str | None = None,
) -> None:
    """Validate the root-component constraints on a deployment.

    root-component only applies when the deployment's domain setup exposes a root host: a
    domain-format whose template has a droppable ``{component}`` prefix
    (``ROOT_COMPONENT_FORMAT_IDS``). On any other format (the dash formats for instance) it
    is inert, so an inherited value, a clone copying the source's root-component, is
    ignored rather than rejected.

    Raises:
        ComponentValidationError: when root-component applies but names a component that
            is not part of this deployment.
    """
    if not root_component_name:
        return

    if domain_format not in ROOT_COMPONENT_FORMAT_IDS:
        return

    if root_component_name not in deployment_component_names:
        raise ComponentValidationError(
            f"root-component '{root_component_name}' is not a component in this deployment. "
            f"Valid components: {', '.join(deployment_component_names)}"
        )


def parse_aliases(aliases_str: str) -> dict[str, Any]:
    """Parse a YAML aliases string into a dictionary.

    Aliases let components reference platform-provided variables in ``$VARIABLE_NAME``
    syntax (``DATABASE_URL: $HOST:$PORT/$DB_NAME``). Valid YAML that is not a mapping comes
    back as an empty dict.

    Raises:
        ComponentValidationError: when the string is not valid YAML.
    """
    # Round-trip: without a public key the mapping is written back into the project file
    # unencrypted, and a scalar must keep the form it was typed in.
    try:
        aliases_dict = load_yaml_round_trip(aliases_str)
    except YAMLError as e:
        raise ComponentValidationError(f"Invalid aliases format: {e!s}") from e
    if aliases_dict and isinstance(aliases_dict, dict):
        return aliases_dict
    return {}


def apply_resource_limits(
    resources: dict,
    *,
    cpu_limit: str | None = None,
    memory_limit: str | None = None,
) -> None:
    """Write CPU/memory limits into the canonical nested resources form, in place.

    Manifest generation (``project_file_handler._resolve_resources``) reads
    ``resources.limits.*`` and ``resources.requests.*`` -- never the flat
    ``resources.cpu`` / ``resources.memory`` shorthand. The read-time fixup only
    migrates that shorthand into ``limits`` when the nested value is ABSENT, so
    writing the shorthand over an existing limit silently dropped the change.
    Writing the nested form directly makes a limit change take effect on the same
    commit.

    A memory request must not exceed the memory limit, so an existing request
    above the new limit is lowered to it (and an absent request is set to it).
    """
    from opi.services.resource_analyzer import parse_k8s_memory_to_mi

    if cpu_limit is None and memory_limit is None:
        return

    limits = resources.setdefault("limits", {})
    requests = resources.setdefault("requests", {})

    if cpu_limit is not None:
        limits["cpu"] = cpu_limit
    if memory_limit is not None:
        limits["memory"] = memory_limit
        existing = requests.get("memory")
        if existing is None or parse_k8s_memory_to_mi(str(existing)) > parse_k8s_memory_to_mi(memory_limit):
            requests["memory"] = memory_limit


async def build_component_config(
    name: str,
    component_type: str,
    port: int | None,
    path: str,
    services: list[str],
    rewrite: str | None = None,
    cpu_limit: str | None = None,
    memory_limit: str | None = None,
    env_vars: str | None = None,
    aliases: str | None = None,
    public_key: str | None = None,
    default_port: int | None = None,
    ports: list[int] | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """Build one component config, for project creation and the add-component API alike.

    Args:
        name: component name.
        component_type: "single", "frontend", "backend" and the like.
        port: inbound port, None for a background worker.
        path: ingress path.
        services: the component's services as strings.
        rewrite: the path the ingress rewrites the match to before the request reaches the
            container ("/" for a component listening on the root). Left out entirely when
            not given, which keeps the path unchanged.
        cpu_limit: CPU limit, "1" or "500m".
        memory_limit: memory limit, "256Mi" or "1Gi".
        env_vars: environment variables in KEY=value form; encrypted here.
        aliases: YAML string of alias definitions.
        public_key: the AGE public key the env vars are encrypted with.
        default_port: the port to fall back to when neither ports nor port is given.
        ports: inbound ports as a list; takes precedence over the single-port `port`.
        description: free text saying what the component does. Left out when empty.
    """
    # `ports` (array) takes precedence over the single-port `port` alias; fall back
    # to default_port (e.g. project creation) when neither is given.
    inbound_ports = ports or ([port] if port else ([default_port] if default_port else []))
    services_list = ServiceAdapter.build_component_service_entries(services)

    # A rewrite is only written when the caller asked for one. Absent means "pass the
    # path on unchanged", which is what a component that serves its own prefix needs;
    # an empty value would render as a rewrite to the root in the ingress snippet.
    path_entry: dict[str, str] = {"match": path}
    if rewrite:
        path_entry["rewrite"] = rewrite

    component_config: dict[str, Any] = {
        "name": name,
        **({"description": description} if description else {}),
        "type": component_type,
        "ports": {"inbound": inbound_ports, "outbound": [80, 443]},
        # Schema expects an array of component-path objects (see project_v2.json
        # $defs/component-path and the COMPONENT_PATH editable's [{match}] shape),
        # not a bare string. Emitting the string here made add-component/creation
        # produce files their own schema rejects at process time.
        "path": [path_entry],
        "services": services_list,
        "uses-components": [],
    }

    # Add resource limits if specified, in the canonical nested form. Deliberately NOT
    # through apply_user_resource_intent (RC-141): that path records the values as a wish
    # the tuner has to leave alone, and creation always carries both limits (the wizard
    # prefills 1 CPU / 256Mi, the API defaults to the same), so it would pin every new
    # component at its creation value. A value nobody chose is not a wish; the first real
    # edit of the component is, and that one does go through the intent path.
    # Nothing is lost by writing straight here: a component that does not exist yet has no
    # deployment override to clear and no history to carry.
    if cpu_limit or memory_limit:
        component_config["resources"] = {}
        apply_resource_limits(component_config["resources"], cpu_limit=cpu_limit, memory_limit=memory_limit)

    # Encrypt and add user env vars if provided
    if env_vars:
        if not public_key:
            logger.warning(f"Could not encrypt env_vars for component '{name}': no AGE public key available")
        else:
            encrypted_env_vars = await encrypt_age_content(env_vars, public_key)
            component_config["user-env-vars"] = LiteralScalarString(encrypted_env_vars)

    # Add aliases if provided. Stored as ONE AGE block whose plaintext is KEY=value
    # lines, exactly like user-env-vars (RC-106). An unencrypted mapping is kept as-is
    # when no public key is available (backward compatible).
    if aliases:
        aliases_dict = parse_aliases(aliases)
        if aliases_dict:
            if public_key:
                block = "\n".join(f"{key}={value}" for key, value in aliases_dict.items())
                component_config["aliases"] = LiteralScalarString(await encrypt_age_content(block, public_key))
            else:
                logger.warning("Could not encrypt aliases for component '%s': no AGE public key available", name)
                component_config["aliases"] = aliases_dict

    return component_config


def normalize_container_image(image: str) -> tuple[str, bool]:
    """Lowercase a container image reference, and say whether that changed anything.

    The OCI Distribution Specification requires repository names to be lowercase.
    """
    normalized = image.lower()
    was_normalized = normalized != image
    if was_normalized:
        logger.warning(
            f"Container image contained uppercase characters and was normalized: '{image}' -> '{normalized}'"
        )
    return normalized, was_normalized


def resolve_deployment_filter(deployment_name: str | None, deployment_names: list[str] | None) -> list[str] | None:
    """Normalize the single/plural deployment-filter arguments into one list.

    Returns None when no filter is requested (process all deployments). When a
    list is returned it is the exhaustive set of deployment names to target -
    an empty list therefore means *zero* deployments, not "all". ``deployment_names``
    takes precedence over the singular ``deployment_name`` convenience argument.
    """
    if deployment_names is not None:
        return deployment_names
    if deployment_name is not None:
        return [deployment_name]
    return None


def deployments_using_component(project_data: dict[str, Any], component_name: str) -> list[str]:
    """The names of the deployments referencing this component, in file order."""
    names = []
    for deployment in project_data.get("deployments") or []:
        if not isinstance(deployment, dict) or not deployment.get("name"):
            continue
        if any(
            isinstance(component, dict) and component.get("reference") == component_name
            for component in deployment.get("components") or []
        ):
            names.append(str(deployment["name"]))
    return names


def affected_deployments(
    project_data: dict[str, Any],
    *,
    deployment_name: str | None = None,
    component_names: list[str] | None = None,
) -> list[str] | None:
    """The deployments a change touches, or None for a project-wide change.

    A change on ONE deployment touches only that one. A change on a component touches the
    deployments using it, and there can be more than one. Without either, the change is
    project-wide and nothing may be narrowed.

    Whoever narrows a round with this has to record the same list as the task scope
    (``opi.services.tasks.task_supersede.scope_of``): a scope wider than what the round really
    processes would supersede a waiting task whose deployments nobody does afterwards.
    """
    if deployment_name:
        return [deployment_name]
    if component_names:
        names: list[str] = []
        for component_name in component_names:
            names.extend(deployments_using_component(project_data, component_name))
        return list(dict.fromkeys(names))
    return None


def project_level_deployment(deployments: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The deployment carrying the PROJECT LEVEL of this project, or None.

    The alphabetically first one names the repository. A shared rule, so that the writer
    and the ArgoCD application never pick a different repository.
    """
    on_cluster = [d for d in deployments if isinstance(d, dict) and d.get("name")]
    if not on_cluster:
        return None
    return min(on_cluster, key=lambda d: str(d["name"]))


#: The message for a reserved project name, so the wizard, the API and git all give the
#: same sentence. See is_reserved_project_name in opi.core.cluster_config.
RESERVED_PROJECT_NAME_MESSAGE = (
    "Projectnaam '{name}' is niet beschikbaar: de namespace van dit project zou samenvallen "
    "met een namespace van het platform zelf. Kies een andere naam."
)


def namespace_level_deployment(
    deployments: list[dict[str, Any]], cluster: str, namespace: str
) -> dict[str, Any] | None:
    """The deployment writing the NAMESPACE-wide manifests of this namespace, or None.

    The same rule as ``project_level_deployment``, one layer down: the alphabetically first
    deployment in the namespace is the owner. Without such an owner, two deployments in the
    same namespace put the same object in two ArgoCD applications.
    """
    in_namespace = [
        d
        for d in deployments
        if isinstance(d, dict) and d.get("name") and d.get("cluster") == cluster and d.get("namespace") == namespace
    ]
    if not in_namespace:
        return None
    return min(in_namespace, key=lambda d: str(d["name"]))


# should_encrypt_user_env_var function removed - all user env vars are now always encrypted


def encrypted_repository_password() -> str:
    """The deployments-repo password in the form a project file may carry: never plain text.

    Encrypted with the PLATFORM key, because that is the key that reads it back
    (``ArgoManager`` and the git connectors decrypt with ``SOPS_AGE_PRIVATE_KEY``). A value
    the environment already supplies as ``base64+age:`` is handed through unchanged. The
    setting itself may be ``plain:``: the env secret is a SOPS file, the project file is not.

    Raises:
        HTTPException: 500 when the setting is missing; without it no project file can be
            written at all.
    """
    stored = settings.PROJECT_REPO_PASSWORD
    if not stored:
        raise HTTPException(
            status_code=500,
            detail="PROJECT_REPO_PASSWORD is niet gezet in de omgeving van OPI; zonder die kan het projectbestand niet worden geschreven.",
        )
    form, _ = parse_password_with_prefix(stored)
    if form == "base64+age":
        return stored
    plain_password = decrypt_password_smart_sync(stored, settings.SOPS_AGE_PRIVATE_KEY)
    return encrypt_to_base64_age_sync(plain_password, settings.SOPS_AGE_PUBLIC_KEY)


async def generate_self_service_project_yaml(project_data: Any, *, api_token: str | None = None) -> str:
    """Build the project file for a self-service creation, as a YAML string.

    ``project_data`` is a ``SelfServiceProjectRequest``, or any object carrying the same
    attributes; ``generate_base_project_file`` passes a ``SimpleNamespace``.
    """
    try:
        private_key, public_key = generate_age_key_pair()
        encrypted_private_key = await wrap_project_key(private_key)
        logger.debug(f"Generated AGE key pair for project: {project_data.project_name}")
    except Exception as e:
        logger.error(f"Failed to generate AGE key pair: {e}")
        raise HTTPException(status_code=500, detail=f"Cannot create project: AGE key generation failed. {e!s}")

    # The first credential of this project: a token of the ``project-api`` service account.
    # Only the hash goes into the file (schema version 2.12), so the value cannot be read
    # back afterwards and the caller is the one that has to keep it.
    #
    # Only when the caller actually RECEIVES that value, which is the case on creation
    # through the API. The wizard does not receive it, so there the account stays without a
    # token: a token nobody knows is a credential nobody can use and nobody cleans up. The
    # admin issues one on the Team tab.
    token_hash = hash_van(api_token) if api_token else None

    repo_password = encrypted_repository_password()

    # Parse project-level services using the service adapter
    project_services = ServiceAdapter.parse_services_from_strings(project_data.services or [])

    # Build components list from form data
    components_list = []
    if project_data.components:
        for idx, comp in enumerate(project_data.components):
            try:
                component_config = await build_component_config(
                    name=f"component-{idx + 1}",
                    component_type=comp.type,
                    port=comp.port,
                    path=comp.path,
                    services=comp.services or [],
                    cpu_limit=comp.cpu_limit,
                    memory_limit=comp.memory_limit,
                    env_vars=comp.env_vars,
                    aliases=comp.aliases,
                    public_key=public_key,
                    default_port=8080,
                )
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))

            components_list.append(component_config)
    else:
        fallback_services_list = ServiceAdapter.build_component_service_entries([svc.value for svc in project_services])

        fallback_component_config: dict[str, Any] = {
            "name": "main",
            "type": "deployment",
            "ports": {"inbound": [8080], "outbound": [80, 443]},
            "services": fallback_services_list,
            "uses-components": [],
        }

        components_list.append(fallback_component_config)

    # ONE deployment holding every component.
    deployments_list = []
    if project_data.components:
        component_refs = []
        for idx, comp in enumerate(project_data.components):
            image, _ = normalize_container_image(comp.image or "nginx:latest")
            component_refs.append({"reference": f"component-{idx + 1}", "image": image})

        deployment_name = project_data.deployment_name
        deployment_config = {
            "name": deployment_name,
            "cluster": project_data.cluster,
            "namespace": project_data.project_name,
            "repository": "main-repo",
            "components": component_refs,
        }

        root_component = next(
            (f"component-{idx + 1}" for idx, comp in enumerate(project_data.components) if comp.root), None
        )
        _apply_web_address_settings(deployment_config, project_data, deployment_name, root_component)

        deployments_list.append(deployment_config)
    else:
        deployment_name = project_data.deployment_name
        deployment_config = {
            "name": deployment_name,
            "cluster": project_data.cluster,
            "namespace": project_data.project_name,
            "repository": "main-repo",
            "components": [{"reference": "main", "image": "nginx:latest"}],
        }

        _apply_web_address_settings(deployment_config, project_data, deployment_name, None)

        deployments_list.append(deployment_config)

    config_section = {
        "age-public-key": public_key,
        "age-private-key": LiteralScalarString(encrypted_private_key),
    }

    # Add contact-email if specified (overrides cluster default for Let's Encrypt)
    if hasattr(project_data, "contact_email") and project_data.contact_email:
        config_section["contact-email"] = project_data.contact_email

    from opi.services.schema_migration import LATEST_SCHEMA_VERSION_VALUE

    project_config = {
        "schema-version": LATEST_SCHEMA_VERSION_VALUE,
        "name": project_data.project_name,
        "display-name": project_data.display_name,
        "description": project_data.project_description or "Project created via self-service portal",
        "clusters": [project_data.cluster],
        "services": [service.value for service in project_services],  # Project-level services
        "config": config_section,
        "repositories": [
            {
                "name": "main-repo",
                "url": settings.PROJECT_REPO_URL,
                "username": settings.PROJECT_REPO_USERNAME,
                "password": repo_password,
                "branch": settings.PROJECT_REPO_BRANCH,
                "path": ".",
            }
        ],
        "components": components_list,
        "deployments": deployments_list,
    }

    users: list[dict[str, Any]] = []
    if project_data.user_email and project_data.user_roles:
        for email, role_value in zip(project_data.user_email, project_data.user_roles, strict=False):
            if email and email.strip():
                chosen = sorted(role.value for role in roles_from_value(role_value))
                users.append({"email": email.strip(), "roles": chosen or list(PROJECT_API_ROLLEN)})

    # The service account carrying the API access of this project, with the roles of an
    # admin: it opens the whole write side.
    users.append(
        {
            "type": "service-account",
            "name": PROJECT_API_ACCOUNT,
            "roles": list(PROJECT_API_ROLLEN),
            "tokens": (
                [
                    {
                        "name": "eerste",
                        "expires": vervaldatum(settings.TOKEN_MAX_DAGEN_SERVICE_ACCOUNT).isoformat(),
                        "hash": token_hash,
                    }
                ]
                if token_hash
                else []
            ),
        }
    )
    project_config["users"] = users

    # Handle multiline password with literal block scalar
    password = project_config["repositories"][0]["password"]
    if password and "\n" in password:
        project_config["repositories"][0]["password"] = LiteralScalarString(password)

    # Generate YAML content via the single canonical writer
    yaml_content = dump_yaml_to_string(project_config)

    logger.info(
        f"Generated project YAML for {project_data.project_name} with {len(components_list)} components "
        f"and {len(deployments_list)} deployments"
    )

    return yaml_content


async def generate_base_project_file(
    project_name: str,
    display_name: str,
    description: str,
    cluster: str,
    owner_email: str,
) -> tuple[dict[str, Any], str]:
    """Build the base project file for a project created outside the wizard.

    "Base" means: the project exists, knows its repository and carries its own
    keys. That is everything the platform needs to recognise a project and
    everything a caller needs to keep working on it through the API. What runs in
    it -- components and deployments -- is configured afterwards, so this file
    deliberately declares neither.

    The content comes out of ``generate_self_service_project_yaml``, the same builder the
    self-service portal uses; only the parts that describe what runs are removed
    afterwards.

    Args:
        project_name: Technical project name (already validated)
        display_name: Human-readable name
        description: Project description
        cluster: Cluster the project targets
        owner_email: The creator, recorded as a member with ``CREATOR_ROLES``

    Returns:
        A tuple of the project file as a dict, and the plaintext API key of the
        new project.

    Raises:
        ProjectApiKeyError: When the generated API key cannot be read back in
            plaintext, so no usable key could be handed to the caller.
    """
    project_data = SimpleNamespace(
        project_name=project_name,
        display_name=display_name,
        project_description=description,
        cluster=cluster,
        deployment_name="main",
        domain_format=None,
        subdomain=None,
        base_domain=None,
        issuer=None,
        contact_email=None,
        user_email=[owner_email],
        user_roles=[sorted(role.value for role in CREATOR_ROLES)],
        services=[],
        components=None,
    )

    token_value, _ = nieuw_token()
    yaml_content = await generate_self_service_project_yaml(project_data, api_token=token_value)
    project_dict = load_yaml_from_string(yaml_content)
    if not project_dict:
        raise ProjectApiKeyError(f"Kon het gegenereerde projectbestand voor '{project_name}' niet inlezen")

    # Nothing runs yet: drop the placeholder component and deployment the
    # self-service builder adds for the form flow. A project without deployments
    # is valid and is left alone by processing; the caller adds a deployment when
    # there is something to deploy.
    project_dict.pop("components", None)
    project_dict.pop("deployments", None)

    # The token is in the file as a hash only, so this value is the only one that will ever
    # exist of it. Checking that the hash is in there, so the caller never receives a token
    # that matches nothing.
    summary = get_project_service().build_project_from_data(dict(project_dict), f"projects/{project_name}.yaml")
    hashes = {t.hash for member in (summary.users or []) for t in member.tokens} if summary else set()
    if hash_van(token_value) not in hashes:
        raise ProjectApiKeyError(
            f"Het eerste API-token voor '{project_name}' staat niet in het projectbestand; "
            f"het project is niet aangemaakt."
        )

    return project_dict, token_value
