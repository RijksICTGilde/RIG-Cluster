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


# ---------------------------------------------------------------------------
# Guard (WP1): every consumer swept in the audit must read a *configured* service
# in all three formats. Each test below reverts to red if its fix is undone,
# because it exercises the record form -- the one the old raw-structure reads
# (``next(iter(keys))``, ``"x" in entry``, membership-on-dict) silently missed.
# ---------------------------------------------------------------------------


class TestConfiguredServiceReaders:
    """Manager/handler readers that fetch a service's config block by service type."""

    def test_minio_config_read_in_every_format(self) -> None:
        from opi.manager.minio_manager import MinioManager

        cfg = {"buckets": ["a"]}
        for entry in _formats(ServiceType.MINIO_STORAGE.value, config=cfg):
            if entry is None:
                continue
            project = {"services": [entry], "deployments": [{"name": "dep"}]}
            # self is unused by the reader; pass None to avoid the manager's I/O deps.
            assert MinioManager._get_minio_service_config(None, project, "dep") == cfg

    def test_redis_config_read_in_every_format(self) -> None:
        from opi.manager.redis_manager import RedisManager

        cfg = {"maxmemory": "64mb"}
        for entry in _formats(ServiceType.REDIS.value, config=cfg):
            if entry is None:
                continue
            assert RedisManager._get_redis_service_config({"services": [entry]}) == cfg

    def test_bootstrap_keycloak_config_read_in_every_format(self) -> None:
        from opi.bootstrap.keycloak_setup import KeycloakSetup

        cfg = {"template": "algoritmeregister", "restrict_access": {"enabled": True}}
        for entry in _formats(ServiceType.KEYCLOAK.value, config=cfg):
            if entry is None:
                continue
            extracted = KeycloakSetup._extract_keycloak_service_config(None, {"services": [entry]})
            assert extracted["template"] == "algoritmeregister"
            assert extracted["restrict_access"] == {"enabled": True}


class TestPublishOnWebCertificateGuard:
    """The attachment delete-guard scans publish-on-web ``provided`` certificates; a
    record-form entry must not slip past it (else an in-use cert looks deletable)."""

    def test_provided_certificate_detected_in_every_format(self) -> None:
        from opi.handlers.project_file_handler import attachment_is_referenced

        cfg = {"tls": "provided", "attachment": "my-cert"}
        for entry in _formats(ServiceType.PUBLISH_ON_WEB.value, config=cfg):
            if entry is None:
                continue
            project = {"services": [entry], "components": [], "deployments": []}
            assert attachment_is_referenced(project, "my-cert") is True


class TestComponentsUsingServiceGuard:
    """Backup discovery resolves which components use a storage/db service; a storage
    service in the component *record* form (``{reference, config}``) must be found."""

    def test_storage_component_found_in_record_form(self) -> None:
        from opi.handlers.project_file_handler import ProjectFileHandler

        project = {
            "components": [
                {
                    "name": "app",
                    "services": [{"reference": ServiceType.PERSISTENT_STORAGE.value, "config": [{"name": "data"}]}],
                }
            ],
            "deployments": [{"name": "dep", "components": [{"reference": "app"}]}],
        }
        results = ProjectFileHandler().get_components_using_service(
            project, "dep", [ServiceType.PERSISTENT_STORAGE.value]
        )
        assert [r["component_name"] for r in results] == ["app"]


class TestFilterEmptyServiceEntries:
    """The empty-entry filter must keep a uniform record and still drop empties, without
    the single-key assumption its ``break`` used to make."""

    def test_keeps_record_and_bare_string_drops_empty(self) -> None:
        from opi.handlers.project_file_handler import _filter_empty_service_entries

        kept = _filter_empty_service_entries(
            [
                "publish-on-web",
                {"name": "keycloak", "config": {"template": "sso-only"}},
                {"reference": "persistent-storage", "config": [{"name": "data"}]},
                {"metrics-scraper": None},
                {"redis": {}},
            ]
        )
        assert "publish-on-web" in kept
        assert {"name": "keycloak", "config": {"template": "sso-only"}} in kept
        assert {"reference": "persistent-storage", "config": [{"name": "data"}]} in kept
        assert {"metrics-scraper": None} not in kept
        assert {"redis": {}} not in kept


# (file, forbidden substring) pairs -- each is a raw-structure read the audit removed.
_FORBIDDEN_RAW_READS = [
    ("opi/manager/minio_manager.py", "next(iter(service.keys()))"),
    ("opi/manager/redis_manager.py", "next(iter(service.keys()))"),
    ("opi/bootstrap/keycloak_setup.py", '"keycloak" in service'),
    ("opi/handlers/project_file_handler.py", "next(iter(service.keys()))"),
]


class TestNoRawEntryReadsReintroduced:
    """Source guard: the swept files must not reintroduce the raw-structure reads that
    miss the record form. This fails loudly if a fix is reverted to the old shape."""

    def test_forbidden_patterns_absent(self) -> None:
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        offenders = [f"{rel}: {needle}" for rel, needle in _FORBIDDEN_RAW_READS if needle in (root / rel).read_text()]
        assert not offenders, "raw service-entry read reintroduced:\n" + "\n".join(offenders)
