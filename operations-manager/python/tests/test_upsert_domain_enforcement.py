"""Tests for API-side domain enforcement in upsert_deployment.

The wizard runs DomainConfigEnforcer; the API upsert path previously did not,
so a dot-separated URL format could be paired with a dash-only domain (e.g.
the ODCN cluster domain), producing an unreachable multi-label hostname.
ProjectManager._enforce_domain_config reuses the same enforcer so the API
enforces the same rule.

"The same rule" includes the enforcer's distinction between an error and a warning. A
``FieldWarning`` means "dit is op aanvraag" and is non-blocking in the wizard; the API
used to collapse it into a refusal, which meant that asking for a domain through the API
could only fail. It now lets the write through, so the request the caller needs actually
gets made (see tests/test_publish_on_web_aanvraag_via_api.py).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_manager():
    with (
        patch("opi.manager.project_manager.KubectlConnector"),
        patch("opi.handlers.sops.SopsHandler"),
        patch("opi.generation.manifests.ManifestGenerator"),
        patch("opi.manager.argo_manager.ArgoManager", return_value=MagicMock()),
        patch("opi.manager.bootstrap_manager.BootstrapManager", return_value=MagicMock()),
        patch("opi.manager.delete_project_manager.DeleteProjectManager", return_value=MagicMock()),
        patch("opi.manager.keycloak_manager.KeycloakManager", return_value=MagicMock()),
        patch("opi.manager.minio_manager.MinioManager", return_value=MagicMock()),
        patch("opi.manager.redis_manager.RedisManager", return_value=MagicMock()),
        patch("opi.manager.pvc_manager.PVCManager", return_value=MagicMock()),
    ):
        from opi.manager.project_manager import ProjectManager

        pm = ProjectManager()
        pm.get_name = AsyncMock(return_value="demo")
        return pm


def _project(domain_format: str, base_domain: str) -> dict:
    return {
        "name": "demo",
        "deployments": [
            {
                "name": "productie",
                "base-domain": base_domain,
                "domain-format": domain_format,
            }
        ],
    }


class TestEnforceDomainConfig:
    async def test_dot_format_on_dash_only_domain_is_rejected(self):
        pm = _make_manager()
        data = _project("deployment.project", "rijksapps.nl")
        with patch("opi.forms.editables.enforcers.get_domain_supports_dots", return_value=False):
            error = await pm._enforce_domain_config(data, "productie")
        assert error is not None
        assert "punten" in error.lower()  # "ondersteunt geen punten in de domeinnaam"

    async def test_dash_format_on_dash_only_domain_is_allowed(self):
        pm = _make_manager()
        data = _project("component-deployment-project", "rijksapps.nl")
        with (
            patch("opi.forms.editables.enforcers.get_domain_supports_dots", return_value=False),
            patch("opi.forms.editables.enforcers.get_supported_base_domains", return_value={"rijksapps.nl"}),
        ):
            error = await pm._enforce_domain_config(data, "productie")
        assert error is None

    async def test_dot_format_on_dots_capable_domain_is_allowed(self):
        pm = _make_manager()
        data = _project("deployment.project", "custom.example.com")
        with (
            patch("opi.forms.editables.enforcers.get_domain_supports_dots", return_value=True),
            patch("opi.forms.editables.enforcers.get_supported_base_domains", return_value={"custom.example.com"}),
        ):
            error = await pm._enforce_domain_config(data, "productie")
        assert error is None

    async def test_unknown_deployment_name_is_noop(self):
        pm = _make_manager()
        data = _project("deployment.project", "rijksapps.nl")
        with patch("opi.forms.editables.enforcers.get_domain_supports_dots", return_value=False):
            error = await pm._enforce_domain_config(data, "does-not-exist")
        assert error is None

    async def test_unapproved_literal_base_domain_becomes_a_request(self):
        """A literal base-domain that is neither cluster-supported nor approved for the
        project is ON REQUEST -- which is not the same thing as invalid.

        The approval gate must still FIRE for it: it used to fire only for the
        '__custom__' sentinel, so an API upsert could set an arbitrary out-of-cluster
        domain nobody ever looked at. That guard stays, and the enforcer still raises for
        this domain -- but as the non-blocking ``FieldWarning`` it is in the wizard, so
        the caller lands on the same request flow instead of on an error. What keeps the
        domain from being USED meanwhile is not this gate but
        ``apply_domain_approval_fallback`` (tests/test_domain_approval.py): an unapproved
        domain publishes on the cluster address until an approver says otherwise.
        """
        from opi.forms.editables.enforcers import DomainConfigEnforcer, FieldWarning

        pm = _make_manager()
        data = _project("component-deployment-project", "evil.example.org")
        with (
            patch("opi.forms.editables.enforcers.get_domain_supports_dots", return_value=False),
            patch("opi.forms.editables.enforcers.get_supported_base_domains", return_value={"rijksapps.nl"}),
        ):
            error = await pm._enforce_domain_config(data, "productie")

            # Het is geen fout, maar het gaat ook niet ongemerkt langs: de enforcer ziet
            # het domein wel degelijk, en zegt er "op aanvraag" over.
            with pytest.raises(FieldWarning) as raised:
                await DomainConfigEnforcer(deployment_index=0).enforce(data, {"project_name": "demo"})

        assert error is None
        assert "evil.example.org" in str(raised.value)

    async def test_approved_project_domain_is_allowed(self):
        """A literal base-domain that the project has approved passes the gate."""
        pm = _make_manager()
        data = _project("component-deployment-project", "mijn-app.nl")
        data["domains"] = {"allowed-domains": [{"domain": "mijn-app.nl", "status": "approved"}]}
        with (
            patch("opi.forms.editables.enforcers.get_domain_supports_dots", return_value=False),
            patch("opi.forms.editables.enforcers.get_supported_base_domains", return_value={"rijksapps.nl"}),
        ):
            error = await pm._enforce_domain_config(data, "productie")
        assert error is None

    async def test_enforces_against_the_named_deployment_index(self):
        """The enforcer must target the upserted deployment, not always index 0."""
        pm = _make_manager()
        data = {
            "name": "demo",
            "deployments": [
                {"name": "staging", "base-domain": "rijksapps.nl", "domain-format": "component-deployment-project"},
                {"name": "productie", "base-domain": "rijksapps.nl", "domain-format": "deployment.project"},
            ],
        }
        with (
            patch("opi.forms.editables.enforcers.get_domain_supports_dots", return_value=False),
            patch("opi.forms.editables.enforcers.get_supported_base_domains", return_value={"rijksapps.nl"}),
        ):
            # staging (index 0) uses a dash format -> valid
            assert await pm._enforce_domain_config(data, "staging") is None
            # productie (index 1) uses a dot format on a dash-only domain -> rejected
            assert await pm._enforce_domain_config(data, "productie") is not None

    async def test_cluster_default_with_subdomain_skips_approval(self):
        """A cluster-default deployment (no base-domain) must not be validated against
        an arbitrary restricted platform domain's subdomain rules.

        Regression for the pr797 bug: the enforcer used to resolve a missing
        base-domain to next(iter(supported)) -- an arbitrary supported domain --
        and run ITS subdomain restrictions, wrongly rejecting cluster-default PR
        deployments with "subdomein 'pr797' voor 'rijksapp.dev' is op aanvraag".
        """
        pm = _make_manager()
        data = {
            "name": "regel-k4c",
            "deployments": [
                # No base-domain -> cluster default. Uses a {subdomain} nice-URL format.
                {"name": "pr797", "domain-format": "subdomain", "subdomain": "pr797"},
            ],
        }
        with (
            patch("opi.forms.editables.enforcers.get_supported_base_domains", return_value={"rijksapp.dev"}),
            patch("opi.forms.editables.enforcers.get_domain_supports_dots", return_value=True),
            # Even though the arbitrary supported domain is restricted, the cluster
            # default must skip it entirely -- so this must never be consulted.
            patch(
                "opi.forms.editables.enforcers.is_subdomain_allowed_for_project",
                return_value=(False, "restricted"),
            ),
        ):
            error = await pm._enforce_domain_config(data, "pr797")
        assert error is None

    async def test_explicit_restricted_domain_subdomain_still_requires_approval(self):
        """The subdomain-approval gate still fires for an EXPLICITLY chosen restricted domain.

        Same correction as the domain case above: firing means "op aanvraag", which the
        API now turns into a request rather than into a refusal. The gate itself must keep
        firing -- a restricted domain whose subdomains nobody checks is the whole reason
        it is restricted.
        """
        from opi.forms.editables.enforcers import DomainConfigEnforcer, FieldWarning

        pm = _make_manager()
        data = {
            "name": "regel-k4c",
            "deployments": [
                {"name": "pr797", "base-domain": "rijksapp.dev", "domain-format": "subdomain", "subdomain": "pr797"},
            ],
        }
        with (
            patch("opi.forms.editables.enforcers.get_supported_base_domains", return_value={"rijksapp.dev"}),
            patch("opi.forms.editables.enforcers.get_domain_supports_dots", return_value=True),
            patch(
                "opi.forms.editables.enforcers.is_subdomain_allowed_for_project",
                return_value=(False, "restricted"),
            ),
            patch("opi.forms.editables.enforcers.get_subdomain_status", return_value=None),
        ):
            error = await pm._enforce_domain_config(data, "pr797")

            with pytest.raises(FieldWarning) as raised:
                await DomainConfigEnforcer(deployment_index=0).enforce(data, {"project_name": "regel-k4c"})

        assert error is None
        assert "pr797" in str(raised.value)
