"""
Extended tests for opi.core.cluster_config module.

Tests cluster config retrieval, domain extraction, edge cases, and helper functions.
"""

from unittest.mock import patch

import pytest
from opi.core.cluster_config import (
    CLUSTER_CONFIG,
    _compute_ca_hash,
    get_argo_namespace,
    get_ca_certificate_config,
    get_cluster_config,
    get_cluster_domains_config,
    get_database_server,
    get_external_dns_target_for_hostname,
    get_ingress_cluster_issuer,
    get_ingress_config,
    get_ingress_ip_whitelist,
    get_ingress_postfix,
    get_ingress_tls_enabled,
    get_keycloak_config,
    get_keycloak_discovery_url,
    get_keycloak_support_http,
    get_letsencrypt_contact_email,
    get_managed_zones,
    get_minio_host,
    get_minio_port,
    get_minio_server,
    get_namespace,
    get_namespace_prefix,
    get_prefixed_namespace,
    get_redis_server,
    get_storage_access_modes,
    get_storage_class_name,
    get_storage_config,
    get_supported_domain_names,
    get_volume_snapshot_class,
    is_domain_supported,
    is_platform_domain,
    uses_capsule,
)


class TestGetClusterConfig:
    """Tests for get_cluster_config."""

    def test_valid_local_cluster(self):
        config = get_cluster_config("local")
        assert config["ingress_postfix"] == ".kind"

    def test_valid_production_cluster(self):
        config = get_cluster_config("odcn-production")
        assert "ingress_postfix" in config

    def test_unknown_cluster_raises(self):
        with pytest.raises(ValueError, match="not found in configuration"):
            get_cluster_config("nonexistent-cluster")

    def test_returns_full_config_dict(self):
        config = get_cluster_config("local")
        expected_keys = ["ingress_postfix", "namespace_prefix", "namespace", "argo_namespace"]
        for key in expected_keys:
            assert key in config


class TestIngressFunctions:
    """Tests for ingress-related config functions."""

    def test_get_ingress_postfix_local(self):
        assert get_ingress_postfix("local") == ".kind"

    def test_get_ingress_postfix_production(self):
        result = get_ingress_postfix("odcn-production")
        assert ".rijksapps.nl" in result

    def test_get_ingress_postfix_invalid_cluster(self):
        with pytest.raises(ValueError, match="not found in configuration"):
            get_ingress_postfix("invalid")

    def test_get_ingress_config_local(self):
        config = get_ingress_config("local")
        assert config["enable_tls"] is True
        assert "ip_whitelist" in config

    def test_get_ingress_tls_enabled_local(self):
        assert get_ingress_tls_enabled("local") is True

    def test_get_ingress_ip_whitelist_local(self):
        result = get_ingress_ip_whitelist("local")
        assert result == "0.0.0.0"

    def test_get_ingress_cluster_issuer_local(self):
        result = get_ingress_cluster_issuer("local")
        assert result == "kind-ca-issuer"

    def test_get_ingress_cluster_issuer_production(self):
        result = get_ingress_cluster_issuer("odcn-production")
        assert result is None  # Not configured in production


class TestNamespaceFunctions:
    """Tests for namespace-related config functions."""

    def test_get_namespace_prefix_local(self):
        assert get_namespace_prefix("local") == "rig-"

    def test_get_namespace_prefix_production(self):
        assert get_namespace_prefix("odcn-production") == "rig-prd-"

    def test_get_argo_namespace_local(self):
        assert get_argo_namespace("local") == "rig-system"

    def test_get_prefixed_namespace(self):
        result = get_prefixed_namespace("local", "myproject")
        assert result == "rig-myproject"

    def test_get_prefixed_namespace_production(self):
        result = get_prefixed_namespace("odcn-production", "myproject")
        assert result == "rig-prd-myproject"

    def test_get_namespace_local(self):
        assert get_namespace("local") == "rig-system"


class TestStorageFunctions:
    """Tests for storage config functions."""

    def test_get_storage_config_local(self):
        config = get_storage_config("local")
        assert config["storage_class_name"] == "csi-hostpath-sc"

    def test_get_storage_class_name_local(self):
        assert get_storage_class_name("local") == "csi-hostpath-sc"

    def test_get_storage_access_modes_local(self):
        assert get_storage_access_modes("local") == ["ReadWriteOnce"]

    def test_get_volume_snapshot_class_local(self):
        result = get_volume_snapshot_class("local")
        assert result == "csi-hostpath-snapclass"


class TestKeycloakFunctions:
    """Tests for Keycloak config functions."""

    def test_get_keycloak_discovery_url_local(self):
        result = get_keycloak_discovery_url("local")
        assert result == "https://keycloak.kind"

    def test_get_keycloak_config_local(self):
        config = get_keycloak_config("local")
        assert config["support_http"] is True

    def test_get_keycloak_support_http_local(self):
        assert get_keycloak_support_http("local") is True

    def test_get_keycloak_support_http_production(self):
        assert get_keycloak_support_http("odcn-production") is False


class TestDatabaseAndMinioFunctions:
    """Tests for database and MinIO config functions."""

    def test_get_database_server_local(self):
        result = get_database_server("local")
        assert "rig-db-rw" in result

    def test_get_minio_host_local(self):
        result = get_minio_host("local")
        assert "minio" in result

    def test_get_minio_port_local(self):
        assert get_minio_port("local") == 9000

    def test_get_minio_server_local(self):
        result = get_minio_server("local")
        assert ":9000" in result

    def test_get_redis_server_local(self):
        result = get_redis_server("local")
        assert "redis" in result


class TestCapsuleAndLetsEncrypt:
    """Tests for capsule and Let's Encrypt config."""

    def test_local_does_not_use_capsule(self):
        assert uses_capsule("local") is False

    def test_production_uses_capsule(self):
        assert uses_capsule("odcn-production") is True

    def test_get_letsencrypt_contact_email(self):
        email = get_letsencrypt_contact_email("local")
        assert email is not None
        assert "@" in email


class TestComputeCaHash:
    """Tests for _compute_ca_hash."""

    def test_nonexistent_file(self):
        result = _compute_ca_hash("/nonexistent/cert.pem")
        assert result is None

    @patch("opi.core.cluster_config.Path")
    @patch("opi.core.cluster_config.subprocess.run")
    def test_successful_hash(self, mock_run, mock_path):
        mock_path.return_value.exists.return_value = True
        mock_run.return_value.stdout = "abcdef01\n"
        mock_run.return_value.returncode = 0

        result = _compute_ca_hash("/tmp/cert.pem")
        assert result == "abcdef01"

    @patch("opi.core.cluster_config.Path")
    @patch("opi.core.cluster_config.subprocess.run")
    def test_openssl_failure(self, mock_run, mock_path):
        mock_path.return_value.exists.return_value = True
        mock_run.side_effect = FileNotFoundError("openssl not found")

        result = _compute_ca_hash("/tmp/cert.pem")
        assert result is None


class TestCaCertificateConfig:
    """Tests for get_ca_certificate_config."""

    def test_local_has_ca_config(self):
        with patch("opi.core.cluster_config._compute_ca_hash", return_value="abc123"):
            config = get_ca_certificate_config("local")
        assert config is not None
        assert config["enabled"] is True
        assert "node_path" in config
        assert "container_path" in config
        assert config["hash"] == "abc123"

    def test_production_no_ca_config(self):
        config = get_ca_certificate_config("odcn-production")
        assert config is None


class TestClusterDomainFunctions:
    """Tests for the cluster domain configuration functions."""

    def test_get_cluster_domains_config_local(self):
        config = get_cluster_domains_config("local")
        assert config is not None
        assert "supported_domains" in config

    def test_get_supported_domain_names_local(self):
        domains = get_supported_domain_names("local")
        assert "kind" in domains
        assert "local" in domains

    def test_is_domain_supported_kind(self):
        assert is_domain_supported("local", "kind") is True

    def test_is_domain_not_supported(self):
        assert is_domain_supported("local", "example.com") is False

    def test_production_supported_domains(self):
        domains = get_supported_domain_names("odcn-production")
        assert "rijks.app" in domains


class TestExternalDnsTargetForHostname:
    """De external-dns-annotatie komt uit hetzelfde ``domains``-blok als de rest.

    Bij een verkeerde sleutel krijgt elke hostnaam None en verdwijnt de annotatie zonder
    foutmelding uit de ingress. Daarom pinnen deze toetsen een echt doel uit de configuratie.
    """

    def test_subdomain_gets_the_target_of_its_base_domain(self):
        assert get_external_dns_target_for_hostname("odcn-production", "mijnapp.rijks.app") == "router.rijks.app"

    def test_bare_base_domain_gets_the_same_target(self):
        assert get_external_dns_target_for_hostname("odcn-production", "rijks.app") == "router.rijks.app"

    def test_each_configured_domain_has_its_own_target(self):
        assert get_external_dns_target_for_hostname("odcn-production", "a.rijksapp.nl") == "router.rijksapp.nl"
        assert get_external_dns_target_for_hostname("odcn-production", "a.rijksapp.dev") == "router.rijksapp.dev"

    def test_hostname_in_the_cluster_postfix_zone_has_no_target(self):
        """De standaardzone van het cluster krijgt zijn DNS van de router zelf."""
        assert get_external_dns_target_for_hostname("odcn-production", "a.rig.prd1.gn2.quattro.rijksapps.nl") is None

    def test_a_name_that_only_ends_in_the_same_letters_is_not_a_subdomain(self):
        assert get_external_dns_target_for_hostname("odcn-production", "nietrijks.app") is None

    def test_cluster_without_configured_targets_returns_none(self):
        """De Kind-clusters publiceren geen DNS, dus geen enkel domein heeft een doel."""
        assert get_external_dns_target_for_hostname("local", "mijnapp.kind") is None
        assert get_external_dns_target_for_hostname("sandboxed-local", "mijnapp.sandbox.rijksapp.dev") is None

    def test_the_most_specific_domain_wins(self):
        """Valt een naam onder twee geconfigureerde domeinen, dan telt de langste."""
        config = {
            "supported_domains": [
                {"domain": "example.nl", "external_dns_target": "router.example.nl"},
                {"domain": "test.example.nl", "external_dns_target": "router.test.example.nl"},
            ]
        }
        with patch("opi.core.cluster_config.get_cluster_domains_config", return_value=config):
            target = get_external_dns_target_for_hostname("odcn-production", "app.test.example.nl")

        assert target == "router.test.example.nl"

    def test_cluster_without_domains_block_returns_none(self):
        with patch("opi.core.cluster_config.get_cluster_domains_config", return_value=None):
            assert get_external_dns_target_for_hostname("odcn-production", "mijnapp.rijks.app") is None

    def test_unknown_cluster_raises(self):
        with pytest.raises(ValueError, match="not found in configuration"):
            get_external_dns_target_for_hostname("nonexistent-cluster", "mijnapp.rijks.app")


class TestManagedZones:
    """De reserveringslijst hangt aan dit antwoord: welke zones ZAD zelf bedient."""

    def test_local_serves_its_two_domains(self):
        assert get_managed_zones("local") == ["kind", "local"]

    def test_sandbox_serves_its_two_domains(self):
        assert get_managed_zones("sandboxed-local") == ["sandbox.rijksapp.dev", "robbertuittenbroek.nl"]

    def test_production_also_names_its_postfix_zone(self):
        assert get_managed_zones("odcn-production") == [
            "rijks.app",
            "rijksapp.nl",
            "rijksapp.dev",
            "rig.prd1.gn2.quattro.rijksapps.nl",
        ]

    def test_cluster_without_domains_block_has_no_zones(self):
        with patch("opi.core.cluster_config.get_cluster_domains_config", return_value=None):
            assert get_managed_zones("odcn-production") == []

    def test_unknown_cluster_raises(self):
        with pytest.raises(ValueError, match="not found in configuration"):
            get_managed_zones("nonexistent-cluster")


class TestIsPlatformDomain:
    """De beheervraag: valt deze naam binnen een zone die wij bedienen."""

    def test_an_offered_domain_is_ours(self):
        assert is_platform_domain("odcn-production", "rijks.app") is True
        assert is_platform_domain("odcn-production", "rijksapp.nl") is True
        assert is_platform_domain("odcn-production", "rijksapp.dev") is True

    def test_a_name_under_an_offered_domain_is_ours(self):
        """Suffix, geen lidmaatschap: team.rijks.app staat in geen enkele lijst en is toch
        een label op onze registreerbare zone."""
        assert is_platform_domain("odcn-production", "team.rijks.app") is True

    def test_the_postfix_zone_is_ours(self):
        assert is_platform_domain("odcn-production", "rig.prd1.gn2.quattro.rijksapps.nl") is True

    def test_the_parent_of_the_postfix_zone_is_not_ours(self):
        """rijksapps.nl is van ODC-Noord en in gebruik als eigen basisdomein van projecten."""
        assert is_platform_domain("odcn-production", "ux-onderzoeken.rijksapps.nl") is False
        assert is_platform_domain("odcn-production", "rijksapps.nl") is False

    def test_a_tenant_domain_is_not_ours(self):
        assert is_platform_domain("odcn-production", "uitbetrouwbarebron.nl") is False

    def test_a_name_that_only_ends_in_the_same_letters_is_not_ours(self):
        assert is_platform_domain("odcn-production", "nietrijks.app") is False

    def test_sandbox_serves_the_domain_it_offers(self):
        assert is_platform_domain("sandboxed-local", "robbertuittenbroek.nl") is True
        assert is_platform_domain("sandboxed-local", "rijks.app") is False

    def test_unknown_cluster_raises(self):
        with pytest.raises(ValueError, match="not found in configuration"):
            is_platform_domain("nonexistent-cluster", "rijks.app")


class TestManagedZonesCoverTheRest:
    """De beheerlijst staat los, dus hij kan wegdrijven. Deze toets is de grendel: vergeet
    iemand een nieuwe zone, dan valt hij om in plaats van dat er stil een gat ontstaat.
    """

    @pytest.mark.parametrize("cluster_name", sorted(CLUSTER_CONFIG))
    def test_the_postfix_zone_is_managed(self, cluster_name):
        zone = get_ingress_postfix(cluster_name).lstrip(".")
        assert is_platform_domain(cluster_name, zone), f"{cluster_name}: {zone} staat in geen managed_zones"

    @pytest.mark.parametrize("cluster_name", sorted(CLUSTER_CONFIG))
    def test_every_offered_domain_is_managed(self, cluster_name):
        for domain in get_supported_domain_names(cluster_name):
            assert is_platform_domain(cluster_name, domain), f"{cluster_name}: {domain} staat in geen managed_zones"


class TestSelectableClusters:
    """The create wizard offers only the clusters the environment is configured for.

    Production must never let a user pick a dev cluster: the choice comes from the
    managing cluster's own config, defaulting to just that cluster.
    """

    def test_production_offers_only_odcn_production(self):
        from opi.core.cluster_config import get_selectable_clusters

        # odcn-production has no create_wizard_clusters key, so it defaults to itself.
        with patch("opi.core.config.settings.CLUSTER_MANAGER", "odcn-production"):
            assert get_selectable_clusters() == ["odcn-production"]

    def test_development_offers_the_configured_set(self):
        from opi.core.cluster_config import get_selectable_clusters

        with patch("opi.core.config.settings.CLUSTER_MANAGER", "local"):
            clusters = get_selectable_clusters()
        assert clusters == ["local", "sandboxed-local", "odcn-production"]

    def test_unknown_configured_cluster_is_dropped(self):
        """A typo in the config must not surface a non-existent cluster."""
        from opi.core import cluster_config as cc

        with (
            patch("opi.core.config.settings.CLUSTER_MANAGER", "local"),
            patch.dict(cc.CLUSTER_CONFIG["local"], {"create_wizard_clusters": ["local", "does-not-exist"]}),
        ):
            assert cc.get_selectable_clusters() == ["local"]

    def test_wizard_provider_follows_the_selectable_set(self):
        from opi.forms.visualizers.providers import ClusterOptionsProvider

        with patch("opi.core.config.settings.CLUSTER_MANAGER", "odcn-production"):
            values = [o["value"] for o in ClusterOptionsProvider().get_options()]
        assert values == ["odcn-production"], values
