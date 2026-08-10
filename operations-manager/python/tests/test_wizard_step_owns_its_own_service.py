"""A wizard step stores the service it configures, and no more.

Reported three times, and the first two fixes missed it. The symptom: pick sso-only in the
keycloak step, and the invite step still offers a local account; go back, and the keycloak
step shows the old value again.

Two defects, and either one alone keeps it broken:

1. ``_extract_section_data`` copied the WHOLE services list for any section touching it,
   so the invite step carried a stale copy of the keycloak config. Because invite-config
   comes after keycloak-config in the section order, that copy won on merge. Measured on a
   real session file: keycloak-config held ``sso-only``, invite-config held ``sso-support``,
   and the merge produced ``sso-support``.
2. ``get_merged_data`` merged only the ``services`` key by name. The virtual key holding
   the CONFIG was replaced wholesale, so once step 1 was fixed the keycloak config vanished
   behind the invite step's instead of being wrong.

The first defect hid the second: while every step carried a copy of everything, replacing
looked like it worked.
"""

from __future__ import annotations

from typing import Any

from opi.forms.editables.service_path import smart_get_value
from opi.forms.visualizers.flows import get_flow
from opi.forms.visualizers.wizard_sections import build_domain_section
from opi.forms.wizard.state import WizardState
from opi.web.router_wizard import _extract_section_data

_FLOW = "create-project"


def _section(section_id: str) -> Any:
    return next(s for s in get_flow(_FLOW).sections if s.section_id == section_id)


def _submitted(template: str) -> dict[str, Any]:
    """A submission as the wizard has it: every service present, one being edited."""
    return {
        "services": [
            {"name": "keycloak", "config": {"template": template}},
            {"name": "invite", "config": {"default-language": "nl"}},
            "health-check",
        ]
    }


def test_a_step_stores_only_the_service_it_configures() -> None:
    stored = _extract_section_data(_section("keycloak-config").editables, _submitted("sso-only"))

    names = [e.get("name") for e in stored["_services-config"] if isinstance(e, dict)]
    assert names == ["keycloak"], f"the keycloak step should not carry other services: {names}"


def test_a_bare_selection_entry_survives() -> None:
    """A service chosen without config is the SELECTION, not someone else's config."""
    stored = _extract_section_data(_section("keycloak-config").editables, _submitted("sso-only"))

    assert "health-check" in stored["_services-config"]


def test_the_invite_step_does_not_carry_the_keycloak_config() -> None:
    stored = _extract_section_data(_section("invite-config").editables, _submitted("sso-support"))

    names = [e.get("name") for e in stored["_services-config"] if isinstance(e, dict)]
    assert "keycloak" not in names


def test_two_steps_keep_each_other_s_config_on_merge() -> None:
    """The second defect: merging the virtual key by replace loses the other step's work."""
    state = WizardState(flow_id=_FLOW, current_step="invite-config")
    state.active_sections = ["services", "keycloak-config", "invite-config"]
    state.store_step_data("services", {"services": ["keycloak", "invite"]})
    state.store_step_data(
        "keycloak-config", _extract_section_data(_section("keycloak-config").editables, _submitted("sso-only"))
    )
    state.store_step_data(
        "invite-config", _extract_section_data(_section("invite-config").editables, _submitted("sso-only"))
    )

    merged = state.get_merged_data()

    assert smart_get_value(merged, "services/keycloak/config/template") == "sso-only"
    assert smart_get_value(merged, "services/invite/config/default-language") == "nl"


def test_the_later_step_cannot_revive_an_older_value() -> None:
    """The reported symptom, end to end: the keycloak step is edited last in wall-clock
    time but sits earlier in the section order, and its value must still win."""
    state = WizardState(flow_id=_FLOW, current_step="keycloak-config")
    state.active_sections = ["services", "keycloak-config", "invite-config"]
    state.store_step_data("services", {"services": ["keycloak", "invite"]})
    state.store_step_data(
        "invite-config", _extract_section_data(_section("invite-config").editables, _submitted("sso-support"))
    )
    state.store_step_data(
        "keycloak-config", _extract_section_data(_section("keycloak-config").editables, _submitted("sso-only"))
    )

    assert smart_get_value(state.get_merged_data(), "services/keycloak/config/template") == "sso-only"


def _deployment_submitted() -> dict[str, Any]:
    """A deployment as the Webadres step has it: its own service plus everyone else's."""
    return {
        "deployments": [
            {
                "name": "productie",
                "services": [
                    {"reference": "publish-on-web", "config": {"subdomain": "wies"}},
                    {"reference": "cross-domain-access", "config": {"allow-from": ["ander-project"]}},
                    "temp-storage",
                ],
            }
        ]
    }


def test_a_deployment_step_stores_only_its_own_service_entries() -> None:
    """The per-item counterpart: identity decides, not shape.

    A section addressing ``deployments[*]/services{publish-on-web}/config/...`` owns that
    one entry. Another service's entry is not this section's to carry -- neither its config
    record nor its bare selection string, which is just as much a statement about a service
    this step never mentioned (RC-60 review, suggestion 2).
    """
    stored = _extract_section_data(build_domain_section(0).editables, _deployment_submitted())

    entries = stored["deployments"][0]["services"]
    assert [e.get("reference") for e in entries] == ["publish-on-web"]


def test_the_other_services_survive_the_merge() -> None:
    """Dropping them is safe because the merge is additive by name, not a replace."""
    state = WizardState(flow_id=_FLOW, current_step="domains")
    state.active_sections = ["domains"]
    state.base_data = _deployment_submitted()
    state.store_step_data("domains", _extract_section_data(build_domain_section(0).editables, _deployment_submitted()))

    merged_services = state.get_merged_data()["deployments"][0]["services"]

    names = [e.get("reference") if isinstance(e, dict) else e for e in merged_services]
    assert names == ["publish-on-web", "cross-domain-access", "temp-storage"]
