"""Tests for ServiceAdapter and ServiceType."""

import pytest
from opi.services.services import ServiceAdapter, ServiceDefinition, VariableDefinition
from opi.services.services_enums import ServiceBinding, ServiceType


class TestServiceType:
    """Tests for the ServiceType enum."""

    def test_all_enum_values_exist(self):
        expected = {
            "publish-on-web",
            "keycloak",
            "persistent-storage",
            "temp-storage",
            "postgresql-database",
            "namespace-postgresql-database",
            "minio-storage",
            "redis",
            "namespace-redis",
            "authorization-wall",
            "metrics-scraper",
            "health-check",
            "platform",
            "attachments",
            "sleep-mode",
            "invite",
            "resource-tuning",
            "deployment-health",
            "cross-domain-access",
            "user-env-vars",
            "aliases",
        }
        actual = {st.value for st in ServiceType}
        assert actual == expected

    def test_enum_count(self):
        # 21 sinds de gezondheidscheck een systeemdienst werd (RC-28); 20 sinds
        # user-env-vars en aliases systeemdiensten werden (RC-25); daarvoor 18, sinds
        # cross-domain-access en resource-tuning er allebei bij kwamen.
        assert len(ServiceType) == 21


class TestGetAllServices:
    """Tests for ServiceAdapter.get_all_services."""

    def test_returns_all_service_types(self):
        result = ServiceAdapter.get_all_services()
        assert set(result) == set(ServiceType)

    def test_returns_list(self):
        result = ServiceAdapter.get_all_services()
        assert isinstance(result, list)


class TestGetServiceDefinition:
    """Tests for ServiceAdapter.get_service_definition."""

    def test_returns_service_definition(self):
        defn = ServiceAdapter.get_service_definition(ServiceType.PUBLISH_ON_WEB)
        assert isinstance(defn, ServiceDefinition)

    def test_publish_on_web_definition(self):
        defn = ServiceAdapter.get_service_definition(ServiceType.PUBLISH_ON_WEB)
        assert defn.binding is ServiceBinding.COMPONENT
        assert defn.name == "Publiceren op het web"

    def test_publish_on_web_exposes_public_host_and_hostname(self):
        """PUBLISH_ON_WEB must expose both PUBLIC_HOST (full URL) and PUBLIC_HOSTNAME (bare hostname)."""
        defn = ServiceAdapter.get_service_definition(ServiceType.PUBLISH_ON_WEB)
        var_names = {v.name for v in defn.variables}
        assert "PUBLIC_HOST" in var_names
        assert "PUBLIC_HOSTNAME" in var_names
        for v in defn.variables:
            if v.name in ("PUBLIC_HOST", "PUBLIC_HOSTNAME"):
                assert v.source == "direct"

    def test_postgresql_definition(self):
        defn = ServiceAdapter.get_service_definition(ServiceType.POSTGRESQL_DATABASE)
        assert defn.binding is ServiceBinding.DEPLOYMENT
        assert defn.secret_class == "DatabaseSecret"

    def test_every_service_has_definition(self):
        for service in ServiceType:
            defn = ServiceAdapter.get_service_definition(service)
            assert defn is not None
            assert defn.name
            assert defn.description
            # Drie mogelijkheden sinds RC: een dienst die aan niets bindt hoort bij het
            # project als geheel. Invite was de aanleiding; die stond op COMPONENT omdat er
            # geen andere waarde was, waarna hij in de componentkeuze verscheen.
            assert defn.binding in (
                ServiceBinding.COMPONENT,
                ServiceBinding.DEPLOYMENT,
                ServiceBinding.PROJECT,
            )


class TestGetServiceByValue:
    """Tests for ServiceAdapter.get_service_by_value."""

    def test_valid_value(self):
        assert ServiceAdapter.get_service_by_value("keycloak") == ServiceType.KEYCLOAK

    def test_all_values_roundtrip(self):
        for service in ServiceType:
            assert ServiceAdapter.get_service_by_value(service.value) == service

    def test_invalid_value_raises(self):
        with pytest.raises(ValueError, match="nonexistent"):
            ServiceAdapter.get_service_by_value("nonexistent")


class TestParseServicesFromStrings:
    """Tests for ServiceAdapter.parse_services_from_strings."""

    def test_valid_names(self):
        result = ServiceAdapter.parse_services_from_strings(["keycloak", "redis"])
        assert result == [ServiceType.KEYCLOAK, ServiceType.REDIS]

    def test_empty_list(self):
        assert ServiceAdapter.parse_services_from_strings([]) == []

    def test_unknown_name_raises_value_error(self):
        with pytest.raises(ValueError, match="Unknown service"):
            ServiceAdapter.parse_services_from_strings(["nonexistent"])

    def test_sso_rijk_gives_helpful_error(self):
        with pytest.raises(ValueError, match=r"sso-rijk.*renamed.*keycloak"):
            ServiceAdapter.parse_services_from_strings(["sso-rijk"])

    def test_non_string_raises_type_error(self):
        with pytest.raises(TypeError, match="must be a string"):
            ServiceAdapter.parse_services_from_strings([123])  # type: ignore[list-item]


class TestGetStorageServices:
    """Tests for ServiceAdapter.get_storage_services."""

    def test_filters_to_storage_only(self):
        services = [
            ServiceType.PUBLISH_ON_WEB,
            ServiceType.PERSISTENT_STORAGE,
            ServiceType.TEMP_STORAGE,
            ServiceType.POSTGRESQL_DATABASE,
        ]
        result = ServiceAdapter.get_storage_services(services)
        assert result == [ServiceType.PERSISTENT_STORAGE, ServiceType.TEMP_STORAGE]

    def test_no_storage_services(self):
        services = [ServiceType.KEYCLOAK, ServiceType.REDIS]
        assert ServiceAdapter.get_storage_services(services) == []

    def test_empty_input(self):
        assert ServiceAdapter.get_storage_services([]) == []


class TestIsComponentAndDeploymentService:
    """Tests for is_component_service and is_deployment_service."""

    @pytest.mark.parametrize(
        "service",
        [
            ServiceType.PUBLISH_ON_WEB,
            ServiceType.KEYCLOAK,
            ServiceType.PERSISTENT_STORAGE,
            ServiceType.TEMP_STORAGE,
            ServiceType.AUTHORIZATION_WALL,
        ],
    )
    def test_component_services(self, service: ServiceType):
        assert ServiceAdapter.is_component_service(service) is True
        assert ServiceAdapter.is_deployment_service(service) is False

    @pytest.mark.parametrize(
        "service",
        [
            ServiceType.POSTGRESQL_DATABASE,
            ServiceType.NAMESPACE_POSTGRESQL_DATABASE,
            ServiceType.MINIO_STORAGE,
            ServiceType.REDIS,
            ServiceType.NAMESPACE_REDIS,
        ],
    )
    def test_deployment_services(self, service: ServiceType):
        assert ServiceAdapter.is_deployment_service(service) is True
        assert ServiceAdapter.is_component_service(service) is False


class TestGetVariables:
    """Tests for ServiceAdapter.get_variables."""

    def test_postgresql_has_expected_variables(self):
        variables = ServiceAdapter.get_variables(ServiceType.POSTGRESQL_DATABASE)
        names = [v.name for v in variables]
        assert "DATABASE_SERVER_HOST" in names
        assert "DATABASE_SERVER_PORT" in names
        assert "DATABASE_PASSWORD" in names
        assert "DATABASE_DB" in names

    def test_all_variables_are_variable_definitions(self):
        for service in ServiceType:
            for var in ServiceAdapter.get_variables(service):
                assert isinstance(var, VariableDefinition)

    def test_persistent_storage_has_data_path(self):
        variables = ServiceAdapter.get_variables(ServiceType.PERSISTENT_STORAGE)
        names = [v.name for v in variables]
        assert "DATA_PATH" in names

    def test_temp_storage_has_temp_path(self):
        variables = ServiceAdapter.get_variables(ServiceType.TEMP_STORAGE)
        names = [v.name for v in variables]
        assert "TEMP_PATH" in names


class TestGetVariablesBySource:
    """Tests for ServiceAdapter.get_variables_by_source."""

    def test_postgresql_secret_variables(self):
        secret_vars = ServiceAdapter.get_variables_by_source(ServiceType.POSTGRESQL_DATABASE, "secret")
        assert len(secret_vars) > 0
        assert all(v.source == "secret" for v in secret_vars)

    def test_persistent_storage_direct_variables(self):
        direct_vars = ServiceAdapter.get_variables_by_source(ServiceType.PERSISTENT_STORAGE, "direct")
        assert len(direct_vars) > 0
        assert all(v.source == "direct" for v in direct_vars)

    def test_no_matching_source(self):
        direct_vars = ServiceAdapter.get_variables_by_source(ServiceType.POSTGRESQL_DATABASE, "direct")
        assert direct_vars == []


class TestNeedsMethods:
    """Tests for needs_database_access, needs_object_storage, needs_redis."""

    def test_needs_database_access_true(self):
        assert ServiceAdapter.needs_database_access([ServiceType.POSTGRESQL_DATABASE]) is True

    def test_needs_database_access_false(self):
        assert ServiceAdapter.needs_database_access([ServiceType.REDIS]) is False

    def test_needs_object_storage_true(self):
        assert ServiceAdapter.needs_object_storage([ServiceType.MINIO_STORAGE]) is True

    def test_needs_object_storage_false(self):
        assert ServiceAdapter.needs_object_storage([ServiceType.KEYCLOAK]) is False

    def test_needs_redis_true_for_redis(self):
        assert ServiceAdapter.needs_redis([ServiceType.REDIS]) is True

    def test_needs_redis_true_for_namespace_redis(self):
        assert ServiceAdapter.needs_redis([ServiceType.NAMESPACE_REDIS]) is True

    def test_needs_redis_false(self):
        assert ServiceAdapter.needs_redis([ServiceType.POSTGRESQL_DATABASE]) is False

    def test_empty_list(self):
        assert ServiceAdapter.needs_database_access([]) is False
        assert ServiceAdapter.needs_object_storage([]) is False
        assert ServiceAdapter.needs_redis([]) is False


class TestNeedsInfrastructureNamespace:
    """Tests for ServiceAdapter.needs_infrastructure_namespace."""

    def test_namespace_postgresql(self):
        assert ServiceAdapter.needs_infrastructure_namespace([ServiceType.NAMESPACE_POSTGRESQL_DATABASE]) is True

    def test_namespace_redis(self):
        assert ServiceAdapter.needs_infrastructure_namespace([ServiceType.NAMESPACE_REDIS]) is True

    def test_regular_postgresql(self):
        assert ServiceAdapter.needs_infrastructure_namespace([ServiceType.POSTGRESQL_DATABASE]) is False

    def test_regular_redis(self):
        assert ServiceAdapter.needs_infrastructure_namespace([ServiceType.REDIS]) is False

    def test_empty(self):
        assert ServiceAdapter.needs_infrastructure_namespace([]) is False

    def test_mixed_with_namespace_service(self):
        services = [ServiceType.KEYCLOAK, ServiceType.NAMESPACE_POSTGRESQL_DATABASE, ServiceType.REDIS]
        assert ServiceAdapter.needs_infrastructure_namespace(services) is True


class TestExtractServiceNamesFromProjectServices:
    """Tests for ServiceAdapter.extract_service_names_from_project_services."""

    def test_string_format(self):
        result = ServiceAdapter.extract_service_names_from_project_services(["keycloak", "redis"])
        assert result == ["keycloak", "redis"]

    def test_dict_format(self):
        result = ServiceAdapter.extract_service_names_from_project_services(
            [{"namespace-postgresql-database": {"config": {"size": "10Gi"}}}]
        )
        assert result == ["namespace-postgresql-database"]

    def test_mixed_formats(self):
        result = ServiceAdapter.extract_service_names_from_project_services(
            ["keycloak", {"namespace-postgresql-database": {"config": {}}}]
        )
        assert result == ["keycloak", "namespace-postgresql-database"]

    def test_empty_list(self):
        assert ServiceAdapter.extract_service_names_from_project_services([]) == []

    def test_empty_dict_raises(self):
        with pytest.raises(ValueError, match="Cannot determine service name"):
            ServiceAdapter.extract_service_names_from_project_services([{}])

    def test_ambiguous_multi_key_dict_raises(self):
        # A multi-key dict without name/reference is ambiguous (RC-5 A: the new form
        # uses an explicit name/reference key).
        with pytest.raises(ValueError, match="Cannot determine service name"):
            ServiceAdapter.extract_service_names_from_project_services([{"a": {}, "b": {}}])

    def test_new_record_format_accepted(self):
        # RC-5 A: {name/reference, config} records (multi-key dicts) resolve by the
        # name/reference value, not the sole key.
        assert ServiceAdapter.extract_service_names_from_project_services(
            [{"name": "keycloak", "config": {"template": "sso-only"}}, {"reference": "publish-on-web"}]
        ) == ["keycloak", "publish-on-web"]

    def test_invalid_type_raises(self):
        with pytest.raises(TypeError, match="Invalid service item type"):
            ServiceAdapter.extract_service_names_from_project_services([123])  # type: ignore[list-item]


class TestFilterComponentAndDeploymentServices:
    """Tests for filter_component_services and filter_deployment_services."""

    def test_filter_component_services(self):
        services = [
            ServiceType.PUBLISH_ON_WEB,
            ServiceType.POSTGRESQL_DATABASE,
            ServiceType.KEYCLOAK,
            ServiceType.REDIS,
        ]
        result = ServiceAdapter.filter_component_services(services)
        assert result == [ServiceType.PUBLISH_ON_WEB, ServiceType.KEYCLOAK]

    def test_filter_deployment_services(self):
        services = [
            ServiceType.PUBLISH_ON_WEB,
            ServiceType.POSTGRESQL_DATABASE,
            ServiceType.KEYCLOAK,
            ServiceType.REDIS,
        ]
        result = ServiceAdapter.filter_deployment_services(services)
        assert result == [ServiceType.POSTGRESQL_DATABASE, ServiceType.REDIS]

    def test_filter_empty_list(self):
        assert ServiceAdapter.filter_component_services([]) == []
        assert ServiceAdapter.filter_deployment_services([]) == []

    def test_every_service_is_bound_in_exactly_one_way(self):
        """Component, deployment of geen van beide, en nooit twee tegelijk.

        Het was eerder een tweedeling die alle diensten moest dekken. Dat klopte niet: een
        uitnodiging bindt aan niets en werd daardoor als componentdienst opgevoerd, met als
        zichtbaar gevolg dat hij in de componentkeuze stond en dat de UI meldde dat je hem
        per component kiest.
        """
        from opi.services.registry import SERVICES
        from opi.services.services_enums import ServiceBinding

        all_services = ServiceAdapter.get_all_services()
        component = set(ServiceAdapter.filter_component_services(all_services))
        deployment = set(ServiceAdapter.filter_deployment_services(all_services))
        project = {
            service for service in all_services if SERVICES[service].definition.binding is ServiceBinding.PROJECT
        }

        assert component & deployment == set(), "een dienst bindt niet aan twee dingen tegelijk"
        assert component & project == set()
        assert deployment & project == set()
        assert component | deployment | project == set(all_services), "elke dienst hoort in precies een bak"


class TestResolveServiceDependencies:
    """resolve_service_dependencies must handle the mixed services list (bare names +
    single-key config dicts like {'attachments': {'data': ...}}) without crashing on a
    set() over dicts, and preserve the dict entries."""

    def test_preserves_dict_entries_in_mixed_list(self):
        from opi.services.services import ServiceAdapter

        mixed = [
            "publish-on-web",
            {"keycloak": {"config": {"template": "sso-support"}}},
            {"attachments": {"data": [{"id": "sso", "filename": "c.pem", "content": "x"}]}},
        ]
        out = ServiceAdapter.resolve_service_dependencies(mixed)
        assert any(isinstance(e, dict) and "attachments" in e for e in out)
        assert any(isinstance(e, dict) and "keycloak" in e for e in out)
        # the attachments catalog data survives
        att = next(e for e in out if isinstance(e, dict) and "attachments" in e)
        assert att["attachments"]["data"][0]["id"] == "sso"

    def test_bare_string_list_still_works(self):
        from opi.services.services import ServiceAdapter

        assert ServiceAdapter.resolve_service_dependencies(["publish-on-web", "minio-storage"]) == [
            "publish-on-web",
            "minio-storage",
        ]
