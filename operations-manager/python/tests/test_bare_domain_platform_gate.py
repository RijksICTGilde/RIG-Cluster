"""The bare-domain rule must hold on every write path, not just the wizard's.

A bare-domain ingress claims the APEX of a base domain -- and its Let's Encrypt
certificate -- from one tenant namespace. On a platform domain that takes the domain away
from every other tenant, so ``DomainConfigEnforcer`` has always refused it.

It refused it only behind ``if not domain_format: return`` though, and that was enough
while the wizard was the only writer: there the field is reachable only after a format is
chosen. Since the web address moved under the service (RC-60) the field is also writable
through ``PUT /api/v2/projects/{p}/services/publish-on-web/deployments/{d}/config``, whose
body can carry a base-domain and a bare-domain component and no format at all. That shape
walked straight past the rule, through storage and validation, into an apex ingress.

"Not a platform domain" is only half of it. The other half is WHOSE domain it is: a
domain not approved for this project can very well have DNS pointing at this cluster
already -- that is how another tenant serves its subdomains -- so claiming its apex from
here takes over that tenant's domain, certificate included. That half used to sit behind
the same ``domain-format`` early return, in ``is_domain_allowed_for_project``, and the
publication path had no second gate for it: ``apply_domain_approval_fallback`` runs only
in the ``DOMAIN_FORMAT_TEMPLATES`` branch, while ``register_bare_domain`` and the apex
ingress sit outside it and only ever checked syntax and availability.

These tests pin the whole rule to the SHAPE (bare domain + a domain that is not this
project's own), not to the presence of a domain-format, on both gates that see a write and
on the helper the publication path uses.
"""

from unittest.mock import AsyncMock, patch

import pytest
from opi.connectors.subdomain import BARE_DOMAIN_PLATFORM_MESSAGE, validate_bare_domain_allowed
from opi.core.project_schema import ProjectIntegrityError
from opi.forms.editables.enforcers import DomainConfigEnforcer
from opi.manager.project_validation import validate_project_structure


def _project(config: dict, allowed_domains: list[dict] | None = None) -> dict:
    """A project whose single deployment carries ``config`` under publish-on-web."""
    service_config: dict = {}
    if allowed_domains is not None:
        service_config["domains"] = {"allowed-domains": allowed_domains}
    return {
        "name": "demo",
        "services": [{"reference": "publish-on-web", "config": service_config}],
        "deployments": [
            {
                "name": "productie",
                "cluster": "local",
                "namespace": "demo",
                "services": [{"reference": "publish-on-web", "config": config}],
                "components": [{"reference": "frontend", "image": "ghcr.io/org/app:v1"}],
            }
        ],
        "components": [{"name": "frontend", "type": "single", "services": ["publish-on-web"]}],
    }


#: The project owns mijn-app.nl: an approved entry in the allow-list.
_OWNED = [{"domain": "mijn-app.nl", "status": "approved"}]


#: Exactly what the config PUT can store: a platform base domain, a bare-domain component,
#: and no domain-format anywhere.
_PUT_SHAPE = {"base-domain": "rijksapp.dev", "expose-component-on-bare-domain": "frontend"}


class TestEnforcerRefusesBareDomainOnPlatformDomain:
    async def test_without_a_domain_format(self):
        with (
            patch("opi.forms.editables.enforcers.get_supported_base_domains", return_value={"rijksapp.dev"}),
            pytest.raises(ValueError, match="Kaal domein"),
        ):
            await DomainConfigEnforcer().enforce(_project(_PUT_SHAPE), {"project_name": "demo"})

    async def test_with_a_domain_format(self):
        """The wizard shape stays refused too -- this is not a swap of one hole for another."""
        config = {**_PUT_SHAPE, "domain-format": "component-deployment-project"}
        with (
            patch("opi.forms.editables.enforcers.get_supported_base_domains", return_value={"rijksapp.dev"}),
            pytest.raises(ValueError, match="Kaal domein"),
        ):
            await DomainConfigEnforcer().enforce(_project(config), {"project_name": "demo"})

    async def test_own_domain_without_a_format_is_still_allowed(self):
        """The rule is about domains that are not the project's own. A domain approved for
        this project reaches the availability check, which is the only thing standing
        between it and approval."""
        config = {"base-domain": "mijn-app.nl", "expose-component-on-bare-domain": "frontend"}
        with (
            patch("opi.forms.editables.enforcers.get_supported_base_domains", return_value={"rijksapp.dev"}),
            patch.object(DomainConfigEnforcer, "_check_bare_domain_availability", new=AsyncMock()) as availability,
        ):
            await DomainConfigEnforcer().enforce(_project(config, _OWNED), {"project_name": "demo"})
        availability.assert_awaited_once()

    async def test_no_bare_domain_component_is_untouched(self):
        """A deployment that does not ask for a bare domain must not be affected."""
        with patch("opi.forms.editables.enforcers.get_supported_base_domains", return_value={"rijksapp.dev"}):
            await DomainConfigEnforcer().enforce(_project({"base-domain": "rijksapp.dev"}), {"project_name": "demo"})


#: Another tenant's domain: not a platform domain, and not approved for this project.
_FOREIGN_SHAPE = {"base-domain": "victim.nl", "expose-component-on-bare-domain": "frontend"}


class TestEnforcerRefusesBareDomainOnAForeignDomain:
    """The apex of a domain this project does not own is not free just because it is not a
    platform domain -- see the module docstring."""

    async def test_without_a_domain_format(self):
        with (
            patch("opi.forms.editables.enforcers.get_supported_base_domains", return_value={"rijksapp.dev"}),
            patch.object(DomainConfigEnforcer, "_check_bare_domain_availability", new=AsyncMock()),
            pytest.raises(ValueError, match="Kaal domein"),
        ):
            await DomainConfigEnforcer().enforce(_project(_FOREIGN_SHAPE), {"project_name": "demo"})

    async def test_a_self_created_request_does_not_unlock_it(self):
        """``ensure_domain_requests`` writes a ``requested`` entry for any domain a
        deployment names, so a request must not count as ownership."""
        requested = [{"domain": "victim.nl", "status": "requested"}]
        with (
            patch("opi.forms.editables.enforcers.get_supported_base_domains", return_value={"rijksapp.dev"}),
            patch.object(DomainConfigEnforcer, "_check_bare_domain_availability", new=AsyncMock()),
            pytest.raises(ValueError, match="Kaal domein"),
        ):
            await DomainConfigEnforcer().enforce(_project(_FOREIGN_SHAPE, requested), {"project_name": "demo"})

    async def test_a_revoked_approval_stays_saveable(self):
        """The one exemption, and only in the save gate (``denied_blocks=False``): an
        approver revoking a domain a deployment already exposes on the apex must be able to
        record that verdict. Publication refuses the domain outright, so nothing is
        claimed on it."""
        denied = [{"domain": "mijn-app.nl", "status": "denied"}]
        config = {"base-domain": "mijn-app.nl", "expose-component-on-bare-domain": "frontend"}
        with (
            patch("opi.forms.editables.enforcers.get_supported_base_domains", return_value={"rijksapp.dev"}),
            patch.object(DomainConfigEnforcer, "_check_bare_domain_availability", new=AsyncMock()),
        ):
            await DomainConfigEnforcer(denied_blocks=False).enforce(_project(config, denied), {"project_name": "demo"})


class TestSaveGateRefusesTheStoredShape:
    async def test_the_put_shape_cannot_be_stored(self):
        """``validate_project_structure`` is what a config PUT passes through on its way to
        disk, and it runs the same enforcer. It must reject the shape as well."""
        with (
            patch("opi.forms.editables.enforcers.get_supported_base_domains", return_value={"rijksapp.dev"}),
            pytest.raises(ProjectIntegrityError, match="Kaal domein"),
        ):
            await validate_project_structure(_project(_PUT_SHAPE))

    async def test_the_foreign_domain_shape_cannot_be_stored(self):
        """The reported reproduction: a PUT with another tenant's domain, a bare-domain
        component and no domain-format at all. The save gate runs the enforcer with
        ``denied_blocks=False``, so the rule has to hold there too."""
        with (
            patch("opi.forms.editables.enforcers.get_supported_base_domains", return_value={"rijksapp.dev"}),
            patch.object(DomainConfigEnforcer, "_check_bare_domain_availability", new=AsyncMock()),
            pytest.raises(ProjectIntegrityError, match="Kaal domein"),
        ):
            await validate_project_structure(_project(_FOREIGN_SHAPE))


class TestValidateBareDomainAllowed:
    """The helper the publication path calls just before it registers the bare domain and
    renders the apex ingress -- the gate that a write path the form layer never sees
    cannot get around. Both call sites (``register_bare_domain`` and the apex ingress) sit
    outside the format branch that runs ``apply_domain_approval_fallback``, so this helper
    carries the ownership half of the rule as well."""

    def test_platform_domain_is_refused(self):
        with pytest.raises(ValueError, match=BARE_DOMAIN_PLATFORM_MESSAGE):
            validate_bare_domain_allowed("rijksapp.dev", {"rijksapp.dev", "rijksapps.nl"}, _project({}, _OWNED))

    def test_case_is_ignored(self):
        with pytest.raises(ValueError, match=BARE_DOMAIN_PLATFORM_MESSAGE):
            validate_bare_domain_allowed("RijksApp.DEV", {"rijksapp.dev"}, _project({}, _OWNED))

    def test_foreign_domain_is_refused(self):
        with pytest.raises(ValueError, match="niet goedgekeurd voor dit project"):
            validate_bare_domain_allowed("victim.nl", {"rijksapp.dev"}, _project({}, _OWNED))

    def test_unapproved_status_is_refused(self):
        project = _project({}, [{"domain": "victim.nl", "status": "requested"}])
        with pytest.raises(ValueError, match="niet goedgekeurd voor dit project"):
            validate_bare_domain_allowed("victim.nl", {"rijksapp.dev"}, project)

    def test_own_domain_passes(self):
        validate_bare_domain_allowed("mijn-app.nl", {"rijksapp.dev"}, _project({}, _OWNED))
