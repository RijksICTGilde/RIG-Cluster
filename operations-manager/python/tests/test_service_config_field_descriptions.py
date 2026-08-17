"""Every config field a service exposes says what it is (RC-38).

Measured on 6 August 2026, before this: 151 fields across the 47 models the service
configs are built from, and 147 of them had no description. The knowledge was not
missing -- most fields carried a ``#:`` comment right above them -- it just never left
the source file. Everything downstream that is generated from the models (the committed
schema fragments, the typed config endpoints, the OpenAPI document a client is generated
from) therefore documented nothing, and the API said "banner: string" where the code
said what a banner is.

So the rule is: a field of a service config model carries a ``description``. Not a
convention, a test -- because a new field with no description is exactly as invisible as
the 147 were, and the next person adding one sees no gap.

Scope is the whole reachable graph, not just the top level: a nested value object
(``resources.limits.memory``, a cross-domain rule's ``from.component``) is just as much a
field the caller sends, and skipping it would leave most of the surface undocumented
while the test stayed green.
"""

from __future__ import annotations

import pytest
from opi.services.catalog.base import ConfigLayer
from opi.services.registry import SERVICES
from pydantic import BaseModel


def _nested_models(annotation: object) -> list[type[BaseModel]]:
    """Every pydantic model reachable through one field annotation."""
    candidates = [annotation, *getattr(annotation, "__args__", ())]
    for candidate in list(candidates):
        candidates.extend(getattr(candidate, "__args__", ()))
    return [c for c in candidates if isinstance(c, type) and issubclass(c, BaseModel)]


def _reachable(model: type[BaseModel], seen: set[type[BaseModel]]) -> None:
    if model in seen:
        return
    seen.add(model)
    for field in model.model_fields.values():
        for nested in _nested_models(field.annotation):
            _reachable(nested, seen)


def _models_of(service) -> set[type[BaseModel]]:
    """Every model reachable from a service's config and data models, all layers."""
    seen: set[type[BaseModel]] = set()
    for layer in ConfigLayer:
        for model in (service.config_model_for(layer), service.data_model_for(layer)):
            if model is not None:
                _reachable(model, seen)
    return seen


_SERVICES = [s for s in SERVICES.values() if _models_of(s)]
_IDS = [s.service_type.value for s in _SERVICES]


class TestEveryConfigFieldIsDocumented:
    @pytest.mark.parametrize("service", _SERVICES, ids=_IDS)
    def test_every_field_carries_a_description(self, service) -> None:
        undocumented = [
            f"{model.__name__}.{name}"
            for model in _models_of(service)
            for name, field in model.model_fields.items()
            if not (field.description or "").strip()
        ]
        assert not undocumented, (
            f"Service '{service.service_type.value}' exposes config fields with no description: "
            f"{sorted(undocumented)}. Add description=... on the Field; it is what the API, the schema "
            f"fragment and the OpenAPI document show a caller."
        )

    def test_the_whole_catalog_is_covered(self) -> None:
        # Guards the parametrised test above from passing on an empty list, and pins that
        # the measurement is over the real catalog rather than a handful of services.
        assert len(_SERVICES) >= 14
        total = sum(len(model.model_fields) for service in _SERVICES for model in _models_of(service))
        assert total >= 140


class TestTheDescriptionsReachWhatIsGenerated:
    """A description that stops at the model would fix nothing that was actually broken."""

    def test_a_committed_schema_fragment_carries_them(self) -> None:
        from opi.services.config_schema import fragment_path
        from opi.services.services_enums import ServiceType

        fragment = fragment_path(SERVICES[ServiceType.HEALTH_CHECK]).read_text(encoding="utf-8")
        assert "description" in fragment
        assert "images run non-root" in fragment

    def test_the_openapi_document_carries_them(self) -> None:
        from opi.server import app

        schema = app.openapi()["components"]["schemas"]["HealthCheckConfig"]
        for name, prop in schema["properties"].items():
            assert prop.get("description"), f"HealthCheckConfig.{name} reaches the API undocumented"
