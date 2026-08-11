"""Where a user configures a service: which of the two look-alike fields answers that (RC-33).

``ServiceDefinition.binding`` and ``ConfigLayer`` read as the same question and are not.
Binding is about *selection* (does a component tick this service, or does a deployment get
it wholesale); the config layers are about *where the settings live*. keycloak is the
counterexample that keeps the two apart: it binds per component while its configuration is
one realm for the whole project.

The user-visible consequence, and why this file exists: on the project-wide services step a
ticked service whose config lives only on the component layer produces no configuration
screen at all -- observed on ``dimp-r0v``, health-check ticked, nothing happened, nothing
said why. ``project_step_config_hint`` is the sentence that says why, and it must read the
layers. Reading the binding instead would tell a keycloak user to go look per component,
where there is nothing to find.
"""

from __future__ import annotations

import pytest
from opi.services.catalog.base import ConfigLayer
from opi.services.config_location import (
    binding_label,
    config_hint_for_value,
    project_step_config_hint,
)
from opi.services.registry import SERVICES
from opi.services.services_enums import ServiceBinding, ServiceKind, ServiceType

#: The services measured (5 August 2026, from the registry) as carrying config only away
#: from the project layer. The first five are user-selectable and are exactly the cards
#: that used to go quiet; the last two are SYSTEM services with no card at all, listed
#: here because their config has the same "not on this step" shape.
COMPONENT_ONLY_SERVICES = [
    ServiceType.PUBLISH_ON_WEB,
    ServiceType.METRICS_SCRAPER,
    ServiceType.HEALTH_CHECK,
    ServiceType.PERSISTENT_STORAGE,
    ServiceType.TEMP_STORAGE,
    ServiceType.USER_ENV_VARS,
    ServiceType.ALIASES,
]


class TestTheSourceOfTruthForWhereIConfigure:
    def test_the_layers_answer_it_not_the_binding(self) -> None:
        # keycloak: bound per component, configured per project. If the hint were derived
        # from the binding it would claim keycloak is configured per component.
        keycloak = SERVICES[ServiceType.KEYCLOAK]
        assert keycloak.definition.binding is ServiceBinding.COMPONENT
        assert keycloak.config_layers() == [ConfigLayer.PROJECT]
        assert project_step_config_hint(ServiceType.KEYCLOAK) is None

    def test_binding_and_layers_disagree_for_at_least_one_service(self) -> None:
        # Guards the reason the two fields are kept apart at all: if they ever became
        # equivalent across the whole catalog, merging them would be the better fix and
        # this test should be the thing that says so.
        disagreeing = [
            service_type
            for service_type, service in SERVICES.items()
            if service.definition.binding is ServiceBinding.COMPONENT
            and service.config_layers() == [ConfigLayer.PROJECT]
        ]
        assert ServiceType.KEYCLOAK in disagreeing

    @pytest.mark.parametrize("service_type", list(SERVICES), ids=lambda s: s.value)
    def test_a_hint_is_shown_exactly_when_the_project_layer_has_no_section(self, service_type) -> None:
        service = SERVICES[service_type]
        has_project_section = service.config_form_section(ConfigLayer.PROJECT) is not None
        has_other_config = any(layer is not ConfigLayer.PROJECT for layer in service.config_layers())
        hint = project_step_config_hint(service_type)
        assert (hint is not None) == (not has_project_section and has_other_config)


class TestTheHintForTheServicesStep:
    @pytest.mark.parametrize("service_type", COMPONENT_ONLY_SERVICES, ids=lambda s: s.value)
    def test_every_measured_service_explains_itself(self, service_type) -> None:
        hint = project_step_config_hint(service_type)
        assert hint is not None
        assert "per component" in hint

    def test_it_names_no_service(self) -> None:
        # The sentence is derived from the layers, so it never has to name a service --
        # which is what keeps a new service from needing a template change.
        names = {service.definition.name for service in SERVICES.values()}
        for service_type in COMPONENT_ONLY_SERVICES:
            hint = project_step_config_hint(service_type)
            assert hint is not None
            assert not any(name in hint for name in names)

    def test_a_service_with_a_project_section_stays_silent(self) -> None:
        for service_type in (ServiceType.SLEEP_MODE, ServiceType.REDIS, ServiceType.MINIO_STORAGE):
            assert project_step_config_hint(service_type) is None

    def test_a_service_without_any_config_stays_silent(self) -> None:
        # Nothing to point at: no layer carries config, so there is no "but here" to give.
        assert SERVICES[ServiceType.PLATFORM].config_layers() == []
        assert project_step_config_hint(ServiceType.PLATFORM) is None

    def test_more_than_one_layer_is_phrased_as_one_sentence(self) -> None:
        # user-env-vars carries config on the component AND deployment-component layer.
        hint = project_step_config_hint(ServiceType.USER_ENV_VARS)
        assert hint is not None
        assert " en " in hint
        assert hint.count(";") == 1

    def test_the_string_form_takes_a_project_file_name(self) -> None:
        assert config_hint_for_value("health-check") == project_step_config_hint(ServiceType.HEALTH_CHECK)

    def test_an_unknown_name_has_nothing_to_explain(self) -> None:
        assert config_hint_for_value("not-a-service") is None
        assert config_hint_for_value("") is None


class TestBindingLabel:
    def test_every_service_has_a_readable_binding(self) -> None:
        for service_type in SERVICES:
            label = binding_label(service_type)
            assert label
            # No raw enum leaking into the page, which is what "Component scope" was.
            assert "scope" not in label.lower()

    def test_it_reflects_the_binding_not_the_config_layer(self) -> None:
        assert binding_label(ServiceType.KEYCLOAK) == binding_label(ServiceType.PUBLISH_ON_WEB)
        assert binding_label(ServiceType.KEYCLOAK) != binding_label(ServiceType.POSTGRESQL_DATABASE)


class TestTheCardsShowIt:
    def _render(self, selected: list[str]) -> str:
        from opi.forms.field import FormField
        from opi.forms.visualizers.providers import ServiceOptionsProvider
        from opi.forms.widgets.lotc import LOTCWidgetAdapter

        field = FormField(
            name="services",
            path="services",
            schema_type=list,
            widget_type="service_cards",
            label="Beschikbare Services",
            value=selected,
            options=ServiceOptionsProvider().get_options(),
        )
        return LOTCWidgetAdapter().render_service_cards(field)

    def test_a_component_only_service_says_where_it_is_configured(self) -> None:
        html = self._render(["health-check"])
        assert project_step_config_hint(ServiceType.HEALTH_CHECK) in html

    def test_the_line_is_carried_by_every_card_and_revealed_when_ticked(self) -> None:
        # The user ticks a card and clicks Next, so a line the server only renders on the
        # *next* request never reaches them. It is in the DOM of every card that has one
        # and shown by CSS while the card is selected -- and wizard.js keeps that class in
        # sync client-side, so it appears at the moment of ticking.
        unticked = self._render([])
        assert project_step_config_hint(ServiceType.HEALTH_CHECK) in unticked
        assert "service-card__hint--config" in unticked
        assert 'data-service="health-check"' in unticked

    def test_a_project_configurable_service_carries_no_line(self) -> None:
        html = self._render(["sleep-mode"])
        assert "Geen projectbrede instellingen" in html  # from the other cards
        # ... but not on sleep-mode's own card, which does get a config step.
        sleep_card = html.split('data-service="sleep-mode"', 1)[1].split("</div>", 3)[0]
        assert "Geen projectbrede instellingen" not in sleep_card

    def test_system_services_have_no_card_to_carry_a_hint(self) -> None:
        # user-env-vars and aliases are in the measured set but are never offered as a
        # card, so the services step is not where their config gets explained.
        offered = {option["value"] for option in _service_options()}
        for service_type in (ServiceType.USER_ENV_VARS, ServiceType.ALIASES):
            assert SERVICES[service_type].definition.kind is ServiceKind.SYSTEM
            assert service_type.value not in offered


def _service_options() -> list[dict[str, object]]:
    from opi.forms.visualizers.providers import ServiceOptionsProvider

    return ServiceOptionsProvider().get_options()


class TestEenVergrendeldeDienstOverleeftHetVersturen:
    """Een vergrendelde dienst is een dienst die AAN moet blijven, en juist die viel weg.

    Een dienst wordt vergrendeld zodra een andere hem vereist (keycloak vereist
    publish-on-web). Vergrendeld BETEKENDE ``disabled`` op de checkbox, en een disabled
    checkbox verstuurt zijn waarde niet. De dienst viel dus uit de selectie juist omdat
    hij verplicht is, terwijl de gebruiker hem niet had uitgezet en dat ook niet KON.

    Gemeten op een echte wizardsessie: de servicesstap had
    ``["keycloak", "invite", "cross-domain-access"]`` opgeslagen, zonder publish-on-web.

    De reparatie was eerst een meereizende hidden input naast de disabled checkbox. Die
    is er niet meer: vergrendeld en niet-verstuurd zijn losgekoppeld. Vergrendeld is nu
    alleen nog een UI-eigenschap (``aria-disabled`` plus een JS-handler die het uitvinken
    terugdraait), de checkbox verstuurt gewoon zijn eigen waarde, en de server vult een
    vereiste dienst hoe dan ook aan (``apply_services_mutation``).
    """

    def _render(self, selected: list[str]) -> str:
        from opi.forms.field import FormField
        from opi.forms.visualizers.providers import ServiceOptionsProvider
        from opi.forms.widgets.lotc import LOTCWidgetAdapter

        field = FormField(
            name="services",
            path="services",
            schema_type=list,
            widget_type="service_cards",
            label="Services",
            value=selected,
            options=ServiceOptionsProvider().get_options(),
        )
        return LOTCWidgetAdapter().render_service_cards(field)

    def _card(self, html: str, service: str) -> str:
        """De HTML van één kaart, tot aan het begin van de volgende."""
        after = html.split(f'data-service="{service}"', 1)[1]
        return after.split("data-service=", 1)[0]

    def test_de_vergrendelde_checkbox_is_niet_disabled(self) -> None:
        # keycloak vereist publish-on-web, dus die kaart is vergrendeld.
        card = self._card(self._render(["keycloak", "publish-on-web"]), "publish-on-web")
        # ``:disabled="true"`` is wat bool_attr rendert. ``aria-disabled`` staat er WEL en
        # is iets anders: dat zegt "niet aanpasbaar", niet "niet versturen".
        assert ':disabled="true"' not in card, (
            "een disabled checkbox verstuurt niets; dan verdwijnt de dienst bij het opslaan"
        )

    def test_de_kaart_is_wel_zichtbaar_vergrendeld(self) -> None:
        html = self._render(["keycloak", "publish-on-web"])
        assert "service-card--locked-checked" in html
        card = self._card(html, "publish-on-web")
        assert "Vereist door:" in card
        # Vanaf het eerste beeld, niet pas nadat de JS een keuze heeft gezien.
        assert 'aria-disabled="true"' in card

    def test_een_niet_vergrendelde_kaart_zegt_dat_ook(self) -> None:
        card = self._card(self._render(["keycloak", "publish-on-web"]), "redis")
        assert 'aria-disabled="false"' in card

    def test_er_reist_geen_verborgen_waarde_meer_mee(self) -> None:
        """De pleister mag weg zodra de koppeling zelf weg is; anders staat de dienst
        twee keer in de POST."""
        html = self._render(["keycloak", "publish-on-web"])
        assert '<input type="hidden" name="services[]"' not in html
