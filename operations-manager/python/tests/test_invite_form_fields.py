"""What the invite form offers, requires and prefills.

Every assertion here corresponds to something that was wrong on 5 August:

* the key's help text showed ``&lt;sleutel&gt;`` because ROOS re-emits attribute values,
* not one of the fifteen fields was required, so "volgende" always went through,
* the domain restriction was offered as if it took a list of addresses,
* the auth methods looked like a free choice while an invite can only narrow the realm,
* the texts and the contact address started empty,
* and ``active`` rendered as a list while every project has exactly one invite.
"""

from __future__ import annotations

from typing import Any

import pytest
from opi.forms.editables.editable import WidgetType
from opi.forms.visualizers.bridge import editable_to_form_field, should_render_editable
from opi.forms.visualizers.providers import InviteAuthMethodOptionsProvider
from opi.services.catalog.invite.editables import (
    INVITE_ACTIVE_EDITABLE,
    INVITE_EDITABLES,
    INVITE_ITEM_CHILD_EDITABLES,
    INVITE_RESTRICT_DOMAIN_EDITABLE,
)
from opi.services.catalog.invite.visualizers import INVITE_ACTIVE, INVITE_AUTH_METHODS, INVITE_KEY

PROJECT: dict[str, Any] = {
    "name": "regel-k4c",
    "display-name": "Regelbeheer",
    "users": [
        {"email": "beheer@rijksoverheid.nl", "role": "admin"},
        {"email": "tweede@rijksoverheid.nl", "role": "member"},
    ],
}


def _keycloak(template: str | None) -> dict[str, Any]:
    if template is None:
        return dict(PROJECT)
    return {**PROJECT, "services": [{"keycloak": {"config": {"template": template}}}]}


def _visualizers_by_label() -> dict[str, Any]:
    return {child.label: child for child in INVITE_ACTIVE.children or []}


# --- the escaping bug --------------------------------------------------------


def test_help_text_carries_no_angle_brackets() -> None:
    """Help text becomes the ROOS ``helperText`` ATTRIBUTE, and ROOS re-emits attribute
    values, so anything needing escaping is escaped twice and the reader sees the entities.
    """
    for child in INVITE_ACTIVE.children or []:
        assert "<" not in (child.help_text or ""), child.label
        assert ">" not in (child.help_text or ""), child.label


def test_the_key_help_still_explains_where_the_key_sits() -> None:
    """Dropping the brackets must not drop the meaning."""
    help_text = editable_to_form_field(INVITE_KEY, PROJECT).help_text or ""
    assert "/invite/" in help_text
    assert "leeg" in help_text.lower(), "the generate-one-for-me escape hatch must stay explained"


# --- required fields ---------------------------------------------------------


def test_the_fields_that_end_up_on_a_page_are_required() -> None:
    """The contact address and all six texts are shown to an invited user; empty means a
    page with a blank spot on it."""
    required = {
        child.editable.yaml_path.rsplit("/", 1)[-1] for child in INVITE_ACTIVE.children or [] if child.editable.required
    }
    assert {"contact-email", "nl", "en"} <= required


def test_the_key_is_not_required() -> None:
    """Empty means "generate a safe random one", which is the better default."""
    assert INVITE_KEY.editable.required is False


def _visualizer_for(path_suffix: str) -> Any:
    """Look a child up by its yaml_path, not its label: the label is user-facing text
    that gets reworded, and a test failing on that says nothing about behaviour."""
    for child in INVITE_ACTIVE.children or []:
        if child.editable.yaml_path.endswith(path_suffix):
            return child
    raise AssertionError(f"no invite field writes {path_suffix!r}")


def test_the_application_url_is_not_required() -> None:
    """A project without publish-on-web has no address to offer, and an invitation
    without a destination simply shows no button."""
    assert _visualizer_for("application-url").editable.required is False


def test_the_application_url_is_picked_not_typed() -> None:
    """Someone setting up an invitation knows which deployment and component people
    should land on, not the hostname: that is derived from the domain format, the
    subdomain and the cluster, so typing it means looking it up and getting it wrong."""
    visualizer = _visualizer_for("application-url")

    assert visualizer.widget == WidgetType.SELECT
    assert visualizer.editable.values_provider == "InviteApplicationUrlOptionsProvider"


def test_every_required_field_has_a_default() -> None:
    """Required without a default is just an obstacle: nothing offers a value, so the form
    refuses to advance on a field the user was never shown a starting point for. This is
    the rule that pushed application-url back to optional rather than leaving it stuck."""
    for child in INVITE_ACTIVE.children or []:
        if child.editable.required:
            assert child.editable.default is not None, child.label


# --- computed defaults -------------------------------------------------------


def test_contact_email_is_prefilled_with_the_first_person_on_the_project() -> None:
    field = editable_to_form_field(_visualizers_by_label()["Contact-e-mailadres"], PROJECT, index=0)
    assert field.value == "beheer@rijksoverheid.nl"


def test_the_texts_are_prefilled_and_name_the_project() -> None:
    for label in ("Welkomstbericht (Nederlands)", "Welkomstbericht (Engels)"):
        value = editable_to_form_field(_visualizers_by_label()[label], PROJECT, index=0).value
        assert value, label
        assert "Regelbeheer" in value, f"{label} should name the project"


def test_a_stored_value_is_never_overwritten_by_a_default() -> None:
    """The whole point of a default is that it only fills a gap."""
    stored = {
        **PROJECT,
        "services": [{"invite": {"config": {"active": [{"contact-email": "eigen@example.com"}]}}}],
    }
    field = editable_to_form_field(_visualizers_by_label()["Contact-e-mailadres"], stored, index=0)
    assert field.value == "eigen@example.com"


def test_a_project_without_people_gets_no_contact_default_instead_of_a_crash() -> None:
    field = editable_to_form_field(_visualizers_by_label()["Contact-e-mailadres"], {"name": "leeg"}, index=0)
    assert field.value in (None, "")


# --- the domain restriction --------------------------------------------------


def test_the_domain_restriction_is_not_offered_in_the_form() -> None:
    labels = {child.label for child in INVITE_ACTIVE.children or []}
    assert "Domeinbeperking" not in labels


def test_the_domain_restriction_editable_still_exists() -> None:
    """Hidden, not removed: files that carry it must keep validating, and
    InviteManager.validate_email_domain keeps enforcing it."""
    assert INVITE_RESTRICT_DOMAIN_EDITABLE in INVITE_EDITABLES
    assert INVITE_RESTRICT_DOMAIN_EDITABLE in INVITE_ITEM_CHILD_EDITABLES


# --- auth methods ------------------------------------------------------------


@pytest.mark.parametrize(
    ("template", "expected"),
    [
        ("sso-support", ["sso", "local"]),
        ("sso-only", ["sso"]),
        (None, ["sso"]),
    ],
)
def test_auth_method_options_follow_the_keycloak_template(template: str | None, expected: list[str]) -> None:
    """sso-only sets registrationAllowed/loginWithEmailAllowed to false in the blueprint,
    so a local account cannot exist in that realm. An absent template means sso-only,
    which is the default on KeycloakConfig.template."""
    provider = InviteAuthMethodOptionsProvider(yaml_data=_keycloak(template))
    assert [option["value"] for option in provider.get_options()] == expected


@pytest.mark.parametrize(("template", "shown"), [("sso-support", True), ("sso-only", False), (None, False)])
def test_auth_methods_are_only_shown_when_there_is_something_to_choose(template: str | None, shown: bool) -> None:
    assert should_render_editable(INVITE_AUTH_METHODS, _keycloak(template), index=0) is shown


def test_the_provider_and_the_visibility_rule_agree() -> None:
    """They are two mechanisms answering one question; disagreeing would mean either a
    hidden field with real options or a visible field with one."""
    for template in ("sso-support", "sso-only", None):
        data = _keycloak(template)
        multiple = len(InviteAuthMethodOptionsProvider(yaml_data=data).get_options()) > 1
        assert should_render_editable(INVITE_AUTH_METHODS, data, index=0) is multiple, template


# --- exactly one invite ------------------------------------------------------


def test_the_form_pins_the_invite_list_to_exactly_one() -> None:
    assert INVITE_ACTIVE_EDITABLE.min_items == 1
    assert INVITE_ACTIVE_EDITABLE.max_items == 1
    assert INVITE_ACTIVE_EDITABLE.add_remove is False


def test_it_is_still_a_list_underneath() -> None:
    """Pinned in the form, not migrated on disk: existing files with several invites keep
    validating and keep being served."""
    assert INVITE_ACTIVE_EDITABLE.yaml_path.endswith("active")
    assert INVITE_ACTIVE_EDITABLE.children


class TestDeRolIsEenKeuzeGeenLijst:
    """Het schema laat meerdere realm-rollen toe en de opslag blijft een lijst, maar in de
    praktijk is het er altijd een. De wizard toonde er een reeks voor, met knoppen om
    rollen toe te voegen en te verwijderen; dat suggereert een mogelijkheid die niemand
    gebruikt en die de stap onnodig ingewikkeld maakt.
    """

    def test_de_wizard_toont_precies_een_keuze(self) -> None:
        from opi.services.catalog.invite.visualizers import INVITE_REALM_ROLES

        editable = INVITE_REALM_ROLES.editable
        assert editable.min_items == 1, "er hoort altijd een keuzelijst te staan"
        assert editable.max_items == 1, "er hoort er nooit meer dan een te kunnen"
        # Gelijke min en max betekent voor de sequence-template: geen toevoeg- of
        # verwijderknoppen, alleen het veld. Zie widgets/sequence.html.j2 (fixed_size).
        assert editable.min_items == editable.max_items

    def _verwerk(self, rol: str) -> object:
        import asyncio

        from opi.forms.editables.processor import EditableFormProcessor
        from opi.services.catalog.base import ConfigLayer
        from opi.services.registry import SERVICES
        from opi.services.services_enums import ServiceType

        sectie = SERVICES[ServiceType.INVITE].config_form_section(ConfigLayer.PROJECT)
        assert sectie is not None
        submitted = {
            "_services-config": [{"name": "invite", "config": {"active": [{"name": "u", "realm-roles": [rol]}]}}]
        }
        resultaat, _errors = asyncio.run(
            EditableFormProcessor().process_json_submission(submitted, sectie.editables, {}, edit_mode=False)
        )
        return resultaat["services"][0]["config"]["active"][0]

    def test_een_gekozen_rol_wordt_als_lijst_bewaard(self) -> None:
        """De opslagvorm verandert niet; alleen de invoer is een enkele keuze."""
        assert self._verwerk("beheerder")["realm-roles"] == ["beheerder"]  # type: ignore[index]

    def test_geen_rol_schrijft_geen_sleutel(self) -> None:
        """ "Geen rol toekennen" is een echte keuze en betekent geen rol, niet een rol
        zonder naam. Zonder dit schreef de altijd-zichtbare keuzelijst [""] weg."""
        assert "realm-roles" not in self._verwerk("")  # type: ignore[operator]
