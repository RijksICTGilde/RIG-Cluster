"""Every consumer of ``services`` must read all three entry formats.

A service entry is a bare string, the legacy single-key dict (``{keycloak: {...}}``)
or the uniform record (``{name: keycloak, config: {...}}``). An entry only becomes a
record once it carries config, which is why hand-rolled ``"name" in entry`` checks
survived so long: bare selections kept working and only configured services broke.

Concretely: ``restrict-access`` on keycloak silently did nothing, and namespace
infrastructure went undetected on project delete.
"""

import pytest
from opi.services import ServiceAdapter, ServiceType
from opi.services.services import service_entry_config, service_entry_name, service_entry_type


def _formats(name: str, config: dict | None = None, type_: str | None = None) -> list:
    """The same service expressed in all three on-disk formats."""
    body: dict = {}
    if type_ is not None:
        body["type"] = type_
    if config is not None:
        body["config"] = config
    return [
        name if not body else None,
        {name: body} if body else {name: {}},
        {"name": name, **body},
    ]


class TestEntryHelpers:
    @pytest.mark.parametrize("entry", [e for e in _formats("keycloak") if e is not None])
    def test_name_resolves_for_every_format(self, entry) -> None:
        assert service_entry_name(entry) == "keycloak"

    def test_config_resolves_for_every_format(self) -> None:
        cfg = {"template": "sso-support"}
        legacy = {"keycloak": {"config": cfg}}
        record = {"name": "keycloak", "config": cfg}
        assert service_entry_config(legacy) == cfg
        assert service_entry_config(record) == cfg
        assert service_entry_config("keycloak") is None

    def test_type_resolves_for_every_format(self) -> None:
        legacy = {"keycloak": {"type": "external", "config": {}}}
        record = {"name": "keycloak", "type": "external", "config": {}}
        assert service_entry_type(legacy) == "external"
        assert service_entry_type(record) == "external"
        assert service_entry_type("keycloak") is None


class TestNamespaceInfrastructureDetection:
    """Missed detection here leaves the namespace database/redis behind on delete."""

    @pytest.mark.parametrize(
        "entry",
        [
            ServiceType.NAMESPACE_POSTGRESQL_DATABASE.value,
            {ServiceType.NAMESPACE_POSTGRESQL_DATABASE.value: {"config": {"size": "1Gi"}}},
            {"name": ServiceType.NAMESPACE_POSTGRESQL_DATABASE.value, "config": {"size": "1Gi"}},
        ],
    )
    def test_detected_in_every_format(self, entry) -> None:
        assert ServiceAdapter.project_uses_infrastructure_namespace({"services": ["publish-on-web", entry]}) is True

    def test_absent_when_not_selected(self) -> None:
        project = {"services": ["publish-on-web", {"name": "keycloak", "config": {"template": "sso-only"}}]}
        assert ServiceAdapter.project_uses_infrastructure_namespace(project) is False


class TestComponentServiceNameExtraction:
    """The enforcer that validates component services must resolve every format.

    Component entries key the service on ``reference``, not ``name``. The extractor
    only special-cased ``name`` and otherwise returned the raw dict keys, so a
    configured component service produced the literal names "reference" and "config"
    and the save failed with "gebruikt ongeldige services: reference, config".
    """

    def test_reference_record_resolves_to_the_service_name(self) -> None:
        from opi.forms.editables.enforcers import extract_service_names

        entries = [
            "temp-storage",
            {"reference": "publish-on-web", "config": {"tls": "standard"}},
            {"reference": "persistent-storage", "config": [{"name": "data", "mount-path": "/data"}]},
            {"name": "keycloak", "config": {"template": "sso-only"}},
            {"postgresql-database": {"config": {}}},
        ]

        assert extract_service_names(entries) == [
            "temp-storage",
            "publish-on-web",
            "persistent-storage",
            "keycloak",
            "postgresql-database",
        ]

    def test_component_with_configured_services_passes_the_enforcer(self) -> None:
        import asyncio

        from opi.forms.editables.enforcers import ComponentServicesEnforcer

        value = {
            "services": ["publish-on-web", "persistent-storage"],
            "components": [
                {
                    "name": "headscale",
                    "services": [
                        {"reference": "publish-on-web", "config": {"tls": "standard"}},
                        {"reference": "persistent-storage", "config": [{"name": "data"}]},
                    ],
                }
            ],
        }

        # Must not raise: both referenced services are selected at project level.
        asyncio.run(ComponentServicesEnforcer().enforce(value, {}))
