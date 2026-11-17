"""The outage mode of SSO Rijk on the platform realm.

The truth lives in Keycloak and nowhere else: the realm's ``browserFlow`` IS the state, so a
switch someone made in the admin console shows up here too. OPI keeps no copy.

Only the two flows the blueprint creates can be set. A flow alias never comes from a request;
``features/sso-rijk-uitwijk.md`` holds the whole picture.
"""

import logging
from dataclasses import dataclass

from opi.connectors.keycloak import (
    SSO_OUTAGE_BROWSER_FLOW,
    SSO_REDIRECT_BROWSER_FLOW,
    create_keycloak_connector,
)
from opi.core.config import settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SsoOutageStatus:
    """What the platform realm carries right now."""

    realm: str
    browser_flow: str | None
    outage_active: bool

    @property
    def recognised(self) -> bool:
        """Whether the realm carries one of the two flows the blueprint knows about."""
        return self.browser_flow in (SSO_REDIRECT_BROWSER_FLOW, SSO_OUTAGE_BROWSER_FLOW)


def flow_for(outage_active: bool) -> str:
    return SSO_OUTAGE_BROWSER_FLOW if outage_active else SSO_REDIRECT_BROWSER_FLOW


async def read_status() -> SsoOutageStatus:
    """Read the browser flow of the platform realm straight from Keycloak."""
    realm_name = settings.KEYCLOAK_DEFAULT_REALM
    keycloak = await create_keycloak_connector()
    realm = await keycloak.get_realm(realm_name)
    browser_flow = (realm or {}).get("browserFlow")
    return SsoOutageStatus(
        realm=realm_name,
        browser_flow=browser_flow,
        outage_active=browser_flow == SSO_OUTAGE_BROWSER_FLOW,
    )


async def set_outage(active: bool) -> SsoOutageStatus:
    """Point the platform realm at the outage flow, or back at the SSO redirect.

    Running sessions are untouched: a browser flow only decides what a NEW login does.
    """
    realm_name = settings.KEYCLOAK_DEFAULT_REALM
    wanted = flow_for(active)

    keycloak = await create_keycloak_connector()
    changed = await keycloak.ensure_browser_flow(realm_name, wanted)
    if changed:
        logger.warning(f"Browser flow of realm {realm_name} changed to '{wanted}'")
    else:
        logger.info(f"Browser flow of realm {realm_name} already set to '{wanted}'")

    return SsoOutageStatus(realm=realm_name, browser_flow=wanted, outage_active=active)
