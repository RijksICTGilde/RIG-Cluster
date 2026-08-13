"""A certificate per deployment (RC-78).

The deployment-component layer carries the same ``tls``/``attachment`` pair as the
component, precisely so one deployment can serve its own certificate while another stays
on the platform's. The capability was in the model and in the resolution cascade but had
no editable, so no form offered it.

What these tests hold in place, in the order the plan states them:

* the fields arrive at the deployment form through the SERVICE hook, not through the form
  naming publish-on-web;
* an empty value means "follow the component" and says so;
* an override replaces the component's mode entirely -- including switching ``provided``
  off, which is the half of the question that could not be read off the code;
* production with its own certificate and staging on Let's Encrypt render two different
  ingresses;
* an override counts as a use of its attachment, so the delete guard cannot call a
  certificate unused.
"""

from __future__ import annotations

import asyncio

import pytest
from jinja2 import Environment, FileSystemLoader
from opi.handlers.project_file_handler import (
    USAGE_CERTIFICATE,
    ProjectFileHandler,
    attachment_usage_sites,
)
from opi.services.catalog.base import ConfigLayer
from opi.services.registry import (
    deployment_component_service_editables,
    deployment_component_service_visualizers,
    get_service,
)
from opi.services.services_enums import ServiceType

_ENV = Environment(loader=FileSystemLoader("manifests"))

#: Attachment content is AGE-encrypted in the file; the guard checks the envelope.
_AGE_BLOCK = "-----BEGIN AGE ENCRYPTED FILE-----\nx\n-----END AGE ENCRYPTED FILE-----"

_TLS_PATH = "deployments[*]/components[*]/services/publish-on-web/config/tls"
_ATTACHMENT_PATH = "deployments[*]/components[*]/services/publish-on-web/config/attachment"


def _tls(project: dict, component: str, deployment: str | None = None) -> str:
    return ProjectFileHandler.__new__(ProjectFileHandler).extract_component_publish_on_web_tls(
        project, component, deployment
    )


def _two_deployments() -> dict:
    """One component, two deployments: production supplies its own certificate."""
    return {
        "services": [{"attachments": {"data": [{"id": "prod-cert", "filename": "prod.pem", "content": _AGE_BLOCK}]}}],
        "components": [{"name": "web", "services": ["publish-on-web"]}],
        "deployments": [
            {
                "name": "productie",
                "components": [
                    {
                        "reference": "web",
                        "services": {"publish-on-web": {"config": {"tls": "provided", "attachment": "prod-cert"}}},
                    }
                ],
            },
            {"name": "staging", "components": [{"reference": "web"}]},
        ],
    }


# --- 1. declared by the service, gathered by the form --------------------------------


def test_service_declares_the_override_at_the_deployment_component_layer() -> None:
    """publish-on-web answers the layer its model already accepted."""
    service = get_service(ServiceType.PUBLISH_ON_WEB)
    paths = [e.yaml_path for e in service.config_editables(ConfigLayer.DEPLOYMENT_COMPONENT)]
    assert paths == [_TLS_PATH, _ATTACHMENT_PATH]
    assert ConfigLayer.DEPLOYMENT_COMPONENT in service.config_layers()


def test_registry_hook_delivers_the_fields() -> None:
    """The hook the deployment form reads carries them -- that is the whole wiring.

    Before RC-78 this hook returned exactly one field, user-env-vars', while the TLS pair
    sat hand-written in the forms layer and could therefore only ever appear on the one
    step that named it.
    """
    assert _TLS_PATH in [e.yaml_path for e in deployment_component_service_editables()]
    assert _ATTACHMENT_PATH in [v.editable.yaml_path for v in deployment_component_service_visualizers()]


def test_deployment_edit_modal_offers_the_certificate_per_component() -> None:
    """modal-edit-deployment-<n>: a TLS choice and a certificate field per component."""
    from opi.forms.visualizers.wizard_sections import build_deployment_edit_section

    section = build_deployment_edit_section(1, component_count=2)
    paths = [child.editable.yaml_path for child in section.editables[0].children or []]
    assert "deployments[1]/components[*]/services/publish-on-web/config/tls" in paths
    assert "deployments[1]/components[*]/services/publish-on-web/config/attachment" in paths


def test_the_form_layer_does_not_name_publish_on_web_for_this_layer() -> None:
    """The fields are the service's; the deployment form must not re-declare them.

    A second definition is how the two drift apart -- which is exactly the state this
    change found: the forms layer had its own copy with its own provider.
    """
    from pathlib import Path

    source = Path("opi/forms/editables/fields/deployments.py").read_text()
    assert "publish-on-web/config/tls" not in source.split("# Focused, read-only")[0]
    assert "DEPLOYMENT_COMP_PUBLISH_TLS_EDITABLE = Editable(" not in source


# --- 2. empty means "follow the component", and shows it ------------------------------


def test_empty_override_changes_nothing() -> None:
    project = _two_deployments()
    assert _tls(project, "web", "staging") == "standard"  # no override -> the component
    project["deployments"][1]["components"][0]["services"] = {"publish-on-web": {"config": {}}}
    assert _tls(project, "web", "staging") == "standard"  # an empty block is not an override


def test_inherit_option_names_what_the_component_says() -> None:
    """An empty select must not read as "no TLS": it names the mode it falls back to."""
    from opi.forms.visualizers.providers import PublishTlsOverrideOptionsProvider

    project = _two_deployments()
    project["components"][0]["services"] = [{"publish-on-web": {"config": {"tls": "passthrough"}}}]
    options = PublishTlsOverrideOptionsProvider(
        yaml_data=project,
        yaml_path="deployments[1]/components[0]/services/publish-on-web/config/tls",
    ).get_options()

    assert options[0]["value"] == ""
    assert "Erven van het component" in options[0]["label"]
    assert "passthrough" in options[0]["label"].lower()
    assert [opt["value"] for opt in options[1:]] == ["standard", "passthrough", "provided"]


def test_inherit_option_falls_back_without_context() -> None:
    """No project data (a bare render): plain wording rather than a guess."""
    from opi.forms.visualizers.providers import PublishTlsOverrideOptionsProvider

    assert PublishTlsOverrideOptionsProvider().get_options()[0]["label"] == "Erven (geen override)"


def test_deployment_edit_modal_renders_the_inherited_mode() -> None:
    from opi.forms.visualizers.wizard_sections import build_deployment_edit_section
    from opi.web.router_wizard import _create_renderer

    project = _two_deployments()
    section = build_deployment_edit_section(1)
    html = _create_renderer().render_fields_from_editables(
        editables=section.editables, yaml_data=project, layout=section.layout or [], edit_mode=True
    )
    assert "Certificaat (alleen voor deze deployment)" in html
    assert "Erven van het component" in html


# --- 3. an override replaces the component's mode, provided included -------------------


def test_override_can_switch_provided_off() -> None:
    """The open question of the plan, measured: the cascade takes the WHOLE config block
    from the first level that names a valid mode, so an override does not merely fill in
    where the component is silent -- it replaces it, certificate and all."""
    project = _two_deployments()
    project["components"][0]["services"] = [
        {"publish-on-web": {"config": {"tls": "provided", "attachment": "prod-cert"}}}
    ]
    project["deployments"][1]["components"][0]["services"] = {"publish-on-web": {"config": {"tls": "standard"}}}

    assert _tls(project, "web") == "provided"  # the component still supplies its own
    assert _tls(project, "web", "staging") == "standard"  # and staging really is off it
    # No certificate is resolved for staging: the mode that needed one is gone.
    handler = ProjectFileHandler.__new__(ProjectFileHandler)
    assert asyncio.run(handler.resolve_publish_on_web_certificate(project, "web", "staging")) is None


def test_provided_override_without_a_certificate_is_refused() -> None:
    """The certificate must come from the level that asks for the mode: the component's
    attachment is not inherited along with a mode the override replaced."""
    from opi.services.catalog.publish_on_web.config_model import PublishOnWebComponentConfig
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="attachment"):
        PublishOnWebComponentConfig.model_validate({"tls": "provided"})

    project = _two_deployments()
    project["deployments"][0]["components"][0]["services"]["publish-on-web"]["config"].pop("attachment")
    handler = ProjectFileHandler.__new__(ProjectFileHandler)
    with pytest.raises(ValueError, match="attachment"):
        asyncio.run(handler.resolve_publish_on_web_certificate(project, "web", "productie"))


# --- 4. two deployments, two ingresses ------------------------------------------------


def test_own_certificate_on_production_and_platform_on_staging() -> None:
    project = _two_deployments()
    assert _tls(project, "web", "productie") == "provided"
    assert _tls(project, "web", "staging") == "standard"

    def render(**extra: object) -> str:
        base = {
            "name": "web-ing",
            "service_name": "web",
            "hostname": "app.rijksapps.nl",
            "path": "/",
            "enable_tls": True,
            "tls_secret_name": "web-tls",
            "external_dns_target": "router.rijksapps.nl",
            "issuer_name": None,
            "cluster_issuer": None,
        }
        return _ENV.get_template("ingress.yaml.jinja").render({**base, **extra})

    # production: the customer's own secret, and no platform issuance for it
    productie = render(provided_tls_secret="productie-web-provided-tls")
    assert 'secretName: "productie-web-provided-tls"' in productie
    assert "cert-manager.io/" not in productie

    # staging: unchanged -- the platform issues, as it did before the override existed
    staging = render(cluster_issuer="letsencrypt-prod")
    assert "cert-manager.io/cluster-issuer: letsencrypt-prod" in staging
    assert "provided-tls" not in staging


# --- 5. the attachment is project-wide: an override is a use --------------------------


def test_override_counts_as_a_use_of_the_attachment() -> None:
    """Several deployments can point at one certificate; the delete guard walks them all,
    or it would report a certificate in use as unused."""
    project = _two_deployments()
    sites = attachment_usage_sites(project)["prod-cert"]
    assert [(s.component, s.deployment, s.kind) for s in sites] == [("web", "productie", USAGE_CERTIFICATE)]


def test_component_and_override_pointing_at_one_certificate_are_both_reported() -> None:
    """Two sites, one certificate: the component's own and one deployment's override (RC-96).

    The plan's point 5 in its sharpest form. A guard that walked only the component list
    would call the override's certificate unused, and a guard that walked only the
    deployment components would say the same about the component's -- so the answer has to
    name both places, which is what the delete refusal shows the caller.
    """
    project = _two_deployments()
    project["components"][0]["services"] = [
        {"publish-on-web": {"config": {"tls": "provided", "attachment": "prod-cert"}}}
    ]
    sites = attachment_usage_sites(project)["prod-cert"]
    assert {(s.component, s.deployment) for s in sites} == {("web", None), ("web", "productie")}
    assert all(s.kind == USAGE_CERTIFICATE for s in sites)
    # And as the API hands it to a client: the labels distinguish the two places.
    assert {s.label for s in sites} == {"web", "web (productie)"}


def test_removing_a_certificate_used_by_an_override_is_refused() -> None:
    """Even with the in-use acknowledgement: moving a site off its own certificate is a
    decision, not a side effect of deleting a file."""
    from opi.manager.project_manager import ProjectManager

    project = _two_deployments()
    manager = ProjectManager.__new__(ProjectManager)

    async def get_contents() -> dict:
        return project

    async def get_name() -> str:
        return "demo"

    manager.get_contents = get_contents  # type: ignore[method-assign]
    manager.get_name = get_name  # type: ignore[method-assign]

    result = asyncio.run(manager.remove_attachment("prod-cert", confirm_in_use=True))
    assert result["success"] is False
    assert result["error_type"] == "in_use"
    assert "productie" in result["error"]
    # and the catalog entry is still there
    assert project["services"][0]["attachments"]["data"][0]["id"] == "prod-cert"


# --- 6. the file still validates ------------------------------------------------------


def test_project_with_a_per_deployment_certificate_validates() -> None:
    from opi.core.project_schema import validate_project_schema
    from opi.manager.project_validation import validate_project_structure

    project = {
        "schema-version": 2,
        "name": "demo",
        "users": [{"email": "a@b.nl", "role": "admin"}],
        "clusters": ["odcn-production"],
        **{k: v for k, v in _two_deployments().items() if k in ("services", "components", "deployments")},
    }
    project["services"].append("publish-on-web")  # the component's reference must resolve
    for deployment in project["deployments"]:
        deployment["cluster"] = "odcn-production"
        deployment["namespace"] = "demo"
    validate_project_schema(project)
    # And the config itself against the service's model: the override is validated with
    # the same PublishOnWebComponentConfig as the component entry it overrides.
    asyncio.run(validate_project_structure(project))
