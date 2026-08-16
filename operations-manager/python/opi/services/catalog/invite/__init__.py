"""invite service: onboard users into the project's Keycloak realm via a shared link.

An invite is a configuration-as-code unit at the PROJECT layer. It owns its typed config
model (``InviteConfig``), its drift-locked schema fragment (``invite.v1.0.json``), its
wizard/edit form section, and its read-only detail-page block. It replaces the old top-level
``invites:`` section, which sat outside the service contract and could only be created by
hand-editing YAML.

An invite provisions nothing, so most service hooks stay on their no-op default. That is not
a weakness of the contract (a behaviourless service is a few lines); it is recorded here so
the next reader does not go looking for something that is deliberately absent:

- ``provision`` / ``provision_order`` -- no deployment resource. The realm user is created
  when the invite is REDEEMED (the public /invite routes), outside the deploy cycle.
- ``cleanup_manager_key`` / ``handle_service_removal`` -- intentionally empty. Removing an
  invite must NOT touch the realm users it already created: once they exist they are
  legitimate users with nothing more to do with the invitation.
- ``manifest_secret_class`` / ``contribute_manifest_context`` / ``build_secret_files`` --
  no envFrom secret, no sidecar, no template override.
- ``config_component_layout`` / ``config_component_visualizers`` / ``config_editables(COMPONENT)``
  -- no component layer: an invitation belongs to the project, not to a container.
- ``config_approvals`` -- no approval: the project admin may invite within their own realm.

The one deviation from keycloak/sleep-mode is ``post_save_action="save_only"``: editing an
invite changes no manifests, so it does not trigger a deploy.

De uitnodigingscode komt WEL uit een leesantwoord (bewust besluit)
-----------------------------------------------------------------

``key`` is het geheim in de link, en toch geeft de gewone lezing van de invite-config hem
terug -- en het aanmaken meldt hem als het platform hem genereerde. Dat ziet eruit als een
fout en is het niet; het is een besluit van de eigenaar, en dit staat hier zodat de
volgende lezer hem niet "herstelt". Drie argumenten:

1. **De code IS de uitnodiging.** Je nodigt iemand uit door hem die link te sturen. Wie de
   code niet kan teruglezen kan de uitnodiging niet versturen, alleen vervangen -- en dat
   maakt een uitnodiging die al onderweg is ongeldig.
2. **Hij is niet geheim in de gewone zin.** Wie de link heeft kan hem inwisselen, dus hij
   is precies zo geheim als het kanaal waarover je hem stuurt. Er valt niets te beschermen
   dat het versturen zelf niet al opgeeft.
3. **Verbergen voor de projecteigenaar beschermt niemand.** Die heeft de projectsleutel
   al, waarmee hij de invite kan overschrijven, de rollen kan veranderen en de hele dienst
   kan uitzetten.

Dit is NIET het antwoord voor ``user-env-vars``. Daar geldt een andere afweging: die
waarden kunnen langlevende geheimen bevatten die met het lezen zelf niets te maken hebben,
en ze blijven ontoegankelijk via de API (zie ``Service.owned_value_is_secret``). De twee
gevallen consistent maken is precies de fout die hier niet gemaakt moet worden.
"""

from __future__ import annotations

import logging
import secrets
from typing import Any

from opi.services.catalog.base import ConfigLayer, DetailPageSection, ProjectPageContext, Service, config_path
from opi.services.catalog.events import on
from opi.services.catalog.invite.config_model import InviteConfig
from opi.services.services import ConfigAdvice, ServiceDefinition, service_entry_name
from opi.services.services_enums import ServiceBinding, ServiceType, UIEvent

logger = logging.getLogger(__name__)


def _generate_invite_key() -> str:
    """A 128-bit URL-safe key that also passes ``InviteKeyValidator``.

    ``secrets.token_urlsafe(16)`` is base64url, so it can start with ``-`` or ``_`` --
    which the validator rejects (a key must start with a letter or digit, since it
    becomes an ``/invite/{key}`` path segment re-validated on edit). Regenerate until
    the first character is alphanumeric; length stays 22 and the full 128 bits of
    entropy are kept.
    """
    while True:
        key = secrets.token_urlsafe(16)
        if key[0].isalnum():
            return key


class InviteService(Service):
    service_type = ServiceType.INVITE
    definition = ServiceDefinition(
        name="Uitnodiging",
        description=(
            "Nodig gebruikers uit voor het Keycloak-realm van dit project via een deelbare link. "
            "De link is de enige toegangsdrempel: wie hem heeft kan een account aanmaken. "
            "Vereist de Keycloak-service."
        ),
        help_template="invite/help.md",
        icon="envelop",
        color="lichtblauw",
        # Niet per component en niet per deployment: een uitnodiging geldt voor het
        # Keycloak-realm van het project. Stond op COMPONENT omdat er geen andere waarde
        # was, met als gevolg dat de dienst in de componentkeuze verscheen en de UI meldde
        # dat je hem per component kiest.
        binding=ServiceBinding.PROJECT,
        variables=[],
        # Path-syntax requirement: auto-selects keycloak, locks it in the UI, and validates
        # at submit that keycloak is present. An invite assigns a realm role, so keycloak
        # must exist. Do NOT build a second dependency mechanism next to this.
        requires=["services/keycloak"],
        # De voorwaardelijke tegenhanger van ``requires``, en met opzet geen tweede eis:
        # een uitnodiging zonder rol is volkomen geldig -- ze levert een kaal account op --
        # totdat keycloak alleen nog rolhouders binnenlaat. Vanaf dat moment geeft dezelfde
        # link geen toegang meer, en tot vandaag kwam niemand daar achter tot iemand hem
        # probeerde. De verwachting staat hier, bij de dienst die het VELD bezit, en wijst
        # met een pad naar de voorwaarde elders; generieke code leest allebei.
        config_advice=[
            ConfigAdvice(
                when=config_path(ConfigLayer.PROJECT, ServiceType.KEYCLOAK, "config", "restrict-access", "enabled"),
                expects=config_path(ConfigLayer.PROJECT, ServiceType.INVITE, "config", "active[*]", "realm-roles"),
                message=(
                    "Keycloak beperkt de toegang tot houders van een rol; een uitnodiging zonder "
                    "realm-rol geeft dus geen toegang."
                ),
            )
        ],
    )
    config_model = InviteConfig
    config_schema_version = "1.0"
    config_section_id = "invite-config"
    modal_flow_id = "modal-edit-invite-config"

    # Tijdelijke gevel: naar buiten toe is een uitnodiging er ÉÉN, geen lijst van één. Het
    # bestand houdt de lijst (zie ``InviteConfig.active`` en het schemafragment), het
    # formulier pint hem al af op precies een (``INVITE_ACTIVE_EDITABLE``, min == max == 1),
    # en deze regel doet hetzelfde voor de API. Waarom het een hack is, wat er bij meerdere
    # entries gebeurt en hoe je hem terugdraait: ``Service.api_singular_lists``.
    api_singular_lists = frozenset({"active"})

    # --- helpers ----------------------------------------------------------------

    def _config_selected(self, project_data: dict[str, Any]) -> bool:
        """Section visibility: whether this project selected the invite service."""
        return self.service_type.value in [
            service_entry_name(entry) for entry in project_data.get("services", []) or []
        ]

    def _keycloak_selected(self, data: dict[str, Any]) -> bool:
        """Guard: keycloak must be selected before invites make sense (an invite assigns a
        realm role). ``requires`` already enforces this in the UI; this is the backstop for
        manual YAML / API paths, so the step explains itself instead of showing empty selects.
        """
        return ServiceType.KEYCLOAK.value in [service_entry_name(entry) for entry in data.get("services", []) or []]

    def generate_missing_values(self, project_data: dict[str, Any]) -> dict[str, str]:
        """Fill any empty invite key with a generated 128-bit random key.

        The link is the only barrier, so a blank key becomes an unguessable, permanent
        key. A self-chosen key is left untouched.

        Both write paths run this: the wizard through ``post_merge`` and the API through
        ``registry.generate_missing_values``. It used to be the wizard's alone, so an
        invite created over the API kept the empty string the caller sent and its link was
        ``/invite/`` -- an invitation nobody could redeem. Returned keyed by yaml path,
        because a generated key that the caller cannot read is the same dead invitation.
        """
        from opi.services.project import Project

        base = config_path(ConfigLayer.PROJECT, self.service_type, "config", "active")
        active = Project(project_data).get(base) or []
        generated: dict[str, str] = {}
        for index, entry in enumerate(active):
            if isinstance(entry, dict) and not entry.get("key"):
                entry["key"] = _generate_invite_key()
                generated[f"{base}[{index}]/key"] = entry["key"]
        if generated:
            project_name = project_data.get("name", "unknown")
            logger.info(f"Generated {len(generated)} invite key(s) for project '{project_name}'")
        return generated

    def _generate_missing_keys(self, project_data: dict[str, Any], _wizard_data: dict[str, Any]) -> None:
        """The wizard's ``post_merge`` signature over :meth:`generate_missing_values`.

        The form hands over two dicts and wants nothing back; one implementation serves
        both, so the portal and the API cannot generate keys by different rules.
        """
        self.generate_missing_values(project_data)

    # --- config field ownership -------------------------------------------------

    def config_api_fields(self, layer: ConfigLayer) -> list[str]:
        return self.config_model_field_names() if layer is ConfigLayer.PROJECT else []

    def config_editables(self, layer: ConfigLayer):
        if layer is not ConfigLayer.PROJECT:
            return []
        from opi.services.catalog.invite.editables import INVITE_EDITABLES

        return INVITE_EDITABLES

    def config_form_section(self, layer: ConfigLayer):
        if layer is not ConfigLayer.PROJECT:
            return None
        cached = getattr(self, "_config_section_cache", None)
        if cached is None:
            from opi.forms.editables.enforcers import UniqueInviteKeyEnforcer
            from opi.forms.layout import Fieldset, Sequence
            from opi.forms.visualizers.sections import FormSection
            from opi.services.catalog.invite.visualizers import INVITE_VISUALIZERS

            def cp(*segments: str) -> str:
                return config_path(ConfigLayer.PROJECT, self.service_type, "config", *segments)

            cached = FormSection(
                section_id="invite-config",
                title="Uitnodigingen",
                icon="envelop",
                description="Nodig gebruikers uit voor het Keycloak-realm van dit project via een deelbare link",
                visible=self._config_selected,
                # save_only (not process_project): an invite changes no manifests, so editing
                # it must not trigger a deploy. Do not "correct" this to process_project.
                post_save_action="save_only",
                enforcer=UniqueInviteKeyEnforcer(),
                post_merge=self._generate_missing_keys,
                guard=self._keycloak_selected,
                guard_message="Kies eerst de Keycloak-service en stel minstens één realm-rol in.",
                editables=INVITE_VISUALIZERS,
                layout=[
                    Fieldset(legend="Algemeen", children=[cp("default-language")]),
                    Fieldset(
                        legend="Actieve uitnodigingen",
                        description=(
                            "Elke uitnodiging krijgt een eigen link. De link is de enige toegangsdrempel: "
                            "wie hem heeft kan een account aanmaken in het realm van dit project."
                        ),
                        children=[Sequence(field_name=cp("active"))],
                    ),
                ],
            )
            self._config_section_cache = cached
        return cached

    @on(UIEvent.PROJECT_SECTIONS)
    def invites_block(self, ctx: ProjectPageContext) -> list[DetailPageSection]:
        # The link is the secret, so this is an authorization choice, not a display one:
        # admin/owner only, like the keycloak realm block.
        if ctx.user_role not in ("admin", "owner"):
            return []
        from opi.handlers.project_file_handler import ProjectFileHandler

        handler = ProjectFileHandler()
        invites = handler.get_all_active_invites(ctx.project_data)
        if not invites:
            return []
        default_language = handler.get_invite_settings(ctx.project_data).get("default_language", "nl")
        context = {
            "invites": [
                {
                    "key": invite.get("key"),
                    "realm_roles": list(invite.get("realm_roles", [])) + list(invite.get("roles", [])),
                    "contact_email": invite.get("contact_email"),
                }
                for invite in invites
                if isinstance(invite, dict) and invite.get("key")
            ],
            "default_language": default_language,
        }
        if not context["invites"]:
            return []
        return [DetailPageSection(template="invite/section-detail.html.j2", context=context)]
