"""A service's config screen carries the same explanation as its card.

The question mark that opens a service's own explanation existed on the service card and
in the overview, but not on the screen where the config is actually filled in -- which is
the one place where someone has to decide something.

Stamped where the sections are collected, not per service: a service declares its
explanation once on its ServiceDefinition, and every surface picks it up. A service that
builds its own PROJECT-level section (keycloak, sleep-mode) therefore needs no line for
this either.
"""

from __future__ import annotations

import pathlib

import opi
import pytest
from opi.forms.visualizers import wizard_sections
from opi.services.services import ServiceAdapter

_STEP = pathlib.Path(opi.__file__).parent / "templates_lotc/bg/_modal-wizard-step.html.j2"
# A help_template resolves against the template root, the service catalog (a service's
# own help.md lives in its package, RC-36), or templates/help for the few
# explanations that belong to no single service.
_HELP_ROOTS = (
    pathlib.Path(opi.__file__).parent / "templates_lotc",
    pathlib.Path(opi.__file__).parent / "services" / "catalog",
    pathlib.Path(opi.__file__).parent / "templates_lotc" / "help",
)

_PROJECT_SECTIONS = [
    name
    for name in dir(wizard_sections)
    if name.endswith("_CONFIG_SECTION") and getattr(wizard_sections, name) is not None
]


def test_there_are_config_sections_to_check() -> None:
    """Guard the guard: an empty list would make the parametrised test vacuous."""
    assert len(_PROJECT_SECTIONS) >= 5


@pytest.mark.parametrize("name", _PROJECT_SECTIONS)
def test_every_service_config_section_offers_its_explanation(name: str) -> None:
    section = getattr(wizard_sections, name)

    assert section.help_template, f"{name} has no explanation to open"
    assert any((root / section.help_template).is_file() for root in _HELP_ROOTS), f"{name} points at a missing template"


def test_the_step_template_renders_the_button() -> None:
    source = _STEP.read_text(encoding="utf-8")

    assert "section.help_template" in source
    assert "openServiceHelp" in source


def test_the_stamp_does_not_overwrite_a_section_that_set_its_own() -> None:
    """A service that wants a different explanation on its config screen than on its
    card must be able to say so; the stamp only fills a gap."""
    from opi.forms.visualizers.sections import FormSection
    from opi.forms.visualizers.wizard_sections import _with_service_help
    from opi.services.services_enums import ServiceType

    section = FormSection(section_id="x", title="X", help_template="eigen.html.j2")
    stamped = _with_service_help(section, ServiceType.KEYCLOAK)

    assert stamped is not None
    assert stamped.help_template == "eigen.html.j2"


def test_the_keycloak_choice_says_who_can_log_in() -> None:
    """The old labels described something else. sso-only sets registrationAllowed and
    loginWithEmailAllowed to false in the blueprint, sso-support sets both to true, so
    the difference is local accounts and the label has to say that."""
    from opi.forms.visualizers.providers import KeycloakTemplateOptionsProvider

    labels = {option["value"]: option["label"] for option in KeycloakTemplateOptionsProvider().get_options()}

    assert labels["sso-only"] == "Alleen SSO Rijk"
    assert "lokale" in labels["sso-support"].lower()
    assert "SSO Rijk" in labels["sso-support"]


def test_every_service_still_has_an_explanation_to_stamp() -> None:
    """The stamp is only as good as the source: a service without a help_template gives
    a config screen without a question mark, silently."""
    missing = [t.value for t, d in ServiceAdapter.SERVICE_DEFINITIONS.items() if not d.help_template]
    assert missing == [], f"these services have no explanation: {missing}"
