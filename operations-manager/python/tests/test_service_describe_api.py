"""The service catalog and the per-service describe (RC-59).

This is the endpoint an agent that has never seen the portal reads first: it has to be
able to find out which services exist, what each one does, where it is applied, how it is
configured and which environment variables it hands a component -- without a human
translating the UI for it.

Two things are guarded here, and the second is the reason the first stays true:

* the shape itself -- the catalog carries the fields you need to *choose* a service, and
  the describe carries what you need to *apply* it;
* coverage -- every registered ``ServiceType`` produces a complete describe. Without that,
  a service added in six months arrives with no explanation and an empty variable list,
  and nothing says so. Same discipline as ``tests/test_service_config_layers.py``, which
  holds every service to its config declarations.

Everything the describe returns is a projection of what the service already declares, so
these tests compare against the registry rather than against a copy of the expected text:
a test with its own copy of the answer is a second documentation system too.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException
from opi.api.v2.router import describe_service_v2, list_configurable_services_v2
from opi.services.catalog.base import ConfigLayer
from opi.services.help_text import service_help_markdown
from opi.services.registry import SERVICES, get_service
from opi.services.services import ServiceAdapter
from opi.services.services_enums import ServiceType

_SERVICES = sorted(ServiceType, key=lambda s: s.value)


def _describe(service_name: str):
    return asyncio.run(describe_service_v2(service_name))


def _catalog():
    return asyncio.run(list_configurable_services_v2())


# ---------------------------------------------------------------------------
# The catalog is enough to choose with
# ---------------------------------------------------------------------------


def test_catalog_lists_every_registered_service() -> None:
    entries = _catalog().services
    assert [entry.name for entry in entries] == sorted(s.value for s in SERVICES)


@pytest.mark.parametrize("service", _SERVICES, ids=lambda s: s.value)
def test_catalog_reports_the_declared_nature_of_a_service(service: ServiceType) -> None:
    """kind/binding/hidden/requires come straight off the definition.

    Without these a client cannot tell what it may pick itself (`user`), what the
    platform always runs (`system`), what is deliberately kept out of the picker
    (`hidden`) and what another service needs first (`requires`).
    """
    definition = ServiceAdapter.get_service_definition(service)
    entry = next(e for e in _catalog().services if e.name == service.value)

    assert entry.kind is definition.kind
    assert entry.binding is definition.binding
    assert entry.hidden is definition.hidden
    assert entry.requires == list(definition.requires)


def test_catalog_reports_a_dependency_that_exists() -> None:
    """At least one service declares a requirement, so the field is not always empty."""
    with_requires = [entry for entry in _catalog().services if entry.requires]
    assert with_requires, "no service declares `requires`; this field would be untested"
    for entry in with_requires:
        for path in entry.requires:
            assert path.startswith("services/"), f"{entry.name} requires {path!r}, which is not a yaml path"


# ---------------------------------------------------------------------------
# Coverage: every service describes itself completely
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("service", _SERVICES, ids=lambda s: s.value)
def test_every_service_has_a_describe_without_empty_mandatory_fields(service: ServiceType) -> None:
    """A new service without a description or with a broken declaration fails here.

    The variable list is deliberately allowed to be empty (most services expose none)
    but never absent -- "no variables" and "we did not look" have to be different
    answers, so the field is always a list.
    """
    described = _describe(service.value)

    assert described.name == service.value
    assert described.description.strip(), f"{service.value} has no description"
    assert described.explanation.strip(), f"{service.value} returns no explanation"
    assert described.explanation == service_help_markdown(service), (
        "the explanation is the service's own help document, not a second text written for the API"
    )
    assert described.kind is not None
    assert described.binding is not None
    assert isinstance(described.variables, list)
    assert isinstance(described.requires, list)
    assert described.cleanup_strategy is not None


@pytest.mark.parametrize("service", _SERVICES, ids=lambda s: s.value)
def test_the_describe_agrees_with_the_catalog_row(service: ServiceType) -> None:
    """One registry, two views: they cannot say different things about the same service."""
    entry = next(e for e in _catalog().services if e.name == service.value)
    described = _describe(service.value)

    assert described.description == entry.description
    assert described.kind is entry.kind
    assert described.binding is entry.binding
    assert described.hidden is entry.hidden
    assert described.requires == entry.requires
    assert described.configurable is entry.configurable
    assert described.config_schema_version == entry.config_schema_version
    assert described.value_targets == entry.value_targets
    assert {layer.target for layer in described.layers} >= set(entry.targets)


@pytest.mark.parametrize("service", _SERVICES, ids=lambda s: s.value)
def test_the_variables_are_the_ones_the_service_declares(service: ServiceType) -> None:
    definition = ServiceAdapter.get_service_definition(service)
    described = _describe(service.value)

    assert [v.name for v in described.variables] == [v.name for v in definition.variables]
    for described_variable, declared in zip(described.variables, definition.variables, strict=True):
        assert described_variable.description == declared.description
        assert described_variable.source == declared.source
        assert described_variable.aliases == list(declared.aliases)
        assert described_variable.secret_key == declared.secret_key


def test_a_service_with_variables_reports_them_in_full() -> None:
    """The gap RC-59 names: eleven services declare variables and none of it was in the API."""
    described = _describe(ServiceType.POSTGRESQL_DATABASE.value)
    by_name = {variable.name: variable for variable in described.variables}

    assert "DATABASE_PASSWORD" in by_name
    password = by_name["DATABASE_PASSWORD"]
    assert password.source == "secret"
    assert password.secret_key == "password"
    assert password.aliases, "the alias of DATABASE_PASSWORD is what a client would otherwise have to guess"


def test_a_service_without_variables_reports_an_empty_list() -> None:
    described = _describe(ServiceType.SLEEP_MODE.value)
    assert described.variables == []


# ---------------------------------------------------------------------------
# Where you apply it
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("service", _SERVICES, ids=lambda s: s.value)
def test_every_config_layer_of_a_service_is_described(service: ServiceType) -> None:
    """A layer the service carries config on is named, with the path it takes in the file.

    Including the layers that deliberately have no form: "here it goes through the API but
    on purpose not through a form" is exactly what an API client needs to be told.
    """
    described = _describe(service.value)
    declared = get_service(service)
    expected = set(declared.config_layers()) | set(declared.form_exempt_layers)

    assert {layer.target for layer in described.layers} >= expected
    for layer in described.layers:
        assert layer.yaml_path, f"{service.value} describes {layer.target} without a yaml path"


@pytest.mark.parametrize("service", _SERVICES, ids=lambda s: s.value)
def test_a_layer_without_a_form_says_why(service: ServiceType) -> None:
    declared = get_service(service)
    described = _describe(service.value)

    for layer in described.layers:
        reason = declared.form_exempt_layers.get(layer.target)
        assert layer.form_exempt_reason == reason
        if reason:
            assert reason.strip(), f"{service.value} exempts {layer.target} from a form without a reason"


def test_a_configurable_layer_points_at_the_endpoint_that_writes_it() -> None:
    """The schema is not copied into the describe; the route that carries it is named."""
    described = _describe(ServiceType.AUTHORIZATION_WALL.value)
    project = next(layer for layer in described.layers if layer.target is ConfigLayer.PROJECT)

    assert project.config_endpoint == ("PUT /api/v2/projects/{project_name}/services/authorization-wall/config/project")
    assert described.config_schema_version, "a configurable service states its config schema version"


def test_a_layer_that_is_only_read_carries_no_write_endpoint() -> None:
    """Not every declared layer has a write route, and the describe must not pretend it does."""
    endpoints = {
        (service.value, layer.target): layer.config_endpoint
        for service in _SERVICES
        for layer in _describe(service.value).layers
    }
    assert any(endpoint is None for endpoint in endpoints.values())
    for (name, _target), endpoint in endpoints.items():
        if endpoint is not None:
            assert endpoint.startswith("PUT /api/v2/projects/{project_name}/services/"), endpoint
            assert f"/services/{name}/config/" in endpoint


# ---------------------------------------------------------------------------
# What it costs when it goes, and what happens on an unknown name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("service", _SERVICES, ids=lambda s: s.value)
def test_removal_consequences_come_from_the_definition(service: ServiceType) -> None:
    definition = ServiceAdapter.get_service_definition(service)
    described = _describe(service.value)

    assert described.cleanup_strategy is definition.cleanup_strategy
    assert described.backup_label == definition.backup_label


def test_an_unknown_service_is_a_404_that_names_the_known_ones() -> None:
    with pytest.raises(HTTPException) as excinfo:
        _describe("does-not-exist")

    assert excinfo.value.status_code == 404
    assert "postgresql-database" in str(excinfo.value.detail), (
        "a client guessing an identifier needs the list of valid ones more than it needs the refusal"
    )
