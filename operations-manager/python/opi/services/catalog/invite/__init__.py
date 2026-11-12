"""invite service: onboard users into the project's Keycloak realm via a shared link.

An invite is a configuration-as-code unit at the PROJECT layer. It owns its typed config
model (``InviteConfig``), its drift-locked schema fragment (``invite.v1.1.json``), its
wizard/edit form section, and its read-only detail-page block.

An invite provisions nothing, so most service hooks stay on their no-op default. Recorded
here so the next reader does not go looking for something that is deliberately absent:

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

The invite code comes back in a read response, on purpose
---------------------------------------------------------

``key`` is the secret in the link, and the ordinary read of the invite config returns it;
creating one reports it when the platform generated it. Do not "fix" that. The code IS the
invitation: whoever cannot read it back cannot send the invitation, only replace it, which
invalidates one already on its way. Whoever has the link can redeem it, so the code is
exactly as secret as the channel it travels over, and the project owner already holds the
project key with which they can overwrite the invite, change the roles and switch the
service off.

This is NOT the answer for ``user-env-vars``: those values may carry long-lived secrets that
have nothing to do with reading them, and they stay unreachable over the API (see
``Service.owned_value_is_secret``). Making the two cases consistent is exactly the mistake
not to make here.
"""

from __future__ import annotations

import logging
import secrets
from typing import TYPE_CHECKING, Any

from opi.core.authz.actions import Action
from opi.services.catalog.base import ConfigLayer, DetailPageSection, ProjectPageContext, Service, config_path
from opi.services.catalog.events import on
from opi.services.catalog.invite.config_model import InviteConfig
from opi.services.services import ConfigAdvice, ServiceDefinition, service_entry_name
from opi.services.services_enums import ServiceType, UIEvent

if TYPE_CHECKING:
    from opi.forms.editables.editable import Editable
    from opi.forms.visualizers.sections import FormSection

logger = logging.getLogger(__name__)


#: The lower bound for a SELF-CHOSEN key. The link is the only barrier, so a short key is
#: guessable; a generated key is 22 characters. Applies only to a key that was not already in
#: the project file: see :meth:`InviteService.validate_project_change`.
INVITE_KEY_MIN_LENGTH = 16


def _invite_keys(project_data: dict[str, Any]) -> set[str]:
    """The keys of the active invitations, through the one reader of that field."""
    from opi.services.project_file_handler import ProjectFileHandler

    invites = ProjectFileHandler().get_all_active_invites(project_data)
    return {str(invite["key"]) for invite in invites if isinstance(invite, dict) and invite.get("key")}


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
            "Vereist de Keycloak-service."
        ),
        help_template="invite/help.md",
        icon="envelop",
        color="lichtblauw",
        # An invitation applies to the project's Keycloak realm, so there is nothing to tick
        # per component.
        selectable_per_component=False,
        variables=[],
        # Path-syntax requirement: auto-selects keycloak, locks it in the UI, and validates
        # at submit that keycloak is present. An invite assigns a realm role, so keycloak
        # must exist. Do NOT build a second dependency mechanism next to this.
        requires=["services/keycloak"],
        # The conditional counterpart of ``requires``, and deliberately not a second
        # requirement: an invitation without a role is perfectly valid, it yields a bare
        # account, until keycloak only lets role holders in. From that moment the same link
        # grants no access, and until today nobody found out before someone tried it. The
        # expectation lives here, with the service that owns the FIELD, and points at the
        # condition elsewhere with a path; generic code reads both.
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
    # 1.1 added ``application-target``: the destination of the success button as the
    # component/deployment CHOICE instead of the address that choice worked out to.
    #
    # ``migrate_config`` stays the inherited no-op, deliberately. Existing project files are
    # NOT rewritten: an invitation is a standing arrangement with someone who already holds
    # the link, so converting its destination silently changes where that person ends up,
    # and on a wrong match ends them up somewhere else entirely. ``application-url`` stays
    # valid and equal; the two shapes live side by side with a fixed precedence (see
    # ``destination.resolve_invite_url``).
    config_schema_version = "1.1"
    config_section_id = "invite-config"
    modal_flow_id = "modal-edit-invite-config"

    # Temporary facade: from the outside an invitation is ONE, not a list of one. The file
    # keeps the list (see ``InviteConfig.active`` and the schema fragment), the form already
    # pins it to exactly one (``INVITE_ACTIVE_EDITABLE``, min == max == 1), and this line does
    # the same for the API. Why it is a hack, what happens with several entries and how to
    # undo it: ``Service.api_singular_lists``.
    api_singular_lists = frozenset({"active"})

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
        ``registry.generate_missing_values``. Returned keyed by yaml path, because a
        generated key the caller cannot read is a dead invitation.
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

        It also settles the destination (below), because a save has to leave the entry in
        a shape the model accepts and the picker only owns one of the two fields.
        """
        self.generate_missing_values(project_data)
        self.settle_destination(project_data)

    def settle_destination(self, project_data: dict[str, Any]) -> None:
        """Leave at most one destination per invite: the chosen target wins over an address.

        The portal's picker owns ``application-target`` only -- it offers this project's
        own addresses, and a fixed ``application-url`` is the API/CLI shape for a
        destination outside the project, which the picker can never name. So a project
        file can arrive here with both: an address that was already stored, plus a target
        the user just picked. The model refuses that pair, and rightly: nothing decides
        which of the two the button follows.

        The target wins, because it is the one that was just chosen. Nothing is dropped
        the other way round: an entry with only an address keeps it, since the picker
        writing no target means "no choice made here", not "no destination".
        """
        from opi.services.project import Project

        base = config_path(ConfigLayer.PROJECT, self.service_type, "config", "active")
        # Both spellings of both keys: the on-disk keys are hyphenated, but the files that
        # predate the invite service carry the underscore field names verbatim and validate
        # just as well (see the config-model docstring). Reading only one spelling here
        # would leave exactly those files with the pair the model rejects.
        for entry in Project(project_data).get(base) or []:
            if not isinstance(entry, dict):
                continue
            if not (entry.get("application-target") or entry.get("application_target")):
                continue
            for stale in ("application-url", "application_url"):
                entry.pop(stale, None)

    def validate_project_change(self, previous: dict[str, Any], project_data: dict[str, Any]) -> list[str]:
        """A NEW invite key is at least ``INVITE_KEY_MIN_LENGTH`` characters long.

        New only: a key that was already in the previous version stays valid. Project files
        older than this service carry self-chosen keys, and rejecting those would make their
        file unsaveable while the owner cannot comply (lengthening the key invalidates the
        link already going round). The same pitfall as the storage ceiling, see
        ``STORED_CONTEXT_KEY``.

        The key itself does NOT appear in the message: that goes to a user, and a value
        somebody pasted from somewhere should not be quoted back.
        """
        stored = _invite_keys(previous)
        too_short = sum(
            1 for key in _invite_keys(project_data) if key not in stored and len(key) < INVITE_KEY_MIN_LENGTH
        )
        if not too_short:
            return []
        return [
            f"een nieuwe uitnodigingssleutel moet minstens {INVITE_KEY_MIN_LENGTH} tekens lang zijn "
            f"({too_short} te kort). Laat de sleutel leeg om er een veilige te laten genereren"
        ]

    def validate_project(self, project_data: dict[str, Any]) -> list[str]:
        """No invitation with a local account in a realm that links on email address.

        Such a realm links an SSO identity to every account with the same address, so an
        account created here with a hand-typed address takes over the identity of whoever
        supplies that address over SSO. The invite path already refuses ``local`` there
        (``get_invite_auth_methods``); this keeps the choice out of the project file too, so
        the file does not promise what the link does not do.

        Read through the handler, the same route the invite path takes: besides the service
        location it also knows the old ``invites:`` section and yields one spelling.
        """
        from opi.services.catalog.keycloak.account_link import realm_account_link_mode
        from opi.services.project_file_handler import ProjectFileHandler

        mode = realm_account_link_mode(project_data)
        if mode is None:
            return []

        # The key is NOT in the message: this passes a logger.warning on the write paths as a
        # ProjectIntegrityError, and the key is a bearer secret.
        return [
            "een uitnodiging staat een lokaal account toe, maar het "
            f"keycloak-realm van dit project koppelt automatisch op e-mailadres "
            f"(account-link: {mode}); lokale accounts zijn daar niet toegestaan"
            for invite in ProjectFileHandler().get_all_active_invites(project_data)
            if "local" in (invite.get("auth_methods") or [])
        ]

    def config_api_fields(self, layer: ConfigLayer) -> list[str]:
        return self.config_model_field_names() if layer is ConfigLayer.PROJECT else []

    def config_editables(self, layer: ConfigLayer) -> list[Editable]:
        if layer is not ConfigLayer.PROJECT:
            return []
        from opi.services.catalog.invite.editables import INVITE_EDITABLES

        return INVITE_EDITABLES

    def config_form_section(self, layer: ConfigLayer) -> FormSection | None:
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
                        description="Elke uitnodiging krijgt een eigen link.",
                        children=[Sequence(field_name=cp("active"))],
                    ),
                ],
            )
            self._config_section_cache = cached
        return cached

    @on(UIEvent.PROJECT_SECTIONS)
    def invites_block(self, ctx: ProjectPageContext) -> list[DetailPageSection]:
        # The link is the secret, so this is an authorization choice, not a display one:
        # the same rule as the project key, read from the register.
        if not ctx.can(Action.PROJECT_KEY_READ):
            return []
        from opi.services.project_file_handler import ProjectFileHandler

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
