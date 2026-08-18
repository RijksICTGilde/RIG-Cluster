"""Structural validation of a complete project dict.

Extracted from ProjectManager so the ProjectStore can run the SAME checks on the
final state of a mutation, before anything is written or committed. Keeping one
implementation means there is no "less validated" write path: ProjectManager and
ProjectStore both call into here.

Runs AFTER json-schema validation (opi.core.project_schema.validate_project_schema)
and BEFORE any write or commit. Fails closed on the first violation.
"""

import logging
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ValidationError

from opi.core.config import settings
from opi.core.project_schema import ProjectIntegrityError
from opi.forms.editables.enforcers import DomainConfigEnforcer, FieldWarning
from opi.handlers.project_file_handler import validate_attachment_couplings, validate_attachment_references
from opi.services import ServiceAdapter
from opi.services.catalog.base import ConfigLayer, Service
from opi.services.catalog.publish_on_web.domain_config import DomainSetting, get_domain_setting
from opi.services.catalog.shared.storage import STORED_CONTEXT_KEY
from opi.services.postgres_scope import get_postgres_schemas
from opi.services.project import Project
from opi.services.registry import SERVICES, get_service, property_owning_services
from opi.services.services import (
    service_entry_config,
    service_entry_data,
    service_entry_name,
    service_entry_schema_version,
)
from opi.services.services_enums import ServiceType
from opi.utils.naming import generate_extra_database_schema, registry_tag_owner
from opi.utils.project_utils import ComponentValidationError, validate_component_paths, validate_root_component

if TYPE_CHECKING:
    from opi.forms.editables.editable import Editable

logger = logging.getLogger(__name__)

#: This module validates a project file that ALREADY EXISTS -- on every save, and on
#: every reprocess and replay of a file nobody touched. A rule about how large a new
#: volume may be does not belong here: applying it to stored data would turn an older
#: project with a larger mount into a file that can no longer be saved at all, and a
#: PVC cannot shrink, so its owner could not comply either. Ceilings are enforced where
#: the value ARRIVES (the config API's request bodies and the form field); here the
#: shape is checked and the value is taken as it stands.
STORED_PROJECT_CONTEXT: dict[str, Any] = {STORED_CONTEXT_KEY: True}


def _accepted_config_fields(provider: Service, layer: ConfigLayer) -> list[str]:
    """The config field names a service accepts at ``layer``, for error guidance.

    Sources the service's own declarative field metadata: ``config_api_fields``
    (the API/YAML-accepted keys, derived from ``config_model_field_names`` for
    modelled services), falling back to the leaf names of ``config_editables`` for
    services whose config is a sequence with no flat field set (storage). Returns []
    when the service declares neither.
    """
    fields = provider.config_api_fields(layer)
    if fields:
        return fields
    # Sequence configs (storage) declare no flat field set, so read the leaf names off
    # the config_editables: the per-entry child fields (name/size/mount-path) when the
    # editable is a sequence, else the editable's own leaf.
    names: list[str] = []
    for editable in provider.config_editables(layer):
        leaves = editable.children or [editable]
        names.extend(child.yaml_path.rsplit("/", 1)[-1] for child in leaves)
    return names


def validation_reasons(error: ValidationError) -> str:
    """De redenen van een ValidationError, zoals ze aan een gebruiker getoond mogen worden.

    ``str(e)`` van pydantic is uitvoer voor een ontwikkelaar: hij zet er
    ``[type=value_error, input_value=..., input_type=dict]`` achter en een link naar
    errors.pydantic.dev. Dat kwam zo op het scherm van iemand die een webadres wilde
    wijzigen, met de afgekeurde waarde erin -- en die waarde kan een geheim zijn.

    ``error["msg"]`` draagt alleen de reden. Het voorvoegsel ``Value error, `` dat pydantic
    voor een ``model_validator`` zet valt eraf: de zin eromheen zegt al dat er iets ongeldig
    is, en "Value error" voegt daar niets aan toe wat de lezer verder helpt.
    """
    reasons = [error_entry["msg"].removeprefix("Value error, ") for error_entry in error.errors()]
    return "; ".join(reasons) or "waarde voldoet niet aan het model"


def _validate_one_config(
    name: str, raw: Any, layer: ConfigLayer, where: str, project_name: str, from_version: str | None = None
) -> None:
    """Validate one service config block against its provider's typed model.

    Shared by the project-level and component-level walks. Skips services that are
    unknown or take no typed config. ``from_version`` is the entry's stamped
    ``schema-version``, threaded through so the provider migrates an older config
    block forward before validating (None = current version). Fails closed: raises
    ProjectIntegrityError, with the service's own accepted-field list
    (config_api_fields / config_editables) appended so the message tells the user
    which keys the service accepts.
    """
    try:
        service_type = ServiceType(name)
    except ValueError:
        return  # unknown service name -- other validation handles it
    provider = get_service(service_type)
    model = provider.config_model_for(layer)
    if model is None:
        return  # service takes no typed config at this layer
    try:
        if model is provider.config_model:
            provider.validate_config(raw, from_version=from_version, context=STORED_PROJECT_CONTEXT)
        else:
            # A layer-specific model (per-mount clone state). OPI writes it, so there is no
            # stamped version to migrate from; validate the shape directly.
            model.model_validate(raw)
    except ValidationError as e:
        accepted = _accepted_config_fields(provider, layer)
        hint = f" Geaccepteerde velden: {', '.join(accepted)}." if accepted else ""
        raise ProjectIntegrityError(
            f"Project '{project_name}': configuratie van service '{name}' {where} is ongeldig: "
            f"{validation_reasons(e)}.{hint}"
        ) from e


def _validate_one_data_block(name: str, raw: Any, layer: ConfigLayer, where: str, project_name: str) -> None:
    """Validate one service's DEFINE-side ``data`` block against its provider's model.

    The counterpart of ``_validate_one_config`` for the definitions a service stores
    rather than the configuration of a use. Skips services that define nothing at the
    layer. Fails closed, and reports only the validators' own reasons -- a definition
    holds the thing itself (an attachment's content), so pydantic's ``input_value``
    would put an encrypted blob, or worse a plaintext one, in the log and the response.
    """
    try:
        service_type = ServiceType(name)
    except ValueError:
        return  # unknown service name -- other validation handles it
    model = get_service(service_type).data_model_for(layer)
    if model is None:
        return  # service defines nothing at this layer
    try:
        model.model_validate(raw)
    except ValidationError as e:
        reasons = validation_reasons(e)
        raise ProjectIntegrityError(
            f"Project '{project_name}': gegevens van service '{name}' {where} zijn ongeldig: {reasons}."
        ) from None


def validate_service_configs(project_data: dict[str, Any]) -> None:
    """Validate every service's config against its provider's typed model (RC-5 A:
    the per-service config-validation chokepoint).

    Covers all three layers a config can live at: project-level service definitions
    (keycloak, namespace-postgres, auth-wall), component-level service references
    (persistent-storage / temp-storage mounts, metrics-scraper port/path) and
    deployment-level entries (clone state today). Services without a config block, or
    without a typed model, are skipped. Fails closed: raises ProjectIntegrityError on the
    first invalid service config.
    """
    view = Project(project_data)
    project_name = project_data.get("name", "(onbekend)")

    # Project-level service definitions.
    for name in ServiceAdapter.extract_service_names_from_project_services(project_data.get("services", [])):
        entry = view.service_entry(name)
        # The DEFINE side first: what the service stores under ``data`` (the attachments
        # catalog today). It was validated by nothing at all -- this walk only ever
        # looked at ``config`` -- so a catalog entry with a missing filename or an id
        # that cannot become a volume name was committed and failed at deploy time.
        data = service_entry_data(entry)
        if data is not None:
            _validate_one_data_block(name, data, ConfigLayer.PROJECT, "op projectniveau", project_name)
        raw = view.service_config(name)
        if raw is None:
            continue  # bare service / no project-level config to validate
        from_version = service_entry_schema_version(entry)
        _validate_one_config(name, raw, ConfigLayer.PROJECT, "op projectniveau", project_name, from_version)

    # Component-level service references (storage mounts, metrics port/path). Their
    # config lives on the component's service entry, not at project level, so the
    # project-level walk above never sees it.
    for component in project_data.get("components", []) or []:
        if not isinstance(component, dict):
            continue
        comp_name = component.get("name", "(onbekend)")
        for entry in component.get("services", []) or []:
            name = service_entry_name(entry)
            config = service_entry_config(entry)
            if name is None or config is None:
                continue  # bare reference / no config to validate
            from_version = service_entry_schema_version(entry)
            _validate_one_config(
                name, config, ConfigLayer.COMPONENT, f"in component '{comp_name}'", project_name, from_version
            )

    # Deployment-level service entries (clone state today, anything a service declares
    # tomorrow). ``$defs/deployment-service-config`` in the global schema is deliberately
    # open, so this walk is the only thing between a typo and a silently ignored setting.
    for deployment in project_data.get("deployments", []) or []:
        if not isinstance(deployment, dict):
            continue
        dep_name = deployment.get("name", "(onbekend)")
        for entry in deployment.get("services", []) or []:
            name = service_entry_name(entry)
            config = service_entry_config(entry)
            if name is None or config is None:
                continue  # bare reference / no config to validate
            from_version = service_entry_schema_version(entry)
            _validate_one_config(
                name, config, ConfigLayer.DEPLOYMENT, f"in deployment '{dep_name}'", project_name, from_version
            )

    # Deployment-component service entries. Two shapes live here: a dict keyed by service
    # name (``{publish-on-web: {config: ...}}``) and, for the storage services, a list of
    # per-mount records under that key. Both are walked, because the global schema no longer
    # guards this layer: opening up the deployment envelope moved that job here.
    for deployment in project_data.get("deployments", []) or []:
        if not isinstance(deployment, dict):
            continue
        dep_name = deployment.get("name", "(onbekend)")
        for component in deployment.get("components", []) or []:
            if not isinstance(component, dict):
                continue
            comp_name = component.get("reference") or component.get("name", "(onbekend)")
            where = f"in component '{comp_name}' van deployment '{dep_name}'"
            services = component.get("services")
            if isinstance(services, dict):
                for name, body in services.items():
                    for entry in body if isinstance(body, list) else [body]:
                        config = entry.get("config") if isinstance(entry, dict) else None
                        if config is None:
                            continue
                        _validate_one_config(
                            name,
                            config,
                            ConfigLayer.DEPLOYMENT_COMPONENT,
                            where,
                            project_name,
                            service_entry_schema_version(entry),
                        )
            elif isinstance(services, list):
                for entry in services:
                    name = service_entry_name(entry)
                    config = service_entry_config(entry)
                    if name is None or config is None:
                        continue
                    _validate_one_config(
                        name,
                        config,
                        ConfigLayer.DEPLOYMENT_COMPONENT,
                        where,
                        project_name,
                        service_entry_schema_version(entry),
                    )

    _validate_owned_properties(project_data, project_name)


def _validate_owned_properties(project_data: dict[str, Any], project_name: str) -> None:
    """Validate the plain component properties that SYSTEM services own (RC-25).

    ``user-env-vars`` and ``aliases`` are services whose config is a property of the
    component rather than a block in a ``services:`` list, so the walks above never see
    them -- which is exactly why they went unvalidated until now. The services declare
    the property (``owned_property``) and the layers they carry it on
    (``config_editables``), so this loop names neither service.
    """
    for service in property_owning_services():
        key, model = service.owned_property, service.config_model
        if key is None or model is None:
            # property_owning_services() filters on owned_property, and a service that owns
            # one is always modelled -- but this walk is a fail-closed validation path, so
            # it narrows explicitly instead of leaning on an assert (which `python -O`
            # strips, turning the guarantee into a silent skip).
            continue
        if service.config_editables(ConfigLayer.COMPONENT):
            for component in project_data.get("components", []) or []:
                if isinstance(component, dict) and component.get(key) is not None:
                    _validate_owned_property(
                        service,
                        model,
                        component[key],
                        f"van component '{component.get('name', '(onbekend)')}'",
                        project_name,
                    )
        if not service.config_editables(ConfigLayer.DEPLOYMENT_COMPONENT):
            continue
        for deployment in project_data.get("deployments", []) or []:
            if not isinstance(deployment, dict):
                continue
            dep_name = deployment.get("name", "(onbekend)")
            for component in deployment.get("components", []) or []:
                if isinstance(component, dict) and component.get(key) is not None:
                    comp_name = component.get("reference") or component.get("name", "(onbekend)")
                    _validate_owned_property(
                        service,
                        model,
                        component[key],
                        f"van component '{comp_name}' in deployment '{dep_name}'",
                        project_name,
                    )


def _validate_owned_property(service: Service, model: type[BaseModel], raw: Any, where: str, project_name: str) -> None:
    """Validate one owned-property value against its service's model; fail closed.

    The model is passed in rather than read off the service again, so the narrowing the
    caller already did is carried in the signature instead of re-asserted here.

    The message names only the validators' own reasons, never the value. The properties
    these services own hold secrets (``user-env-vars`` is the component's own environment,
    a value may be a password), and this message is both logged at WARNING and returned to
    the caller -- so ``str(e)`` would put a pasted secret from an unparseable plaintext
    value in the central OPI log and in an HTTP response. ``e.errors()[*]["msg"]`` carries
    the reason without pydantic's ``input_value``, and the chain is dropped (``from None``)
    because the ValidationError itself still holds the input for any handler that logs a
    traceback.
    """
    try:
        model.model_validate(raw)
    except ValidationError as e:
        reasons = validation_reasons(e)
        raise ProjectIntegrityError(
            f"Project '{project_name}': '{service.owned_property}' {where} is ongeldig: {reasons}."
        ) from None


def validate_component_references(project_data: dict, components: list, context: str = "deployment") -> dict[str, Any]:
    """
    Validate that all component references exist in the project.

    Args:
        project_data: The project data containing component definitions
        components: List of ComponentReference objects or dicts with 'reference' key
        context: Context for error messages (e.g. "deployment", "update")

    Returns:
        Dict with validation result: {"success": bool, "error": str | None, "invalid_references": list | None}
    """
    project_components = project_data.get("components", [])
    component_names = {comp.get("name") for comp in project_components}
    invalid_references = []

    for component in components:
        # Handle both ComponentReference objects and dict format
        reference = getattr(component, "reference", None) or component.get("reference")

        if reference not in component_names:
            invalid_references.append(reference)

    if invalid_references:
        available_components = list(component_names) if component_names else ["none"]
        project_name = project_data.get("name", "unknown")
        error_msg = f"Invalid component references in {context} for project '{project_name}': {invalid_references}. Available components: {available_components}"
        logger.error(error_msg)
        return {"success": False, "error": error_msg, "invalid_references": invalid_references}

    return {"success": True, "error": None, "invalid_references": None}


def _validate_services_listed_once(services: Any, project_name: str, where: str) -> None:
    """A services list may name each service at most once.

    The list is a selection set keyed by service name, so a repeat has no meaning: a
    second entry either says the same thing or silently contradicts the first, and
    every reader (config lookup, provisioning, manifest generation) sees only one of
    them. A hand-edited project file can still contain one, which is why this is a
    check that rejects rather than something that quietly collapses the list.
    """
    if not isinstance(services, list):
        return
    seen: set[str] = set()
    for entry in services:
        name = service_entry_name(entry)
        if name is None:
            continue
        if name in seen:
            raise ProjectIntegrityError(
                f"Project '{project_name}': service '{name}' staat meerdere keren in de services-lijst op {where}"
            )
        seen.add(name)


def _project_context_kwargs(provider_class: type, project_data: dict[str, Any]) -> dict[str, Any]:
    """The constructor arguments *provider_class* accepts out of the project context.

    The same trick the form bridge uses (``_filter_provider_kwargs``): a provider declares
    what it needs as ``__init__`` parameters and the caller matches by name, so a provider
    with a fixed list takes no arguments and one that reads the project takes ``yaml_data``.

    ``current_value`` is deliberately NOT offered. A provider given one keeps an unknown
    stored value as an option flagged "(bestaat niet meer)", which is what stops a form
    save from silently dropping it -- and would make every value valid here.
    """
    import inspect

    accepted = set(inspect.signature(provider_class.__init__).parameters) - {"self"}
    return {"yaml_data": project_data} if "yaml_data" in accepted else {}


def validate_declared_choices(project_data: dict[str, Any]) -> list[str]:
    """Values that reference something in this project which is not in this project.

    The sibling of ``collect_config_advice`` and deliberately the other verdict. That one
    asks whether a field is FILLED given a setting elsewhere and warns; this one asks
    whether the value that IS there exists, and refuses. A realm role that no keycloak
    config defines is not a choice with a downside, it is a typo: keycloak assigns
    nothing on redemption (``assign_realm_roles_to_user`` reports it under ``not_found``
    and moves on), so the invited user arrives without the role and, under
    ``restrict-access``, without access.

    The set of valid values is not restated here. It comes from the field's own
    ``values_provider`` -- the same provider that fills the form's select and the same one
    ``x-choices-source`` names in the OpenAPI document, so a caller is judged against
    exactly the list they were told to read.

    Opt-in per field with ``Editable.values_must_exist``, because an options list is
    usually a menu rather than a closed set (see that flag). Only fields that carry it are
    looked at, and only the layers the service declares config on.
    """
    from opi.forms.editables.service_path import expand_wildcard_path
    from opi.forms.visualizers.providers import PROVIDER_REGISTRY, UNDECLARED_SOURCE, OptionsSource

    def walk(editables: list[Editable]) -> list[Editable]:
        found: list[Editable] = []
        for editable in editables:
            found.append(editable)
            found.extend(walk(editable.children or []))
        return found

    errors: list[str] = []
    seen_paths: set[str] = set()
    for service in SERVICES.values():
        for layer in service.config_layers():
            for editable in walk(service.config_editables(layer)):
                if not editable.values_must_exist or not editable.values_provider:
                    continue
                if editable.yaml_path in seen_paths:
                    continue  # the same editable can be declared on more than one layer
                seen_paths.add(editable.yaml_path)
                provider_class = PROVIDER_REGISTRY.get(editable.values_provider)
                if provider_class is None:
                    logger.warning(
                        "Onbekende values_provider %r op %s; waarden niet gecontroleerd",
                        editable.values_provider,
                        editable.yaml_path,
                    )
                    continue
                provider = provider_class(**_project_context_kwargs(provider_class, project_data))
                allowed = {str(option.get("value")) for option in provider.get_options()}
                offered = sorted(option for option in allowed if option)
                if not offered:
                    # The source is empty, so there is nothing to be measured against. This
                    # is not "everything is wrong": it is a project whose values come from
                    # somewhere this provider cannot see -- the four pre-service invite
                    # files name roles of a realm nobody configured through ZAD. Refusing
                    # there would claim knowledge we do not have, and would block the next
                    # edit of a project over a value this release did not introduce. The
                    # case that matters is never empty: with restrict-access on there is
                    # always at least the wall role.
                    continue
                source = getattr(provider_class, "options_source", UNDECLARED_SOURCE)
                what = f"{source.description} " if isinstance(source, OptionsSource) else ""
                for path, value in expand_wildcard_path(project_data, editable.yaml_path):
                    if value is None or str(value) in allowed:
                        continue
                    errors.append(
                        f"'{value}' op {path} bestaat niet in dit project. {what}Nu beschikbaar: {', '.join(offered)}."
                    )
    return errors


def validate_database_schema_names(project_data: dict[str, Any]) -> list[str]:
    """The composed schema names of every extra schema, against every deployment (RC-59).

    ``{project}_{deployment}_{postfix}`` has to fit in PostgreSQL's 63 characters, and how
    much room the postfix has depends on the project and deployment names -- so this cannot
    be a field rule and cannot be decided when the postfix is typed.

    ``UniqueSchemaEnforcer`` already ran it, but only when the *schema list* was being
    saved, and only against the deployments that existed at that moment. That leaves the
    real hole: a postfix that fits today stops fitting the moment a deployment with a
    longer name is added, and nothing on that road looks at schemas. The failure then
    surfaced at rollout, as a ``ValueError`` out of ``generate_extra_database_schema``,
    long after the change that caused it.

    Running it here, in the structural validation every save passes through, closes that:
    adding the deployment is refused, with a message that names both the deployment and
    the postfix that no longer fits.

    Schemas marked for deletion are skipped -- they are on their way out and must not
    block a save.
    """
    errors: list[str] = []
    project_name = project_data.get("name") or ""
    deployment_names = [
        name for d in (project_data.get("deployments") or []) if isinstance(d, dict) and (name := d.get("name"))
    ]
    if not deployment_names:
        return errors

    for entry in get_postgres_schemas(project_data):
        postfix = entry.get("postfix")
        if not postfix:
            continue
        for deployment_name in deployment_names:
            try:
                generate_extra_database_schema(project_name, deployment_name, postfix)
            except ValueError:
                errors.append(
                    f"schema '{postfix}' levert voor deployment '{deployment_name}' een naam op die langer is "
                    f"dan de 63 tekens die PostgreSQL toestaat. Kies een kortere postfix of een kortere "
                    f"deploymentnaam."
                )
    return errors


def _platform_registry_repo() -> str | None:
    """The one repository the platform's image-push endpoint writes into, or None.

    Everything a project pushes lands in ``{REGISTRY_URL}/{REGISTRY_ORG}``; ownership
    lives in the tag (see ``build_registry_tag``). Returns None when no registry is
    configured, which is the case on clusters without the image-push feature.
    """
    if not settings.REGISTRY_URL or not settings.REGISTRY_ORG:
        return None
    return f"{settings.REGISTRY_URL}/{settings.REGISTRY_ORG}"


def _normalize_registry_repo(repo: str) -> str:
    """The comparable form of a registry repository, so one repo has one spelling.

    A hostname is case-insensitive and the https port may be written out, so
    ``RCR.rijksapps.nl/rig`` and ``rcr.rijksapps.nl:443/rig`` are the same repository
    as ``rcr.rijksapps.nl/rig``. The path after the host is left alone: registries
    treat it case-sensitively.
    """
    host, separator, path = repo.partition("/")
    if not separator or not ("." in host or ":" in host or host == "localhost"):
        # No registry host in front (e.g. 'nginx' or 'library/nginx'): nothing to normalize.
        return repo
    host = host.lower()
    host = host.removesuffix(":443")
    return f"{host}/{path}"


def _split_image_reference(image: str) -> tuple[str, str | None, bool]:
    """Split an image reference into (repository, tag, carries-a-digest).

    Handles the shapes the project schema allows: ``repo``, ``repo:tag``,
    ``repo@sha256:...`` and ``repo:tag@sha256:...``, with an optional port in the
    host. A colon that is followed by a ``/`` is a port, not a tag separator.
    """
    reference, digest_separator, _digest = image.partition("@")
    repo, tag_separator, tag = reference.rpartition(":")
    if not tag_separator or "/" in tag:
        return reference, None, bool(digest_separator)
    return repo, tag, bool(digest_separator)


def validate_platform_registry_image_ownership(project_data: dict[str, Any]) -> list[str]:
    """Reject deployment images that point at another project's tag in the shared registry.

    Pinning the push side stops a project from writing another's image; without this
    a project could still READ one, by naming the other's tag as its own deployment
    image. Only references into the platform's own registry repository are judged --
    an image from ghcr.io, Docker Hub or a project's own registry is nobody's business
    here, and is left alone.

    The reference is normalized before it is judged, because one repository has more
    than one valid spelling (uppercase host, an explicit ``:443``). A digest reference
    into the platform repository is refused outright: ownership lives in the tag, and
    a digest names an image in the shared repository without naming its owner.

    Tags from before ownership pinning carry no owner prefix and stay usable, so
    deployments that already run keep running.
    """
    platform_repo = _platform_registry_repo()
    if platform_repo is None:
        return []
    platform_repo = _normalize_registry_repo(platform_repo)

    project_name = project_data.get("name", "")
    errors: list[str] = []
    for deployment in project_data.get("deployments", []) or []:
        if not isinstance(deployment, dict):
            continue
        for component in deployment.get("components", []) or []:
            if not isinstance(component, dict):
                continue
            image = component.get("image")
            if not isinstance(image, str):
                continue
            repo, registry_tag, has_digest = _split_image_reference(image)
            if _normalize_registry_repo(repo) != platform_repo:
                continue
            where = f"deployment '{deployment.get('name')}' component '{component.get('reference')}'"
            if has_digest:
                errors.append(
                    f"{where} verwijst met '{image}' naar de gedeelde platformregistry met een digest. "
                    f"Daar staat niet in van wie de image is, dus verwijs naar je eigen tag "
                    f"('{project_name}_...') in plaats van naar een digest"
                )
                continue
            owner = registry_tag_owner(registry_tag) if registry_tag is not None else None
            if owner is not None and owner != project_name:
                errors.append(
                    f"{where} verwijst met '{image}' naar een image in de gedeelde platformregistry "
                    f"die van project '{owner}' is. "
                    f"Je kunt daar alleen images gebruiken die je zelf gepusht hebt"
                )
    return errors


async def validate_project_structure(project_data: dict[str, Any]) -> None:
    """Validate cross-field structural integrity of a complete project dict.

    Runs the reference/uniqueness/path/root/domain checks against the final merged
    dict so they hold no matter which caller produced it. Raises
    ProjectIntegrityError on the first violation; fails closed.
    """
    project_name = project_data.get("name", "(onbekend)")
    components = project_data.get("components", []) or []
    deployments = project_data.get("deployments", []) or []

    # Component names unique
    seen_components: set[str] = set()
    for comp in components:
        if not isinstance(comp, dict):
            continue
        cname = comp.get("name")
        if not isinstance(cname, str):
            raise ProjectIntegrityError(f"Project '{project_name}': een component zonder naam")
        if cname in seen_components:
            raise ProjectIntegrityError(f"Project '{project_name}': component '{cname}' is meervoudig gedefinieerd")
        seen_components.add(cname)

    # A services list is a selection set: each service at most once
    _validate_services_listed_once(project_data.get("services"), project_name, "projectniveau")
    for comp in components:
        if isinstance(comp, dict):
            _validate_services_listed_once(comp.get("services"), project_name, f"component '{comp.get('name')}'")
    for dep in deployments:
        if not isinstance(dep, dict):
            continue
        _validate_services_listed_once(dep.get("services"), project_name, f"deployment '{dep.get('name')}'")
        for ref in dep.get("components", []) or []:
            if isinstance(ref, dict):
                _validate_services_listed_once(
                    ref.get("services"),
                    project_name,
                    f"deployment '{dep.get('name')}' component '{ref.get('reference')}'",
                )

    project_service_names = set(
        ServiceAdapter.extract_service_names_from_project_services(project_data.get("services", []))
    )
    component_by_name = {c.get("name"): c for c in components if isinstance(c, dict)}

    # Component service references resolve to a project-level service
    for comp in components:
        if not isinstance(comp, dict):
            continue
        comp_service_names = ServiceAdapter.extract_service_names_from_project_services(comp.get("services", []))
        invalid_services = [s for s in comp_service_names if s not in project_service_names]
        if invalid_services:
            raise ProjectIntegrityError(
                f"Project '{project_name}': component '{comp.get('name')}' verwijst naar services die niet op "
                f"projectniveau bestaan: {invalid_services}"
            )

    # Deployment names unique
    seen_deployments: set[str] = set()
    for index, dep in enumerate(deployments):
        if not isinstance(dep, dict):
            continue
        dep_name = dep.get("name")
        if not isinstance(dep_name, str):
            raise ProjectIntegrityError(f"Project '{project_name}': een deployment zonder naam")
        if dep_name in seen_deployments:
            raise ProjectIntegrityError(f"Project '{project_name}': deployment '{dep_name}' is meervoudig gedefinieerd")
        seen_deployments.add(dep_name)

        refs = dep.get("components", []) or []
        domain_mode = get_domain_setting(dep, DomainSetting.DOMAIN_MODE, "component-specific")

        # All component references resolve to a defined component
        reference_result = validate_component_references(project_data, refs, "deployment")
        if not reference_result["success"]:
            raise ProjectIntegrityError(reference_result["error"])

        # Ingress path uniqueness within the deployment
        paths = []
        for ref in refs:
            ref_name = ref.get("reference") if isinstance(ref, dict) else None
            comp = component_by_name.get(ref_name)
            if comp:
                paths.append(comp.get("path", "/"))
        try:
            validate_component_paths(paths, domain_mode)
        except ComponentValidationError as e:
            raise ProjectIntegrityError(str(e)) from e

        # Root component constraints
        root_ref = get_domain_setting(dep, DomainSetting.ROOT_COMPONENT)
        if root_ref:
            ref_names = [name for r in refs if isinstance(r, dict) and (name := r.get("reference"))]
            try:
                validate_root_component(
                    root_ref, ref_names, domain_mode, get_domain_setting(dep, DomainSetting.DOMAIN_FORMAT)
                )
            except ComponentValidationError as e:
                raise ProjectIntegrityError(str(e)) from e

        # Hard domain-config violations. A FieldWarning (e.g. an unapproved
        # custom domain) is non-fatal: the UI handles it via domain-request
        # entries, so only a ValueError/FieldError is a structural rejection.
        # ``denied_blocks=False``: a revoked approval on a domain a deployment already
        # uses must be saveable, otherwise the approver cannot record their own verdict.
        # The revocation takes effect at publication (apply_domain_approval_fallback),
        # not by refusing the write.
        try:
            await DomainConfigEnforcer(deployment_index=index, denied_blocks=False).enforce(
                project_data, {"project_name": project_name}
            )
        except FieldWarning:
            pass
        except ValueError as e:
            raise ProjectIntegrityError(str(e)) from e

    # Attachment references must resolve to a catalog entry, so an unknown id
    # is rejected at save time instead of failing later at deploy/resolve time.
    attachment_errors = validate_attachment_references(project_data)
    if attachment_errors:
        raise ProjectIntegrityError(f"Project '{project_name}': {'; '.join(attachment_errors)}")

    # Attachment couplings must be structurally valid: no reference coupled
    # twice, no empty delivery target, no colliding path/env-var. The base
    # component 'services' list is not covered by the JSON schema, so this is
    # the only place a duplicate reference with an empty path is rejected
    # before it can be committed.
    coupling_errors = validate_attachment_couplings(project_data)
    if coupling_errors:
        raise ProjectIntegrityError(f"Project '{project_name}': {'; '.join(coupling_errors)}")

    # Extra database schemas: the composed name has to fit for EVERY deployment, so this
    # belongs here rather than only on the road that edits the schema list -- adding a
    # deployment is the other way a valid schema name becomes an impossible one.
    schema_errors = validate_database_schema_names(project_data)
    if schema_errors:
        raise ProjectIntegrityError(f"Project '{project_name}': {'; '.join(schema_errors)}")

    # A deployment may not point at another project's tag in the shared platform
    # registry. This is the read half of the ownership the push endpoint pins.
    registry_errors = validate_platform_registry_image_ownership(project_data)
    if registry_errors:
        raise ProjectIntegrityError(f"Project '{project_name}': {'; '.join(registry_errors)}")

    # Values that point at something in this project: a realm role an invite hands out
    # has to be a realm role the keycloak config defines, or nobody gets it. Per-service
    # model validation cannot see this -- it judges one config block in isolation, and
    # both halves of the reference live in different services.
    choice_errors = validate_declared_choices(project_data)
    if choice_errors:
        raise ProjectIntegrityError(f"Project '{project_name}': {'; '.join(choice_errors)}")

    # Per-service typed config validation (RC-5 A). Runs last: the envelope and
    # cross-field structure are valid by here, so this only judges the config values.
    validate_service_configs(project_data)
