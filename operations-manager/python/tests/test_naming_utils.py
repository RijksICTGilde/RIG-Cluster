"""
Comprehensive tests for the centralized naming utilities module.

Tests all public functions in opi.utils.naming for generating consistent
unique names for Kubernetes resources, databases, MinIO, ArgoCD, backups,
infrastructure, and URL handling.
"""

from opi.utils.naming import (
    HostnameFormat,
    ensure_fqdn,
    ensure_url_has_protocol,
    extract_domain_from_url,
    find_root_component,
    generate_argocd_application_filename,
    generate_argocd_application_name,
    generate_argocd_repository_secret_name,
    generate_backup_clone_pvc_name,
    generate_backup_pod_name,
    generate_backup_prefix,
    generate_backup_snapshot_name,
    generate_bare_domain_hostname,
    generate_bucket_name,
    generate_database_name,
    generate_database_schema,
    generate_database_username,
    generate_gitops_argocd_application_path,
    generate_gitops_manifests_folder_path,
    generate_helm_values_filename,
    generate_hostname,
    generate_infrastructure_application_name,
    generate_infrastructure_argocd_application_filename,
    generate_infrastructure_argocd_application_path,
    generate_infrastructure_argocd_appproject_filename,
    generate_infrastructure_argocd_folder_path,
    generate_infrastructure_manifest_path,
    generate_infrastructure_namespace_base,
    generate_ingress_map,
    generate_ingress_name_from_path,
    generate_issuer_name,
    generate_issuer_secret_name,
    generate_keycloak_client_id,
    generate_manifest_name,
    generate_minio_policy_name,
    generate_minio_username,
    generate_network_policy_name,
    generate_project_admin_username,
    generate_project_realm_name,
    generate_public_url,
    generate_pvc_manifest_type,
    generate_pvc_name,
    generate_redis_key_prefix,
    generate_redis_username,
    generate_registry_secret_name,
    generate_resource_identifier,
    generate_restore_pod_name,
    generate_restored_pvc_name,
    generate_storage_name,
    generate_tls_secret_name,
    generate_unique_name,
    get_component_ingress_map,
    get_deployment_hostnames,
    make_argocd_repository_url_unique,
    normalize_base_domain,
    resolve_effective_base_domain,
    sanitize_kubernetes_name,
)

_CLUSTER = "local"


def _approved(*domains: str) -> dict:
    return {"domains": {"allowed-domains": [{"domain": d, "status": "approved"} for d in domains]}}


class TestGenerateUniqueName:
    """Tests for generate_unique_name function."""

    def test_basic_combination(self):
        """Combines deployment and component with hyphen."""
        assert generate_unique_name("frontend", "webapp") == "frontend-webapp"

    def test_preserves_existing_hyphens(self):
        """Preserves hyphens already present in inputs."""
        assert generate_unique_name("my-deploy", "my-comp") == "my-deploy-my-comp"

    def test_empty_strings(self):
        """Works with empty strings."""
        assert generate_unique_name("", "") == "-"
        assert generate_unique_name("deploy", "") == "deploy-"
        assert generate_unique_name("", "comp") == "-comp"


class TestGenerateStorageName:
    """Tests for generate_storage_name function."""

    def test_basic_mount_path(self):
        """Converts basic mount path to storage name."""
        assert generate_storage_name("/data", 0) == "data"

    def test_nested_mount_path(self):
        """Removes slashes from nested paths."""
        assert generate_storage_name("/app/logs", 1) == "applogs"

    def test_empty_mount_path_uses_index(self):
        """Falls back to storage{index} for empty path."""
        assert generate_storage_name("", 0) == "storage0"
        assert generate_storage_name("", 2) == "storage2"

    def test_special_characters_removed(self):
        """Removes hyphens, underscores, and other special characters."""
        assert generate_storage_name("/my-data_dir", 0) == "mydatadir"

    def test_uppercase_lowered(self):
        """Converts uppercase to lowercase."""
        assert generate_storage_name("/MyData", 0) == "mydata"

    def test_all_invalid_chars_falls_back(self):
        """Falls back to index if all chars are invalid after processing."""
        assert generate_storage_name("/---", 3) == "storage3"


class TestGeneratePvcName:
    """Tests for generate_pvc_name function."""

    def test_basic_pvc_name(self):
        """Generates basic PVC name without generation."""
        assert generate_pvc_name("frontend-webapp", "data") == "frontend-webapp-data-pvc"

    def test_generation_none(self):
        """No suffix when generation is None."""
        assert generate_pvc_name("frontend-webapp", "data", None) == "frontend-webapp-data-pvc"

    def test_generation_zero(self):
        """No suffix when generation is 0."""
        assert generate_pvc_name("frontend-webapp", "data", 0) == "frontend-webapp-data-pvc"

    def test_generation_positive(self):
        """Adds -v{gen} suffix for positive generation."""
        assert generate_pvc_name("frontend-webapp", "data", 1) == "frontend-webapp-data-pvc-v1"
        assert generate_pvc_name("frontend-webapp", "data", 2) == "frontend-webapp-data-pvc-v2"


class TestGenerateManifestName:
    """Tests for generate_manifest_name function."""

    def test_basic_manifest_name(self):
        """Generates basic manifest name."""
        assert generate_manifest_name("webapp", "deployment") == "webapp-deployment"

    def test_pvc_manifest_name(self):
        """Generates PVC manifest name."""
        assert generate_manifest_name("webapp", "data-pvc") == "webapp-data-pvc"

    def test_generation_none(self):
        """No suffix for None generation."""
        assert generate_manifest_name("webapp", "data-pvc", None) == "webapp-data-pvc"

    def test_generation_zero(self):
        """No suffix for zero generation."""
        assert generate_manifest_name("webapp", "data-pvc", 0) == "webapp-data-pvc"

    def test_generation_positive(self):
        """Adds version suffix for positive generation."""
        assert generate_manifest_name("webapp", "data-pvc", 1) == "webapp-data-pvc-v1"
        assert generate_manifest_name("webapp", "data-pvc", 2) == "webapp-data-pvc-v2"


class TestGeneratePvcManifestType:
    """Tests for generate_pvc_manifest_type function."""

    def test_basic_type(self):
        """Appends -pvc to storage name."""
        assert generate_pvc_manifest_type("data") == "data-pvc"

    def test_complex_storage_name(self):
        """Works with multi-part storage names."""
        assert generate_pvc_manifest_type("applogs") == "applogs-pvc"


class TestSanitizeKubernetesName:
    """Tests for sanitize_kubernetes_name function."""

    def test_empty_string_returns_unnamed(self):
        """Empty string returns 'unnamed'."""
        assert sanitize_kubernetes_name("") == "unnamed"

    def test_valid_name_unchanged(self):
        """Valid name passes through unchanged."""
        assert sanitize_kubernetes_name("my-resource") == "my-resource"

    def test_uppercase_lowered(self):
        """Converts uppercase to lowercase."""
        assert sanitize_kubernetes_name("MyResource") == "myresource"

    def test_special_chars_replaced(self):
        """Replaces special characters with hyphens."""
        assert sanitize_kubernetes_name("my_resource.v1") == "my-resource-v1"

    def test_consecutive_hyphens_collapsed(self):
        """Collapses consecutive hyphens."""
        assert sanitize_kubernetes_name("my---resource") == "my-resource"

    def test_leading_trailing_hyphens_removed(self):
        """Removes leading and trailing hyphens."""
        assert sanitize_kubernetes_name("-my-resource-") == "my-resource"

    def test_truncation(self):
        """Truncates names exceeding max_length."""
        long_name = "a" * 100
        result = sanitize_kubernetes_name(long_name)
        assert len(result) <= 63

    def test_custom_max_length(self):
        """Respects custom max_length."""
        result = sanitize_kubernetes_name("abcdefghij", max_length=5)
        assert len(result) <= 5
        assert result == "abcde"

    def test_truncation_removes_trailing_hyphen(self):
        """Trailing hyphen after truncation is removed."""
        # name that after truncation would end with hyphen
        result = sanitize_kubernetes_name("abcd-efgh", max_length=5)
        assert result == "abcd"

    def test_all_invalid_chars_returns_unnamed(self):
        """All-invalid input returns 'unnamed'."""
        assert sanitize_kubernetes_name("___") == "unnamed"


class TestGenerateHostname:
    """Tests for generate_hostname function."""

    def test_basic_hostname(self):
        """Generates hostname from parts."""
        result = generate_hostname("webapp", "frontend", "myproject", ".dev.example.com")
        assert result == "webapp-frontend-myproject.dev.example.com"

    def test_different_postfix(self):
        """Works with different ingress postfixes."""
        result = generate_hostname("api", "main", "proj", ".kind")
        assert result == "api-main-proj.kind"


class TestGenerateIngressMap:
    """Tests for generate_ingress_map function."""

    def test_component_specific_no_subdomain(self):
        """Component-specific mode when no subdomain provided."""
        result = generate_ingress_map("webapp", "main", "myproject", ".dev.example.com", None)
        assert result == {"main-webapp": "webapp-main-myproject.dev.example.com"}

    def test_deployment_name_subdomain(self):
        """Deployment-name mode when subdomain matches deployment name."""
        result = generate_ingress_map("webapp", "main", "myproject", ".dev.example.com", "main")
        assert result == {"main-webapp": "main-myproject.dev.example.com"}

    def test_custom_subdomain(self):
        """Custom subdomain mode when subdomain differs from deployment name."""
        result = generate_ingress_map("webapp", "main", "myproject", ".dev.example.com", "myapp")
        assert result == {"main-webapp": "myapp.dev.example.com"}

    def test_subdomain_strips_leading_dot(self):
        """Strips leading dot from ingress postfix in subdomain mode."""
        result = generate_ingress_map("webapp", "main", "myproject", ".example.com", "myapp")
        assert result["main-webapp"] == "myapp.example.com"


class TestGenerateResourceIdentifier:
    """Tests for generate_resource_identifier function."""

    def test_underscore_separator(self):
        """Default separator is underscore."""
        assert generate_resource_identifier("myproject", "frontend") == "myproject_frontend"

    def test_hyphen_separator(self):
        """Supports hyphen separator."""
        assert generate_resource_identifier("myproject", "frontend", "-") == "myproject-frontend"

    def test_underscore_normalizes_hyphens(self):
        """Underscore separator replaces hyphens with underscores."""
        result = generate_resource_identifier("my-project", "front-end", "_")
        assert result == "my_project_front_end"

    def test_hyphen_normalizes_underscores(self):
        """Hyphen separator replaces underscores with hyphens."""
        result = generate_resource_identifier("my_project", "front_end", "-")
        assert result == "my-project-front-end"

    def test_lowercased(self):
        """Inputs are lowercased."""
        assert generate_resource_identifier("MyProject", "FrontEnd") == "myproject_frontend"

    def test_truncation(self):
        """Truncates to max_length."""
        result = generate_resource_identifier("a" * 40, "b" * 40, max_length=63)
        assert len(result) <= 63


class TestGenerateDatabaseUsername:
    """Tests for generate_database_username function."""

    def test_basic_username(self):
        """Generates project_deployment username."""
        assert generate_database_username("myproject", "prod") == "myproject_prod"

    def test_hyphens_replaced(self):
        """Hyphens are replaced with underscores."""
        assert generate_database_username("my-project", "my-deploy") == "my_project_my_deploy"

    def test_uppercase_lowered(self):
        """Uppercase is lowered."""
        assert generate_database_username("MyProject", "Prod") == "myproject_prod"


class TestGenerateDatabaseSchema:
    """Tests for generate_database_schema function."""

    def test_basic_schema(self):
        """Generates project_deployment schema."""
        assert generate_database_schema("myproject", "prod") == "myproject_prod"

    def test_hyphens_replaced(self):
        """Hyphens become underscores."""
        assert generate_database_schema("my-project", "my-deploy") == "my_project_my_deploy"


class TestGenerateDatabaseName:
    """Tests for generate_database_name function."""

    def test_basic_name(self):
        """Generates basic database name."""
        assert generate_database_name("amt", "prod") == "amt_prod"

    def test_generation_none(self):
        """No suffix for None generation."""
        assert generate_database_name("amt", "prod", None) == "amt_prod"

    def test_generation_zero(self):
        """No suffix for zero generation."""
        assert generate_database_name("amt", "prod", 0) == "amt_prod"

    def test_generation_positive(self):
        """Adds _v{gen} suffix for positive generation."""
        assert generate_database_name("amt", "prod", 1) == "amt_prod_v1"
        assert generate_database_name("amt", "prod", 2) == "amt_prod_v2"

    def test_hyphens_replaced(self):
        """Hyphens replaced with underscores."""
        assert generate_database_name("my-project", "my-deploy") == "my_project_my_deploy"


class TestGenerateMinioUsername:
    """Tests for generate_minio_username function."""

    def test_basic_username(self):
        """Generates project_deployment username."""
        assert generate_minio_username("myproject", "prod") == "myproject_prod"

    def test_hyphens_replaced(self):
        """Hyphens become underscores."""
        assert generate_minio_username("my-project", "my-deploy") == "my_project_my_deploy"


class TestGenerateBucketName:
    """Tests for generate_bucket_name function."""

    def test_basic_bucket(self):
        """Generates hyphen-separated bucket name."""
        assert generate_bucket_name("amt", "prod") == "amt-prod"

    def test_generation_none(self):
        """No suffix for None generation."""
        assert generate_bucket_name("amt", "prod", None) == "amt-prod"

    def test_generation_zero(self):
        """No suffix for zero generation."""
        assert generate_bucket_name("amt", "prod", 0) == "amt-prod"

    def test_generation_positive(self):
        """Adds -v{gen} suffix for positive generation."""
        assert generate_bucket_name("amt", "prod", 1) == "amt-prod-v1"
        assert generate_bucket_name("amt", "prod", 2) == "amt-prod-v2"

    def test_lowercase(self):
        """Bucket name is lowercased."""
        assert generate_bucket_name("MyProject", "Prod") == "myproject-prod"


class TestGenerateMinioPolicyName:
    """Tests for generate_minio_policy_name function."""

    def test_basic_policy_name(self):
        """Generates username-bucket-policy name."""
        result = generate_minio_policy_name("amt", "prod")
        assert result == "amt_prod-amt-prod-policy"

    def test_with_hyphens(self):
        """Hyphens in inputs are handled correctly."""
        result = generate_minio_policy_name("amt-136", "deployment-1")
        assert result == "amt_136_deployment_1-amt-136-deployment-1-policy"


class TestGenerateKeycloakClientId:
    """Tests for generate_keycloak_client_id function."""

    def test_without_component(self):
        """Generates project-deployment client ID."""
        assert generate_keycloak_client_id("myproject", "prod") == "myproject-prod"

    def test_with_component(self):
        """Generates project-deployment-component client ID."""
        assert generate_keycloak_client_id("myproject", "prod", "webapp") == "myproject-prod-webapp"

    def test_lowercase(self):
        """Client ID is lowercased."""
        assert generate_keycloak_client_id("MyProject", "Prod") == "myproject-prod"


class TestGenerateArgocdApplicationName:
    """Tests for generate_argocd_application_name function."""

    def test_basic_name(self):
        """Generates project-deployment application name."""
        assert generate_argocd_application_name("myproject", "prod") == "myproject-prod"

    def test_lowercase(self):
        """Application name is lowercased."""
        assert generate_argocd_application_name("MyProject", "Prod") == "myproject-prod"


class TestGenerateArgocdApplicationFilename:
    """Tests for generate_argocd_application_filename function."""

    def test_basic_filename(self):
        """Generates correct YAML filename."""
        result = generate_argocd_application_filename("myproject", "prod")
        assert result == "myproject-prod-argocd-application.yaml"

    def test_lowercase(self):
        """Filename is lowercased."""
        result = generate_argocd_application_filename("MyProject", "Prod")
        assert result == "myproject-prod-argocd-application.yaml"


class TestGenerateGitopsManifestsFolderPath:
    """Tests for generate_gitops_manifests_folder_path function."""

    def test_basic_path(self):
        """Generates cluster/project/deployment path."""
        result = generate_gitops_manifests_folder_path("production", "myproject", "prod")
        assert result == "production/myproject/prod"

    def test_lowercase(self):
        """Path segments are lowercased."""
        result = generate_gitops_manifests_folder_path("Production", "MyProject", "Prod")
        assert result == "production/myproject/prod"


class TestGenerateGitopsArgocdApplicationPath:
    """Tests for generate_gitops_argocd_application_path function."""

    def test_basic_path(self):
        """Generates full path to ArgoCD application file."""
        result = generate_gitops_argocd_application_path("production", "myproject", "prod")
        assert result == "production/myproject/myproject-prod-argocd-application.yaml"


class TestGeneratePublicUrl:
    """Tests for generate_public_url function."""

    def test_https_default(self):
        """Generates HTTPS URL by default."""
        result = generate_public_url("webapp.dev.example.com")
        assert result == "https://webapp.dev.example.com"

    def test_http_explicit(self):
        """Generates HTTP URL when requested."""
        result = generate_public_url("webapp.dev.example.com", use_https=False)
        assert result == "http://webapp.dev.example.com"

    def test_appends_match_path(self):
        """Appends a non-root ingress match path to the URL."""
        result = generate_public_url("webapp.dev.example.com", path="/api")
        assert result == "https://webapp.dev.example.com/api"

    def test_root_path_omitted(self):
        """Root path is omitted to keep the URL clean."""
        assert generate_public_url("webapp.dev.example.com", path="/") == "https://webapp.dev.example.com"


class TestEnsureUrlHasProtocol:
    """Tests for ensure_url_has_protocol function."""

    def test_adds_https(self):
        """Adds https:// to bare hostname."""
        assert ensure_url_has_protocol("example.com") == "https://example.com"

    def test_adds_http(self):
        """Adds http:// when use_https is False."""
        assert ensure_url_has_protocol("example.com", use_https=False) == "http://example.com"

    def test_preserves_existing_https(self):
        """Preserves existing https://."""
        assert ensure_url_has_protocol("https://example.com") == "https://example.com"

    def test_preserves_existing_http(self):
        """Preserves existing http://."""
        assert ensure_url_has_protocol("http://example.com") == "http://example.com"


class TestExtractDomainFromUrl:
    """Tests for extract_domain_from_url function."""

    def test_strips_https(self):
        """Strips https:// prefix."""
        assert extract_domain_from_url("https://example.com") == "example.com"

    def test_strips_http(self):
        """Strips http:// prefix."""
        assert extract_domain_from_url("http://example.com") == "example.com"

    def test_preserves_port(self):
        """Preserves port number."""
        assert extract_domain_from_url("http://example.com:8080/path") == "example.com:8080"

    def test_removes_path(self):
        """Removes path component."""
        assert extract_domain_from_url("https://example.com/some/path") == "example.com"

    def test_bare_domain(self):
        """Returns bare domain unchanged."""
        assert extract_domain_from_url("example.com") == "example.com"

    def test_strips_query_string(self):
        """Query parameters should be stripped, not included in domain."""
        assert extract_domain_from_url("http://example.com?foo=bar") == "example.com"

    def test_strips_fragment(self):
        """Fragment identifiers should be stripped from domain."""
        assert extract_domain_from_url("https://example.com#section") == "example.com"


class TestMakeArgocdRepositoryUrlUnique:
    """Tests for make_argocd_repository_url_unique function."""

    def test_https_github_modified(self):
        """HTTPS GitHub URL gets project name as username."""
        result = make_argocd_repository_url_unique("https://github.com/org/repo.git", "project-a")
        assert result == "https://project-a@github.com/org/repo.git"

    def test_https_gitlab_modified(self):
        """HTTPS GitLab URL gets project name as username."""
        result = make_argocd_repository_url_unique("https://gitlab.com/org/repo.git", "project-a")
        assert result == "https://project-a@gitlab.com/org/repo.git"

    def test_https_bitbucket_modified(self):
        """HTTPS Bitbucket URL gets project name as username."""
        result = make_argocd_repository_url_unique("https://bitbucket.org/org/repo.git", "project-a")
        assert result == "https://project-a@bitbucket.org/org/repo.git"

    def test_ssh_url_unchanged(self):
        """SSH URL is returned unchanged."""
        url = "ssh://git@github.com/org/repo.git"
        assert make_argocd_repository_url_unique(url, "project-c") == url

    def test_unknown_host_unchanged(self):
        """Unknown host URL is returned unchanged."""
        url = "https://custom-git.example.com/org/repo.git"
        assert make_argocd_repository_url_unique(url, "project-d") == url

    def test_existing_username_replaced(self):
        """Existing username in URL is replaced."""
        result = make_argocd_repository_url_unique("https://existing@github.com/org/repo.git", "project-b")
        assert result == "https://project-b@github.com/org/repo.git"

    def test_http_url_unchanged(self):
        """Non-https (http) URL is unchanged."""
        url = "http://github.com/org/repo.git"
        assert make_argocd_repository_url_unique(url, "project-e") == url


class TestGenerateProjectAdminUsername:
    """Tests for generate_project_admin_username function."""

    def test_basic_username(self):
        """Generates project_cluster_admin username."""
        result = generate_project_admin_username("myproject", "production")
        assert result == "myproject_production_admin"

    def test_hyphens_replaced(self):
        """Hyphens become underscores."""
        result = generate_project_admin_username("my-project", "my-cluster")
        assert result == "my_project_my_cluster_admin"


class TestGenerateProjectRealmName:
    """Tests for generate_project_realm_name function."""

    def test_basic_realm(self):
        """Generates project-cluster realm name."""
        result = generate_project_realm_name("myproject", "production")
        assert result == "myproject-production"

    def test_lowercase(self):
        """Realm name is lowercased."""
        result = generate_project_realm_name("MyProject", "Production")
        assert result == "myproject-production"


class TestGenerateIngressNameFromPath:
    """Tests for generate_ingress_name_from_path function."""

    def test_root_path(self):
        """Root path returns base name only."""
        assert generate_ingress_name_from_path("main-api", "/") == "main-api"

    def test_empty_path(self):
        """Empty path returns base name only."""
        assert generate_ingress_name_from_path("main-api", "") == "main-api"

    def test_simple_path(self):
        """Simple path appended to base name."""
        assert generate_ingress_name_from_path("main-api", "/api") == "main-api-api"

    def test_nested_path(self):
        """Nested path slashes removed."""
        assert generate_ingress_name_from_path("main-api", "/v1/users") == "main-api-v1users"

    def test_hyphen_in_path_removed(self):
        """Hyphens in path segment are removed (non-alphanumeric)."""
        assert generate_ingress_name_from_path("main-api", "/health-check") == "main-api-healthcheck"

    def test_max_length_respected(self):
        """Respects max_length parameter."""
        result = generate_ingress_name_from_path("main-api", "/very-long-path-name", max_length=15)
        assert len(result) <= 15


class TestGenerateHelmValuesFilename:
    """Tests for generate_helm_values_filename function."""

    def test_encrypted_default(self):
        """Default is encrypted .sops.yaml."""
        result = generate_helm_values_filename("local-deployment", "docs")
        assert result == "local-deployment-docs-helm-values.sops.yaml"

    def test_decrypted(self):
        """Decrypted uses .yaml extension."""
        result = generate_helm_values_filename("local-deployment", "docs", encrypted=False)
        assert result == "local-deployment-docs-helm-values.yaml"

    def test_lowercase(self):
        """Filename is lowercased."""
        result = generate_helm_values_filename("MyDeploy", "MyChart")
        assert result == "mydeploy-mychart-helm-values.sops.yaml"


class TestFindRootComponent:
    """Tests for find_root_component function."""

    def test_finds_root_from_deployment(self):
        """Reads root-component from deployment dict."""
        deployment = {"name": "prod", "root-component": "frontend"}
        assert find_root_component(deployment) == "frontend"

    def test_no_root_returns_none(self):
        """Returns None when no root-component set."""
        deployment = {"name": "prod"}
        assert find_root_component(deployment) is None

    def test_empty_dict_returns_none(self):
        """Returns None for empty deployment dict."""
        assert find_root_component({}) is None


class TestGetComponentIngressMap:
    """Tests for get_component_ingress_map function."""

    def test_dots_format(self):
        """DOTS format generates component.subdomain.base_domain."""
        result = get_component_ingress_map(
            "frontend",
            "prod",
            "myapp",
            ".kind",
            subdomain="myapp",
            base_domain="rijks.app",
            hostname_format=HostnameFormat.DOTS,
            project_data=_approved("rijks.app"),
            cluster="local",
        )
        assert result == {"prod-frontend": "frontend.myapp.rijks.app"}

    def test_custom_domain_dashes(self):
        """DASHES with base_domain + subdomain generates subdomain.base_domain."""
        result = get_component_ingress_map(
            "frontend",
            "prod",
            "myapp",
            ".kind",
            subdomain="myapp",
            base_domain="custom.nl",
            project_data=_approved("custom.nl"),
            cluster="local",
        )
        assert result == {"prod-frontend": "myapp.custom.nl"}

    def test_cluster_domain_default(self):
        """Default cluster domain uses standard hostname generation."""
        result = get_component_ingress_map(
            "frontend",
            "prod",
            "myapp",
            ".kind",
            project_data={},
            cluster="local",
        )
        assert result == {"prod-frontend": "frontend-prod-myapp.kind"}


class TestGetDeploymentHostnames:
    """Tests for get_deployment_hostnames function."""

    def test_dots_format_includes_root(self):
        """DOTS format includes root hostname."""
        result = get_deployment_hostnames(
            ["frontend", "backend"],
            "prod",
            "myapp",
            ".kind",
            subdomain="myapp",
            base_domain="rijks.app",
            hostname_format=HostnameFormat.DOTS,
            project_data=_approved("rijks.app"),
            cluster=_CLUSTER,
        )
        assert "frontend.myapp.rijks.app" in result
        assert "backend.myapp.rijks.app" in result
        assert "myapp.rijks.app" in result
        assert len(result) == 3

    def test_dashes_format_no_root(self):
        """DASHES format does not add root hostname."""
        result = get_deployment_hostnames(
            ["frontend", "backend"],
            "prod",
            "myapp",
            ".kind",
            project_data={},
            cluster=_CLUSTER,
        )
        assert len(result) == 2

    def test_deduplicates_hostnames(self):
        """Duplicate hostnames are deduplicated (e.g. shared subdomain mode)."""
        result = get_deployment_hostnames(
            ["frontend", "backend"],
            "prod",
            "myapp",
            ".dev.example.com",
            subdomain="myapp",
            project_data={},
            cluster=_CLUSTER,
        )
        # In subdomain mode with subdomain != deployment_name, both resolve to same hostname
        assert "myapp.dev.example.com" in result


class TestHostnameFormat:
    """Tests for HostnameFormat enum."""

    def test_from_domain_mode_nice_url(self):
        """'nice-url' maps to DOTS."""
        assert HostnameFormat.from_domain_mode("nice-url") == HostnameFormat.DOTS

    def test_from_domain_mode_other(self):
        """Any other string maps to DASHES."""
        assert HostnameFormat.from_domain_mode("something") == HostnameFormat.DASHES

    def test_from_domain_mode_none(self):
        """None maps to DASHES."""
        assert HostnameFormat.from_domain_mode(None) == HostnameFormat.DASHES

    def test_from_domain_mode_empty(self):
        """Empty string maps to DASHES."""
        assert HostnameFormat.from_domain_mode("") == HostnameFormat.DASHES


class TestBackupNaming:
    """Tests for backup-related naming functions."""

    def test_generate_backup_prefix(self):
        """Generates cluster/namespace prefix."""
        assert generate_backup_prefix("local", "example-project") == "local/example-project"

    def test_generate_backup_prefix_uppercase(self):
        """Backup prefix is lowercased."""
        assert generate_backup_prefix("PRODUCTION", "My-Project") == "production/my-project"

    def test_generate_backup_snapshot_name(self):
        """Generates snapshot name."""
        result = generate_backup_snapshot_name("app-data", "20250112-143022")
        assert result == "app-data-backup-20250112-143022"

    def test_generate_backup_clone_pvc_name(self):
        """Generates clone PVC name."""
        result = generate_backup_clone_pvc_name("app-data", "20250112-143022")
        assert result == "app-data-backup-clone-20250112-143022"

    def test_generate_backup_pod_name(self):
        """Generates backup pod name."""
        result = generate_backup_pod_name("app-data", "20250112-143022")
        assert result == "backup-app-data-20250112-143022"

    def test_generate_restore_pod_name(self):
        """Generates restore pod name."""
        result = generate_restore_pod_name("app-data", "20250112-143022")
        assert result == "restore-app-data-20250112-143022"

    def test_generate_restored_pvc_name(self):
        """Generates restored PVC name."""
        result = generate_restored_pvc_name("app-data", "20250112-143022")
        assert result == "app-data-restored-20250112-143022"


class TestInfrastructureNaming:
    """Tests for infrastructure-related naming functions."""

    def test_generate_infrastructure_namespace_base(self):
        """Generates project-infrastructure namespace."""
        assert generate_infrastructure_namespace_base("myproject") == "myproject-infrastructure"

    def test_generate_infrastructure_namespace_base_uppercase(self):
        """Lowercased."""
        assert generate_infrastructure_namespace_base("MyProject") == "myproject-infrastructure"

    def test_generate_infrastructure_application_name(self):
        """Generates project-infrastructure application name."""
        assert generate_infrastructure_application_name("myproject") == "myproject-infrastructure"

    def test_generate_infrastructure_manifest_path_no_repo(self):
        """Path without repo_path."""
        result = generate_infrastructure_manifest_path("production", "myproject")
        assert result == "production/myproject/infrastructure"

    def test_generate_infrastructure_manifest_path_with_repo(self):
        """Path with repo_path."""
        result = generate_infrastructure_manifest_path("production", "myproject", "deployments")
        assert result == "deployments/production/myproject/infrastructure"

    def test_generate_infrastructure_argocd_application_filename(self):
        """Generates ArgoCD application filename."""
        result = generate_infrastructure_argocd_application_filename("myproject")
        assert result == "myproject-infrastructure-argocd-application.yaml"

    def test_generate_infrastructure_argocd_appproject_filename(self):
        """Generates ArgoCD AppProject filename."""
        result = generate_infrastructure_argocd_appproject_filename("myproject")
        assert result == "myproject-infrastructure-argocd-appproject.yaml"

    def test_generate_infrastructure_argocd_folder_path(self):
        """Generates folder path for infrastructure ArgoCD resources."""
        result = generate_infrastructure_argocd_folder_path("production", "myproject")
        assert result == "production/myproject-infrastructure"

    def test_generate_infrastructure_argocd_application_path(self):
        """Generates full path to infrastructure ArgoCD application file."""
        result = generate_infrastructure_argocd_application_path("production", "myproject")
        assert result == "production/myproject-infrastructure/myproject-infrastructure-argocd-application.yaml"

    def test_infrastructure_repo_secret_name_distinct_from_project(self):
        """The infrastructure repo secret must be scoped to the infra app so it does not
        collide with the parent project's repo secret (same URL) when both subfolders are
        rendered into the single user-applications ArgoCD app (RepeatedResourceWarning).
        Mirrors how argo_manager derives the two names."""
        project_secret = generate_argocd_repository_secret_name("myproject", "main-repo")
        infra_secret = generate_argocd_repository_secret_name(
            generate_infrastructure_application_name("myproject"), "main-repo"
        )
        assert project_secret == "myproject-main-repo"
        assert infra_secret == "myproject-infrastructure-main-repo"
        assert project_secret != infra_secret


class TestNormalizeBaseDomain:
    """Tests for normalize_base_domain function."""

    def test_basic_domain(self):
        """Dots replaced with hyphens."""
        assert normalize_base_domain("rijksapp.com") == "rijksapp-com"

    def test_subdomain_domain(self):
        """Multiple dots replaced."""
        assert normalize_base_domain("my.subdomain.example.org") == "my-subdomain-example-org"

    def test_empty_string(self):
        """Empty string returns empty string."""
        assert normalize_base_domain("") == ""

    def test_uppercase_lowered(self):
        """Uppercased input is lowered."""
        assert normalize_base_domain("RIJKSAPP.COM") == "rijksapp-com"

    def test_truncation(self):
        """Truncates to max_length."""
        long_domain = "a" * 60 + ".com"
        result = normalize_base_domain(long_domain, max_length=50)
        assert len(result) <= 50

    def test_all_invalid_chars_returns_empty_not_crash(self):
        """Domain with only invalid chars (resolved to hyphens then stripped) should not return empty string."""
        result = normalize_base_domain("...")
        assert result != "", "normalize_base_domain should not return empty for non-empty input"


class TestGenerateIssuerName:
    """Tests for generate_issuer_name function."""

    def test_default_letsencrypt(self):
        """Default issuer type is letsencrypt."""
        assert generate_issuer_name("rijksapp.com") == "letsencrypt-rijksapp-com"

    def test_staging_issuer(self):
        """Staging issuer type works."""
        result = generate_issuer_name("rijksapp.com", "letsencrypt-staging")
        assert result == "letsencrypt-staging-rijksapp-com"

    def test_deployment_scoped(self):
        """Deployment name is appended to scope the issuer per deployment."""
        result = generate_issuer_name("rijksapp.com", deployment_name="staging2")
        assert result == "letsencrypt-rijksapp-com-staging2"

    def test_staging_deployment_scoped(self):
        """Staging issuer with deployment name."""
        result = generate_issuer_name("rijksapp.com", "letsencrypt-staging", "pr-164")
        assert result == "letsencrypt-staging-rijksapp-com-pr-164"


class TestGenerateIssuerSecretName:
    """Tests for generate_issuer_secret_name function."""

    def test_default_secret(self):
        """Appends -key to issuer name."""
        assert generate_issuer_secret_name("rijksapp.com") == "letsencrypt-rijksapp-com-key"

    def test_staging_secret(self):
        """Staging variant."""
        result = generate_issuer_secret_name("rijksapp.com", "letsencrypt-staging")
        assert result == "letsencrypt-staging-rijksapp-com-key"

    def test_deployment_scoped_secret(self):
        """Deployment-scoped secret name."""
        result = generate_issuer_secret_name("rijksapp.com", deployment_name="staging2")
        assert result == "letsencrypt-rijksapp-com-staging2-key"


class TestGenerateNetworkPolicyName:
    """Tests for generate_network_policy_name function."""

    def test_basic_policy(self):
        """Appends -network-policy to purpose."""
        assert generate_network_policy_name("acme-http") == "acme-http-network-policy"

    def test_deployment_scoped_policy(self):
        """Deployment name is inserted before -network-policy suffix."""
        assert generate_network_policy_name("acme-http", "staging2") == "acme-http-staging2-network-policy"


class TestResolveEffectiveBaseDomain:
    """Tests for resolve_effective_base_domain function."""

    def test_explicit_base_domain(self):
        """Returns explicit base_domain when provided."""
        assert resolve_effective_base_domain("rijksapp.com", ".kind") == "rijksapp.com"

    def test_none_falls_back_to_postfix(self):
        """Falls back to ingress_postfix when None."""
        assert resolve_effective_base_domain(None, ".kind") == "kind"

    def test_empty_falls_back_to_postfix(self):
        """Falls back to ingress_postfix when empty string."""
        assert resolve_effective_base_domain("", ".local") == "local"

    def test_strips_leading_dot(self):
        """Strips leading dot from postfix fallback."""
        assert resolve_effective_base_domain(None, ".my.domain.com") == "my.domain.com"


class TestGenerateRegistrySecretName:
    """Tests for generate_registry_secret_name function."""

    def test_basic_secret_name(self):
        """Generates deployment-registry-secret name."""
        assert generate_registry_secret_name("frontend", "github-registry") == "frontend-github-registry-secret"

    def test_lowercase(self):
        """Registry name is lowercased."""
        assert generate_registry_secret_name("frontend", "GitHub-Registry") == "frontend-github-registry-secret"


class TestGenerateTlsSecretName:
    """Tests for generate_tls_secret_name function."""

    def test_basic_tls_name(self):
        """Appends -tls to ingress name."""
        assert generate_tls_secret_name("main-webapp") == "main-webapp-tls"

    def test_complex_ingress_name(self):
        """Works with complex ingress names."""
        assert generate_tls_secret_name("frontend-api-v1users") == "frontend-api-v1users-tls"


class TestGenerateBareDomainHostname:
    """Tests for generate_bare_domain_hostname function."""

    def test_basic_domain(self):
        """Returns the base domain as-is (lowercased)."""
        assert generate_bare_domain_hostname("voorbeeld.nl") == "voorbeeld.nl"

    def test_uppercase_lowered(self):
        """Uppercase input is lowercased."""
        assert generate_bare_domain_hostname("VOORBEELD.NL") == "voorbeeld.nl"

    def test_mixed_case(self):
        """Mixed case is lowercased."""
        assert generate_bare_domain_hostname("Mijn-App.Example.Com") == "mijn-app.example.com"


class TestGetDeploymentHostnamesBareDomain:
    """Tests for get_deployment_hostnames with expose_on_bare_domain."""

    def test_bare_domain_included_when_set(self):
        """Bare domain hostname is appended when expose_on_bare_domain is set."""
        hostnames = get_deployment_hostnames(
            component_names=["frontend"],
            deployment_name="prod",
            project_name="myapp",
            ingress_postfix=".kind",
            subdomain="www",
            base_domain="voorbeeld.nl",
            domain_format="subdomain",
            expose_on_bare_domain="frontend",
            project_data=_approved("voorbeeld.nl"),
            cluster=_CLUSTER,
        )
        assert "voorbeeld.nl" in hostnames
        assert "www.voorbeeld.nl" in hostnames

    def test_bare_domain_not_included_when_false(self):
        """Bare domain hostname is not added when expose_on_bare_domain is False."""
        hostnames = get_deployment_hostnames(
            component_names=["frontend"],
            deployment_name="prod",
            project_name="myapp",
            ingress_postfix=".kind",
            subdomain="www",
            base_domain="voorbeeld.nl",
            domain_format="subdomain",
            expose_on_bare_domain=False,
            project_data=_approved("voorbeeld.nl"),
            cluster=_CLUSTER,
        )
        assert "voorbeeld.nl" not in hostnames

    def test_bare_domain_not_included_when_empty(self):
        """Bare domain hostname is not added when expose_on_bare_domain is empty string."""
        hostnames = get_deployment_hostnames(
            component_names=["frontend"],
            deployment_name="prod",
            project_name="myapp",
            ingress_postfix=".kind",
            subdomain="www",
            base_domain="voorbeeld.nl",
            domain_format="subdomain",
            expose_on_bare_domain="",
            project_data={},
            cluster=_CLUSTER,
        )
        assert "voorbeeld.nl" not in hostnames

    def test_bare_domain_not_duplicated(self):
        """Bare domain appears only once even if it matches a component hostname."""
        hostnames = get_deployment_hostnames(
            component_names=["frontend"],
            deployment_name="prod",
            project_name="myapp",
            ingress_postfix=".kind",
            base_domain="voorbeeld.nl",
            domain_format="subdomain",
            subdomain="www",
            expose_on_bare_domain="frontend",
            project_data={},
            cluster=_CLUSTER,
        )
        assert hostnames.count("voorbeeld.nl") == 1


class TestEnsureFqdn:
    """Tests for ensure_fqdn — used to qualify service names for cross-namespace pods."""

    def test_short_name_gets_qualified(self):
        result = ensure_fqdn("rig-db-rw")
        assert result == "rig-db-rw.rig-system.svc.cluster.local"

    def test_already_qualified_unchanged(self):
        result = ensure_fqdn("rig-db-rw.rig-system.svc.cluster.local")
        assert result == "rig-db-rw.rig-system.svc.cluster.local"

    def test_short_name_with_port(self):
        result = ensure_fqdn("minio:9000")
        assert result == "minio.rig-system.svc.cluster.local:9000"

    def test_qualified_with_port_unchanged(self):
        result = ensure_fqdn("minio.rig-system.svc.cluster.local:9000")
        assert result == "minio.rig-system.svc.cluster.local:9000"

    def test_partial_domain_unchanged(self):
        """A hostname with a dot is already qualified enough."""
        result = ensure_fqdn("db.other-namespace.svc")
        assert result == "db.other-namespace.svc"


class TestRedisNaming:
    """Redis ACL username + key prefix conventions."""

    def test_key_prefix_has_no_trailing_separator(self):
        """The exported prefix is a bare token: no trailing ':' (which YAML-parses
        as a hash) and no trailing '*'. The ':' delimiter belongs to the ACL
        pattern, added in RedisManager, not to the identity token."""
        prefix = generate_redis_key_prefix("myproject", "main")
        assert prefix == "main-myproject"
        assert not prefix.endswith(":")
        assert not prefix.endswith("*")

    def test_key_prefix_equals_username(self):
        """Prefix token and ACL username are the same identity; the only former
        difference was the trailing colon, now removed."""
        assert generate_redis_key_prefix("myproject", "main") == generate_redis_username("myproject", "main")

    def test_key_prefix_sanitized_and_lowercased(self):
        prefix = generate_redis_key_prefix("My_Project", "Prod")
        assert prefix == "prod-my_project"
        assert not prefix.endswith(":")


class TestGenerateMailSenderAddress:
    """Het afzenderadres van een project: ``<local>+<project>@<domein>``.

    De ENE plek waar dit adres wordt samengesteld. De relay stelt hem niet nog eens samen
    maar leest hem terug uit een opzoektabel die OPI vult, precies omdat een tweede
    samensteller het bij de eerste afkapping oneens zou zijn met deze.
    """

    BASIS = "noreply-rijksapp@rijksoverheid.nl"

    def test_het_project_staat_in_het_plusdeel(self):
        from opi.utils.naming import generate_mail_sender_address

        assert generate_mail_sender_address(self.BASIS, "algor-odc") == "noreply-rijksapp+algor-odc@rijksoverheid.nl"

    def test_hoofdletters_worden_kleine_letters(self):
        """Een adres is geen plek voor de schrijfwijze van een projectnaam."""
        from opi.utils.naming import generate_mail_sender_address

        assert generate_mail_sender_address(self.BASIS, "Algor-ODC").startswith("noreply-rijksapp+algor-odc@")

    def test_een_te_lange_projectnaam_wordt_afgekapt(self):
        """De valkuil die niemand narekent: het voorvoegsel is zeventien tekens en een
        lokaal deel mag er vierenzestig, dus vanaf achtenveertig tekens projectnaam loopt
        het adres eroverheen en weigert de upstream het."""
        from opi.utils.naming import MAIL_LOCAL_PART_MAX_LENGTH, generate_mail_sender_address

        adres = generate_mail_sender_address(self.BASIS, "p" * 80)
        lokaal, _, domein = adres.partition("@")
        assert len(lokaal) == MAIL_LOCAL_PART_MAX_LENGTH
        assert domein == "rijksoverheid.nl"

    def test_precies_op_de_grens_blijft_heel(self):
        """De keerzijde: afkappen mag alleen als het moet."""
        from opi.utils.naming import generate_mail_sender_address

        naam = "p" * 47
        assert generate_mail_sender_address(self.BASIS, naam) == f"noreply-rijksapp+{naam}@rijksoverheid.nl"

    def test_het_domein_komt_uit_het_basisadres(self):
        """Nooit een ander domein: SPF-uitlijning is het enige dat een bericht door DMARC
        krijgt, en die bestaat alleen zolang envelope en From: hetzelfde domein dragen."""
        from opi.utils.naming import generate_mail_sender_address

        assert generate_mail_sender_address("post@voorbeeld.example", "x").endswith("@voorbeeld.example")
