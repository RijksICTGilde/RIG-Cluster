"""Tests for the cluster-default base-domain resolver.

Regression for the production domain bug: selecting "Cluster standaard"
(empty base-domain) wrongly showed the "Domein aanvragen" checkbox and
materialised a nice-URL domain (e.g. rijks.app) plus a phantom domain
request into the saved project file.

Root cause: ClusterDefaultDomain.resolve() returned the first *nice-URL*
domain instead of the cluster's default *ingress* domain. On a cluster
where those coincide (e.g. local: ingress ".kind", first supported
"kind") the bug is invisible — which is why sandbox/tests never caught
it. The production cluster has ingress ".rig.prd1...rijksapps.nl" with a
different first supported domain, so the resolved value never equalled
the cluster default and DomainNeedsRequestCondition fired.
"""

from unittest.mock import patch

from opi.forms.editables.conditions import DomainNeedsRequestCondition
from opi.forms.editables.hooks import _resolve_missing_base_domains
from opi.forms.editables.resolvers import ClusterDefaultDomain
from opi.services.catalog.publish_on_web.domain_config import DomainSetting, get_domain_setting

_PROD = "odcn-production"
_PROD_DEFAULT = "rig.prd1.gn2.quattro.rijksapps.nl"


def _prod_settings():
    return patch("opi.core.config.settings.CLUSTER_MANAGER", _PROD)


class TestClusterDefaultDomainResolver:
    def test_resolves_to_cluster_ingress_domain_not_first_nice_url(self):
        with _prod_settings():
            resolved = ClusterDefaultDomain().resolve({})
        assert resolved == _PROD_DEFAULT
        # The bug returned a nice-URL domain like rijks.app
        assert resolved != "rijks.app"


class TestRequestCheckboxVisibility:
    def test_checkbox_hidden_for_cluster_default(self):
        """Empty base-domain (cluster default) must NOT show the request checkbox."""
        cond = DomainNeedsRequestCondition(deployment_index=0)
        resolvers = {"deployments[0]/base-domain": ClusterDefaultDomain()}
        cond.set_resolvers(resolvers)
        data = {"deployments": [{"name": "productie"}]}  # no base-domain -> cluster default
        with _prod_settings():
            assert cond.check(data) is False

    def test_checkbox_shown_for_real_non_default_domain(self):
        cond = DomainNeedsRequestCondition(deployment_index=0)
        data = {"deployments": [{"name": "productie", "base-domain": "klant.example.com"}]}
        with _prod_settings():
            assert cond.check(data) is True


class TestResolveMissingBaseDomains:
    def test_cluster_default_is_not_materialised(self):
        """The hook must not write the cluster default into the deployment."""
        data = {"deployments": [{"name": "productie"}]}
        resolvers = {"deployments[0]/base-domain": ClusterDefaultDomain()}
        with _prod_settings():
            _resolve_missing_base_domains(data, {"resolvers": resolvers})
        assert get_domain_setting(data["deployments"][0], DomainSetting.BASE_DOMAIN) is None

    def test_real_resolved_domain_is_materialised(self):
        """A genuine non-default resolver value is still filled in."""

        class _Fixed:
            def resolve(self, yaml_data):
                return "klant.example.com"

        data = {"deployments": [{"name": "productie"}]}
        resolvers = {"deployments[0]/base-domain": _Fixed()}
        with _prod_settings():
            _resolve_missing_base_domains(data, {"resolvers": resolvers})
        assert get_domain_setting(data["deployments"][0], DomainSetting.BASE_DOMAIN) == "klant.example.com"
