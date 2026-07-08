"""
Tests for domain restriction features.

Tests the subdomain restriction and custom domain approval system:
- Cluster config restricted_subdomains flag
- Project-level allowed-subdomains
- Custom domain approval status
- DomainConfigEnforcer integration with restrictions
"""

from unittest.mock import AsyncMock

import pytest
from opi.connectors.subdomain import (
    get_project_allowed_domain_config,
    get_project_allowed_subdomains,
    is_domain_allowed_for_project,
    is_subdomain_allowed_for_project,
)
from opi.core.cluster_config import (
    get_restricted_subdomain_domains,
    is_domain_subdomain_restricted,
)

# ---------------------------------------------------------------------------
# Cluster config helpers
# ---------------------------------------------------------------------------


class TestIsDomainSubdomainRestricted:
    def test_local_kind_is_restricted(self):
        assert is_domain_subdomain_restricted("local", "kind") is True

    def test_local_local_is_restricted(self):
        assert is_domain_subdomain_restricted("local", "local") is True

    def test_sandboxed_local_domains_are_restricted(self):
        assert is_domain_subdomain_restricted("sandboxed-local", "sandbox.rijksapp.dev") is True
        assert is_domain_subdomain_restricted("sandboxed-local", "robbertuittenbroek.nl") is True

    def test_production_domains_are_restricted(self):
        assert is_domain_subdomain_restricted("odcn-production", "rijks.app") is True
        assert is_domain_subdomain_restricted("odcn-production", "rijksapp.nl") is True
        assert is_domain_subdomain_restricted("odcn-production", "rijksapp.dev") is True

    def test_unknown_domain_is_not_restricted(self):
        assert is_domain_subdomain_restricted("local", "unknown.com") is False

    def test_unknown_cluster_raises(self):
        with pytest.raises(ValueError, match="not found"):
            is_domain_subdomain_restricted("nonexistent", "rijks.app")


class TestGetRestrictedSubdomainDomains:
    def test_local_returns_all_domains(self):
        domains = get_restricted_subdomain_domains("local")
        assert "kind" in domains
        assert "local" in domains

    def test_production_returns_all_domains(self):
        domains = get_restricted_subdomain_domains("odcn-production")
        assert set(domains) == {"rijks.app", "rijksapp.nl", "rijksapp.dev"}

    def test_unknown_cluster_raises(self):
        with pytest.raises(ValueError, match="not found"):
            get_restricted_subdomain_domains("nonexistent")


# ---------------------------------------------------------------------------
# Project-level allowed subdomains helpers
# ---------------------------------------------------------------------------


class TestGetProjectAllowedSubdomains:
    def test_returns_subdomains_for_matching_domain(self):
        project_data = {
            "domains": {
                "allowed-subdomains": [
                    {
                        "domain": "rijks.app",
                        "subdomains": [
                            {"name": "wies", "status": "approved"},
                            {"name": "portaal", "status": "approved"},
                        ],
                    },
                    {
                        "domain": "rijksapp.nl",
                        "subdomains": [
                            {"name": "test", "status": "approved"},
                        ],
                    },
                ]
            }
        }
        result = get_project_allowed_subdomains(project_data, "rijks.app")
        assert len(result) == 2
        assert result[0]["name"] == "wies"
        result_nl = get_project_allowed_subdomains(project_data, "rijksapp.nl")
        assert len(result_nl) == 1
        assert result_nl[0]["name"] == "test"

    def test_returns_empty_for_no_match(self):
        project_data = {
            "domains": {
                "allowed-subdomains": [
                    {
                        "domain": "rijks.app",
                        "subdomains": [
                            {"name": "wies", "status": "approved"},
                        ],
                    },
                ]
            }
        }
        assert get_project_allowed_subdomains(project_data, "rijksapp.dev") == []

    def test_returns_empty_for_no_domains_section(self):
        assert get_project_allowed_subdomains({}, "rijks.app") == []

    def test_returns_empty_for_no_allowed_subdomains(self):
        project_data = {"domains": {}}
        assert get_project_allowed_subdomains(project_data, "rijks.app") == []


# ---------------------------------------------------------------------------
# Custom domain config helpers
# ---------------------------------------------------------------------------


class TestGetProjectCustomDomainConfig:
    def test_returns_config_for_matching_domain(self):
        project_data = {
            "domains": {
                "allowed-domains": [
                    {
                        "domain": "mijn-app.nl",
                        "supports-dots": True,
                        "issuer": "letsencrypt",
                        "status": "approved",
                    }
                ]
            }
        }
        config = get_project_allowed_domain_config(project_data, "mijn-app.nl")
        assert config is not None
        assert config["domain"] == "mijn-app.nl"
        assert config["supports-dots"] is True
        assert config["status"] == "approved"

    def test_returns_none_for_no_match(self):
        project_data = {"domains": {"allowed-domains": [{"domain": "other.nl", "status": "approved"}]}}
        assert get_project_allowed_domain_config(project_data, "mijn-app.nl") is None

    def test_returns_none_for_no_domains_section(self):
        assert get_project_allowed_domain_config({}, "mijn-app.nl") is None


# ---------------------------------------------------------------------------
# Subdomain restriction validation
# ---------------------------------------------------------------------------


class TestIsSubdomainAllowedForProject:
    def test_approved_subdomain_passes(self):
        project_data = {
            "domains": {
                "allowed-subdomains": [
                    {
                        "domain": "rijks.app",
                        "subdomains": [
                            {"name": "wies", "status": "approved"},
                            {"name": "portaal", "status": "approved"},
                        ],
                    },
                ]
            }
        }
        is_allowed, error = is_subdomain_allowed_for_project("wies", "rijks.app", project_data, "odcn-production")
        assert is_allowed is True
        assert error is None

    def test_requested_subdomain_fails(self):
        project_data = {
            "domains": {
                "allowed-subdomains": [
                    {
                        "domain": "rijks.app",
                        "subdomains": [
                            {"name": "wies", "status": "requested"},
                        ],
                    },
                ]
            }
        }
        is_allowed, error = is_subdomain_allowed_for_project("wies", "rijks.app", project_data, "odcn-production")
        assert is_allowed is False
        assert "aangevraagd" in error

    def test_denied_subdomain_fails(self):
        project_data = {
            "domains": {
                "allowed-subdomains": [
                    {
                        "domain": "rijks.app",
                        "subdomains": [
                            {"name": "wies", "status": "denied"},
                        ],
                    },
                ]
            }
        }
        is_allowed, error = is_subdomain_allowed_for_project("wies", "rijks.app", project_data, "odcn-production")
        assert is_allowed is False
        assert "afgewezen" in error

    def test_unknown_subdomain_fails(self):
        project_data = {
            "domains": {
                "allowed-subdomains": [
                    {
                        "domain": "rijks.app",
                        "subdomains": [
                            {"name": "wies", "status": "approved"},
                        ],
                    },
                ]
            }
        }
        is_allowed, error = is_subdomain_allowed_for_project("other", "rijks.app", project_data, "odcn-production")
        assert is_allowed is False
        assert "niet aangevraagd" in error

    def test_no_allowed_subdomains_fails(self):
        project_data = {}
        is_allowed, error = is_subdomain_allowed_for_project("wies", "rijks.app", project_data, "odcn-production")
        assert is_allowed is False
        assert "niet aangevraagd" in error

    def test_case_insensitive_match(self):
        project_data = {
            "domains": {
                "allowed-subdomains": [
                    {
                        "domain": "rijks.app",
                        "subdomains": [
                            {"name": "Wies", "status": "approved"},
                        ],
                    },
                ]
            }
        }
        is_allowed, error = is_subdomain_allowed_for_project("wies", "rijks.app", project_data, "odcn-production")
        assert is_allowed is True

    def test_custom_domain_unrestricted_passes(self):
        """Custom domains without restricted-subdomains allow any subdomain."""
        project_data = {
            "domains": {
                "allowed-domains": [{"domain": "mijn-app.nl", "status": "approved", "restricted-subdomains": False}]
            }
        }
        is_allowed, error = is_subdomain_allowed_for_project("anything", "mijn-app.nl", project_data, "odcn-production")
        assert is_allowed is True

    def test_custom_domain_restricted_checks_allowlist(self):
        """Custom domains with restricted-subdomains check the allow-list."""
        project_data = {
            "domains": {
                "allowed-domains": [{"domain": "mijn-app.nl", "status": "approved", "restricted-subdomains": True}],
                "allowed-subdomains": [
                    {
                        "domain": "mijn-app.nl",
                        "subdomains": [
                            {"name": "api", "status": "approved"},
                        ],
                    },
                ],
            }
        }
        is_allowed, error = is_subdomain_allowed_for_project("api", "mijn-app.nl", project_data, "odcn-production")
        assert is_allowed is True

        is_allowed, error = is_subdomain_allowed_for_project("other", "mijn-app.nl", project_data, "odcn-production")
        assert is_allowed is False


# ---------------------------------------------------------------------------
# Custom domain approval validation
# ---------------------------------------------------------------------------


class TestIsCustomDomainAllowedForProject:
    def test_approved_domain_passes(self):
        project_data = {"domains": {"allowed-domains": [{"domain": "mijn-app.nl", "status": "approved"}]}}
        is_allowed, error = is_domain_allowed_for_project("mijn-app.nl", project_data)
        assert is_allowed is True
        assert error is None

    def test_requested_domain_fails(self):
        project_data = {"domains": {"allowed-domains": [{"domain": "mijn-app.nl", "status": "requested"}]}}
        is_allowed, error = is_domain_allowed_for_project("mijn-app.nl", project_data)
        assert is_allowed is False
        assert "requested" in error

    def test_denied_domain_fails(self):
        project_data = {"domains": {"allowed-domains": [{"domain": "mijn-app.nl", "status": "denied"}]}}
        is_allowed, error = is_domain_allowed_for_project("mijn-app.nl", project_data)
        assert is_allowed is False
        assert "denied" in error

    def test_unregistered_domain_fails(self):
        project_data = {}
        is_allowed, error = is_domain_allowed_for_project("mijn-app.nl", project_data)
        assert is_allowed is False
        assert "niet goedgekeurd" in error

    def test_domain_with_history(self):
        project_data = {
            "domains": {
                "allowed-domains": [
                    {
                        "domain": "mijn-app.nl",
                        "status": "approved",
                        "history": [
                            {
                                "date": "2026-03-28T14:30:00+00:00",
                                "status": "approved",
                                "by": "admin@rijksoverheid.nl",
                                "message": "Goedgekeurd",
                            },
                            {
                                "date": "2026-03-27T10:00:00+00:00",
                                "status": "requested",
                                "by": "dev@rijksoverheid.nl",
                                "message": "Aangevraagd",
                            },
                        ],
                    }
                ]
            }
        }
        is_allowed, error = is_domain_allowed_for_project("mijn-app.nl", project_data)
        assert is_allowed is True


# ---------------------------------------------------------------------------
# Pydantic model tests
# ---------------------------------------------------------------------------


class TestDomainsModel:
    def test_parse_from_yaml_dict(self):
        from opi.forms.models.project_file import DomainsModel

        data = {
            "allowed-subdomains": [
                {
                    "domain": "rijks.app",
                    "subdomains": [
                        {"name": "wies", "status": "approved"},
                        {"name": "portaal", "status": "requested"},
                    ],
                },
            ],
            "allowed-domains": [
                {
                    "domain": "mijn-app.nl",
                    "supports-dots": True,
                    "issuer": "letsencrypt",
                    "restricted-subdomains": False,
                    "status": "approved",
                    "history": [
                        {
                            "date": "2026-03-28T14:30:00+00:00",
                            "status": "approved",
                            "by": "admin@rijksoverheid.nl",
                            "message": "Goedgekeurd",
                        }
                    ],
                }
            ],
        }
        model = DomainsModel.model_validate(data)
        assert len(model.allowed_subdomains) == 1
        assert model.allowed_subdomains[0].domain == "rijks.app"
        assert len(model.allowed_subdomains[0].subdomains) == 2
        assert model.allowed_subdomains[0].subdomains[0].name == "wies"
        assert model.allowed_subdomains[0].subdomains[0].status == "approved"
        assert model.allowed_subdomains[0].subdomains[1].status == "requested"
        assert len(model.allowed_domains) == 1
        assert model.allowed_domains[0].domain == "mijn-app.nl"
        assert model.allowed_domains[0].supports_dots is True
        assert model.allowed_domains[0].status == "approved"
        assert len(model.allowed_domains[0].history) == 1

    def test_empty_domains_model(self):
        from opi.forms.models.project_file import DomainsModel

        model = DomainsModel.model_validate({})
        assert model.allowed_subdomains == []
        assert model.allowed_domains == []

    def test_to_yaml_dict(self):
        from opi.forms.models.project_file import DomainsModel

        model = DomainsModel(
            allowed_subdomains=[],
            allowed_domains=[],
        )
        d = model.model_dump(by_alias=True, exclude_none=True)
        assert "allowed-subdomains" in d
        assert "allowed-domains" in d

    def test_project_file_model_with_domains(self):
        from opi.forms.models.project_file import ProjectFileModel

        data = {
            "name": "test-project",
            "display-name": "Test Project",
            "users": [{"email": "admin@test.nl", "role": "admin"}],
            "domains": {
                "allowed-subdomains": [
                    {
                        "domain": "rijks.app",
                        "subdomains": [
                            {"name": "wies", "status": "approved"},
                        ],
                    },
                ],
                "allowed-domains": [
                    {"domain": "mijn-app.nl", "status": "approved"},
                ],
            },
        }
        model = ProjectFileModel.model_validate(data)
        assert model.domains is not None
        assert len(model.domains.allowed_subdomains) == 1
        assert len(model.domains.allowed_domains) == 1

    def test_project_file_model_without_domains(self):
        from opi.forms.models.project_file import ProjectFileModel

        data = {
            "name": "test-project",
            "display-name": "Test Project",
            "users": [{"email": "admin@test.nl", "role": "admin"}],
        }
        model = ProjectFileModel.model_validate(data)
        assert model.domains is None


# ---------------------------------------------------------------------------
# DomainConfigEnforcer integration: receives full project data
# ---------------------------------------------------------------------------


class TestDomainConfigEnforcerReceivesFullData:
    """Verify that DomainConfigEnforcer.enforce() receives the full project YAML,
    including the root-level 'domains' section needed for subdomain restriction checks.

    The processor passes ``result = copy.deepcopy(yaml_data)`` to the enforcer,
    so it always has access to domains.allowed-subdomains.
    """

    @pytest.mark.asyncio
    async def test_enforcer_allows_subdomain_from_project_allowlist(self, monkeypatch):
        """Enforcer allows a subdomain that is in the project's allowed-subdomains list."""
        from opi.connectors.subdomain import SubdomainConnector
        from opi.forms.editables.enforcers import DomainConfigEnforcer

        monkeypatch.setattr("opi.core.config.settings", type("S", (), {"CLUSTER_MANAGER": "odcn-production"})())
        monkeypatch.setattr(SubdomainConnector, "get_by_subdomain", AsyncMock(return_value=None))

        enforcer = DomainConfigEnforcer(deployment_index=0)
        # Full project YAML with domains.allowed-subdomains at root level
        full_yaml = {
            "deployments": [
                {
                    "name": "productie",
                    "domain-format": "subdomain",
                    "base-domain": "rijks.app",
                    "subdomain": "wies",
                }
            ],
            "domains": {
                "allowed-subdomains": [
                    {
                        "domain": "rijks.app",
                        "subdomains": [
                            {"name": "wies", "status": "approved"},
                            {"name": "portaal", "status": "approved"},
                        ],
                    },
                ],
            },
        }
        # Should not raise — subdomain 'wies' is approved in the allow-list
        result = await enforcer.enforce(full_yaml, {"project_name": "test-project"})
        assert result is full_yaml

    @pytest.mark.asyncio
    async def test_enforcer_rejects_subdomain_not_in_allowlist(self, monkeypatch):
        """Enforcer rejects a subdomain that is NOT in the project's allowed-subdomains list."""
        from opi.forms.editables.enforcers import DomainConfigEnforcer

        monkeypatch.setattr("opi.core.config.settings", type("S", (), {"CLUSTER_MANAGER": "odcn-production"})())

        enforcer = DomainConfigEnforcer(deployment_index=0)
        full_yaml = {
            "deployments": [
                {
                    "name": "productie",
                    "domain-format": "subdomain",
                    "base-domain": "rijks.app",
                    "subdomain": "unauthorized",
                }
            ],
            "domains": {
                "allowed-subdomains": [
                    {
                        "domain": "rijks.app",
                        "subdomains": [
                            {"name": "wies", "status": "approved"},
                        ],
                    },
                ],
            },
        }
        from opi.forms.editables.enforcers import FieldWarning

        with pytest.raises(FieldWarning, match="op aanvraag"):
            await enforcer.enforce(full_yaml, {"project_name": "test-project"})

    @pytest.mark.asyncio
    async def test_enforcer_warns_when_no_domains_section(self, monkeypatch):
        """Enforcer warns about subdomain when project has no domains section at all."""
        from opi.forms.editables.enforcers import DomainConfigEnforcer, FieldWarning

        monkeypatch.setattr("opi.core.config.settings", type("S", (), {"CLUSTER_MANAGER": "odcn-production"})())

        enforcer = DomainConfigEnforcer(deployment_index=0)
        # No 'domains' key — simulates a new project without subdomain config
        yaml_without_domains = {
            "deployments": [
                {
                    "name": "productie",
                    "domain-format": "subdomain",
                    "base-domain": "rijks.app",
                    "subdomain": "anything",
                }
            ],
        }
        with pytest.raises(FieldWarning, match="op aanvraag"):
            await enforcer.enforce(yaml_without_domains, {"project_name": "test-project"})


# ---------------------------------------------------------------------------
# IssuerGenerator with custom domain config
# ---------------------------------------------------------------------------


class TestIssuerGeneratorCustomDomain:
    def test_uses_custom_domain_issuer(self, monkeypatch):
        """IssuerGenerator reads issuer from project custom-domains config."""
        from opi.forms.editables.generators import IssuerGenerator

        monkeypatch.setattr("opi.core.config.settings", type("S", (), {"CLUSTER_MANAGER": "local"})())

        yaml_data = {
            "deployments": [{"base-domain": "mijn-app.nl"}],
            "domains": {
                "allowed-domains": [{"domain": "mijn-app.nl", "issuer": "custom-issuer", "status": "approved"}]
            },
        }
        gen = IssuerGenerator(deployment_index=0)
        result = gen.generate(yaml_data)
        assert result == "custom-issuer"

    def test_falls_back_to_letsencrypt_for_unknown_custom_domain(self, monkeypatch):
        """Custom domains without project config get letsencrypt."""
        from opi.forms.editables.generators import IssuerGenerator

        monkeypatch.setattr("opi.core.config.settings", type("S", (), {"CLUSTER_MANAGER": "local"})())

        yaml_data = {
            "deployments": [{"base-domain": "unknown-domain.nl"}],
        }
        gen = IssuerGenerator(deployment_index=0)
        result = gen.generate(yaml_data)
        assert result == "letsencrypt"
