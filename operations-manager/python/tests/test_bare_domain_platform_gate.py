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

These tests pin the rule to the SHAPE (bare domain + platform domain), not to the presence
of a domain-format, on both gates that see a write and on the helper the publication path
uses.
"""

from unittest.mock import AsyncMock, patch

import pytest
from opi.connectors.subdomain import BARE_DOMAIN_PLATFORM_MESSAGE, validate_bare_domain_allowed
from opi.core.project_schema import ProjectIntegrityError
from opi.forms.editables.enforcers import DomainConfigEnforcer
from opi.manager.project_validation import validate_project_structure


def _project(config: dict) -> dict:
    """A project whose single deployment carries ``config`` under publish-on-web."""
    return {
        "name": "demo",
        "deployments": [
            {
                "name": "productie",
                "cluster": "local",
                "namespace": "demo",
                "services": [{"reference": "publish-on-web", "config": config}],
                "components": [{"reference": "frontend", "image": "ghcr.io/org/app:v1"}],
            }
        ],
        "components": [{"name": "frontend", "type": "single"}],
    }


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
        """The rule is about platform domains. A project's own domain reaches the
        availability check, which is the only thing standing between it and approval."""
        config = {"base-domain": "mijn-app.nl", "expose-component-on-bare-domain": "frontend"}
        with (
            patch("opi.forms.editables.enforcers.get_supported_base_domains", return_value={"rijksapp.dev"}),
            patch.object(DomainConfigEnforcer, "_check_bare_domain_availability", new=AsyncMock()) as availability,
        ):
            await DomainConfigEnforcer().enforce(_project(config), {"project_name": "demo"})
        availability.assert_awaited_once()

    async def test_no_bare_domain_component_is_untouched(self):
        """A deployment that does not ask for a bare domain must not be affected."""
        with patch("opi.forms.editables.enforcers.get_supported_base_domains", return_value={"rijksapp.dev"}):
            await DomainConfigEnforcer().enforce(_project({"base-domain": "rijksapp.dev"}), {"project_name": "demo"})


class TestSaveGateRefusesTheStoredShape:
    async def test_the_put_shape_cannot_be_stored(self):
        """``validate_project_structure`` is what a config PUT passes through on its way to
        disk, and it runs the same enforcer. It must reject the shape as well."""
        with (
            patch("opi.forms.editables.enforcers.get_supported_base_domains", return_value={"rijksapp.dev"}),
            pytest.raises(ProjectIntegrityError, match="Kaal domein"),
        ):
            await validate_project_structure(_project(_PUT_SHAPE))


class TestValidateBareDomainAllowed:
    """The helper the publication path calls just before it registers the bare domain and
    renders the apex ingress -- the gate that a write path the form layer never sees
    cannot get around."""

    def test_platform_domain_is_refused(self):
        with pytest.raises(ValueError, match=BARE_DOMAIN_PLATFORM_MESSAGE):
            validate_bare_domain_allowed("rijksapp.dev", {"rijksapp.dev", "rijksapps.nl"})

    def test_case_is_ignored(self):
        with pytest.raises(ValueError, match=BARE_DOMAIN_PLATFORM_MESSAGE):
            validate_bare_domain_allowed("RijksApp.DEV", {"rijksapp.dev"})

    def test_own_domain_passes(self):
        validate_bare_domain_allowed("mijn-app.nl", {"rijksapp.dev"})
