"""End-to-end test suite for the ``health-check`` service - AND the template for
testing any platform service.

WHY THIS FILE EXISTS TWICE OVER
-------------------------------
1. It is the missing coverage for the health-check service: that its config saves
   to the right place through the wizard, and that it actually changes the
   rendered deployment probes.
2. It is meant to be *copied* when a new service is added. Adding a service that
   is untested at these layers is how a "green" suite ships a broken wizard, so
   copy this file, rename it, and re-point the four LEVELS below at the new
   service. Each LEVEL tests a different seam; a new service needs all four.

THE FOUR LEVELS (fast, deterministic, no cluster) + the UI level
----------------------------------------------------------------
LEVEL 1 - config validation:   a good config passes, a bad one is REJECTED with a
                               message that names the service. (Guards the schema.)
LEVEL 2 - wizard save/round-trip: a component-step POST (services list + the
                               virtual ``_services-config`` key) is processed by
                               EditableFormProcessor and lands in the project file
                               at the RIGHT yaml path, in the RIGHT shape, with
                               ``_services-config`` stripped. (Guards "I configured
                               it in the wizard and it saved".)
LEVEL 3 - manifest contribution: the service reads a component's config and emits
                               the template variables the deployment needs.
                               (Guards config -> intent.)
LEVEL 4 - rendered manifest:   those variables actually render into the deployment
                               YAML (the probe block changes). (Guards intent ->
                               K8s object. This is the seam a golden test with
                               hard-coded vars does NOT cover.)
LEVEL 5 - UI wizard (Playwright): drive the real wizard in a browser, configure
                               the service on a component, submit, and read the
                               saved project file back. See the note at the bottom;
                               lives under tests/e2e/ because it needs the browser
                               fixtures.

Run: uv run pytest tests/test_service_health_check.py -q
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
from opi.core.project_schema import ProjectIntegrityError
from opi.forms.editables.processor import EditableFormProcessor
from opi.forms.visualizers.fields.components import COMPONENTS_SEQUENCE
from opi.generation.manifests import render_template
from opi.manager.project_validation import validate_service_configs
from opi.services.catalog.base import ManifestContext
from opi.services.registry import get_service
from opi.services.services_enums import ServiceType

# Realistic per-component deployment variables (the dict ProjectManager builds before
# rendering deployment.yaml.jinja). Reused so LEVEL 4 renders what the real pipeline
# renders; a new service's template test overrides only the keys its service touches.
from tests.test_golden_manifests import _deployment_vars

SERVICE = ServiceType.HEALTH_CHECK
SERVICE_NAME = SERVICE.value  # "health-check"


# ---------------------------------------------------------------------------
# LEVEL 1 - config validation (component level)
#
# validate_service_configs runs inside validate_project_structure on every write
# path (create, edit, API). A service whose config model is not wired in here can
# be saved with garbage. Test one good config and at least one rejected shape.
# ---------------------------------------------------------------------------


def _project_with_health_check(config: dict[str, Any]) -> dict[str, Any]:
    """A minimal project whose single component carries the health-check service.

    health-check is a COMPONENT-level service in the uniform record shape
    ``{name, config}`` (the same shape publish-on-web / attachments use)."""
    return {
        "components": [
            {
                "name": "web",
                "ports": {"inbound": [8080]},
                "services": [{"name": SERVICE_NAME, "config": config}],
            }
        ]
    }


def test_level1_valid_config_passes() -> None:
    validate_service_configs(
        _project_with_health_check(
            {"scheme": "http", "port": 8081, "liveness-path": "/health/live", "readiness-path": "/health/ready"}
        )
    )  # must not raise


def test_level1_unknown_scheme_is_rejected() -> None:
    # scheme is a closed set; an unknown value must fail and NAME the service so the
    # user can find it.
    with pytest.raises(ProjectIntegrityError, match=SERVICE_NAME):
        validate_service_configs(_project_with_health_check({"scheme": "grpc"}))


def test_level1_unknown_field_is_rejected() -> None:
    # The config model is closed (extra=forbid): a typo'd/unsupported field fails
    # rather than silently vanishing.
    with pytest.raises(ProjectIntegrityError, match=SERVICE_NAME):
        validate_service_configs(_project_with_health_check({"timeout": 5}))


# ---------------------------------------------------------------------------
# LEVEL 2 - wizard save / round-trip
#
# The component wizard step POSTs the selected services as a flat list plus a
# parallel virtual key ``_services-config`` (kept separate so sibling services
# under the same path do not collide). The final submit runs it through
# EditableFormProcessor, which must fold that config back onto the named service
# and drop the virtual key. This is the "I set it in the wizard, did it save?" test.
# ---------------------------------------------------------------------------


def _find_service_config(services: list, service_name: str) -> dict | None:
    """Config dict for a named service in a mixed services list (string | {name}|{key})."""
    for svc in services:
        if isinstance(svc, dict):
            if svc.get("name") == service_name:  # uniform {name, config}
                return svc.get("config")
            if service_name in svc:  # legacy {service: {config: ...}}
                inner = svc[service_name]
                return inner.get("config") if isinstance(inner, dict) else None
    return None


@pytest.mark.asyncio
async def test_level2_wizard_submit_saves_config_to_the_right_place() -> None:
    # Exactly what the component step sends: services flat + _services-config virtual.
    # Note the port arrives as a STRING from the HTML form.
    post = {
        "components": [
            {
                "name": "web",
                "image": "nginx",
                "ports": {"inbound": ["8080"]},
                "services": ["publish-on-web", SERVICE_NAME],
                "path": "/",
                "_services-config": [
                    {
                        SERVICE_NAME: {
                            "config": {
                                "scheme": "http",
                                "port": "8081",
                                "liveness-path": "/health/live",
                                "readiness-path": "/health/ready",
                            }
                        }
                    }
                ],
            }
        ]
    }

    processor = EditableFormProcessor()
    result, errors = await processor.process_json_submission(
        copy.deepcopy(post), [COMPONENTS_SEQUENCE], copy.deepcopy(post), strip_transients=True
    )

    assert not errors, f"submit reported errors: {errors}"
    component = result["components"][0]
    # The virtual key must never leak into the saved project file.
    assert "_services-config" not in component
    # Config landed on the named service, in the config wrapper.
    config = _find_service_config(component["services"], SERVICE_NAME)
    assert config is not None, f"health-check config missing after submit. services={component['services']}"
    assert config == {
        "scheme": "http",
        "port": 8081,  # coerced string -> int by the IntegerConverter
        "liveness-path": "/health/live",
        "readiness-path": "/health/ready",
    }


@pytest.mark.asyncio
async def test_level2_existing_config_survives_a_resubmit() -> None:
    # A later edit/submit whose merged data already carries the config (e.g. the
    # summary showed it) must PRESERVE it, not drop it on the way through.
    merged = {
        "components": [
            {
                "name": "web",
                "image": "nginx",
                "ports": {"inbound": [8080]},
                "services": [
                    "publish-on-web",
                    {"name": SERVICE_NAME, "config": {"scheme": "https", "liveness-path": "/live"}},
                ],
                "path": "/",
            }
        ]
    }
    processor = EditableFormProcessor()
    result, errors = await processor.process_json_submission(
        copy.deepcopy(merged), [COMPONENTS_SEQUENCE], copy.deepcopy(merged), strip_transients=True
    )
    assert not errors
    config = _find_service_config(result["components"][0]["services"], SERVICE_NAME)
    assert config == {"scheme": "https", "liveness-path": "/live"}


# ---------------------------------------------------------------------------
# LEVEL 3 - manifest contribution (config -> template variables)
#
# The service turns a component's config into the variables the deployment template
# consumes. Test that only the keys the user set are overridden, and the important
# guard rails (scheme: none disables; no inbound port -> no probe at all).
# ---------------------------------------------------------------------------


def _manifest_ctx(component_def: dict[str, Any]) -> ManifestContext:
    return ManifestContext(
        deployment_name="prod",
        project_data={},
        unique_name="prod-web",
        cluster="sandboxed-local",
        get_secret=lambda *a, **k: None,
        component_def=component_def,
    )


def test_level3_contributes_probe_vars_from_config() -> None:
    ctx = _manifest_ctx(
        {
            "ports": {"inbound": [8443]},
            "services": [
                {
                    "reference": SERVICE_NAME,
                    "config": {
                        "scheme": "http",
                        "port": 8081,
                        "liveness-path": "/health/live",
                        "readiness-path": "/health/ready",
                    },
                }
            ],
        }
    )
    contribution = get_service(SERVICE).contribute_manifest_context(ctx)
    assert contribution.template_vars == {
        "probe_scheme": "http",
        "probe_port": 8081,
        "probe_liveness_path": "/health/live",
        "probe_readiness_path": "/health/ready",
    }


def test_level3_scheme_none_disables_probes() -> None:
    ctx = _manifest_ctx(
        {"ports": {"inbound": [8443]}, "services": [{"reference": SERVICE_NAME, "config": {"scheme": "none"}}]}
    )
    contribution = get_service(SERVICE).contribute_manifest_context(ctx)
    assert contribution.template_vars == {"probe_scheme": "none"}


def test_level3_no_inbound_port_contributes_no_probe() -> None:
    # A component with no inbound port cannot be probed; selecting the service must
    # not resurrect one.
    ctx = _manifest_ctx(
        {"ports": {"inbound": []}, "services": [{"reference": SERVICE_NAME, "config": {"scheme": "http"}}]}
    )
    contribution = get_service(SERVICE).contribute_manifest_context(ctx)
    assert contribution.template_vars == {}


# ---------------------------------------------------------------------------
# LEVEL 4 - rendered manifest (config -> actual deployment YAML)
#
# The seam a golden test with hard-coded probe vars does not cover: feed the LEVEL 3
# contribution into the SAME template the pipeline renders, and assert the probe
# block in the output actually reflects the config. This proves config -> K8s object.
# ---------------------------------------------------------------------------


def _render_deployment_for_health_check(config: dict[str, Any], *, inbound: list[int]) -> str:
    component_def = {"ports": {"inbound": inbound}, "services": [{"reference": SERVICE_NAME, "config": config}]}
    contribution = get_service(SERVICE).contribute_manifest_context(_manifest_ctx(component_def))
    variables = _deployment_vars(**contribution.template_vars)
    return render_template("deployment.yaml.jinja", variables)


def test_level4_http_config_renders_httpget_probes() -> None:
    rendered = _render_deployment_for_health_check(
        {"scheme": "http", "port": 8081, "liveness-path": "/health/live", "readiness-path": "/health/ready"},
        inbound=[8080],
    )
    # httpGet probes on the monitoring port with the configured paths + scheme.
    assert "httpGet:" in rendered
    assert "port: 8081" in rendered
    assert 'path: "/health/live"' in rendered or "path: /health/live" in rendered
    assert 'path: "/health/ready"' in rendered or "path: /health/ready" in rendered
    assert "scheme: HTTP" in rendered
    # Not a bare tcp socket probe anymore.
    assert "tcpSocket:" not in rendered


def test_level4_scheme_none_renders_no_probes() -> None:
    rendered = _render_deployment_for_health_check({"scheme": "none"}, inbound=[8080])
    assert "livenessProbe:" not in rendered
    assert "readinessProbe:" not in rendered
    assert "startupProbe:" not in rendered


# ---------------------------------------------------------------------------
# LEVEL 5 - UI wizard (Playwright), the browser seam
#
# The four levels above are pure and fast, but they do NOT prove the wizard renders
# the config fields, wires @change correctly, and posts what LEVEL 2 expects. For
# that, add a test under tests/e2e/ (browser fixtures live there). Skeleton:
#
#   @pytest.mark.e2e
#   def test_health_check_configurable_in_wizard(app_server, auth_page):
#       wizard = WizardHelper(auth_page, app_server)
#       wizard.open_create_wizard()
#       wizard.fill_identity(display_name=_unique(), description="hc")
#       wizard.click_next()
#       wizard.fill_services(["publish-on-web"]); wizard.click_next()
#       wizard.fill_team(email="test@example.com"); wizard.click_next()
#       wizard.fill_component(name="web", image="nginx:latest")
#       # select health-check on the component, then set the probe fields it reveals
#       auth_page.check("input[name='components[0]/services[]'][value='health-check']")
#       auth_page.select_option("select[name*='health-check}/config/scheme']", "http")
#       auth_page.fill("input[name*='health-check}/config/liveness-path']", "/health/live")
#       ...
#       wizard.submit()
#       # then read the project file back from Forgejo (sandbox) or the mocked store
#       # (local) and assert services[...] carries the health-check config.
#
# Local run needs the health-check config step to render in the mocked test server;
# sandbox run reads the real committed file. Keep it in tests/e2e/ so the default
# unit run stays browserless.
