"""Tests for the invite service (RC-13).

Covers the config model, the generated/self-chosen key, the key validator, the
cross-project uniqueness enforcer, the realm-role options provider, the detail-page block,
the both-locations read path, and the redemption role-not-assigned detection.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.api.invite_routes import _realm_roles_unassigned
from opi.forms.editables.enforcers import FieldError, UniqueInviteKeyEnforcer
from opi.forms.editables.validators import InviteKeyValidator
from opi.forms.visualizers.providers import InviteRealmRoleOptionsProvider
from opi.handlers.project_file_handler import ProjectFileHandler
from opi.services.catalog.base import ConfigLayer, ProjectPageContext
from opi.services.catalog.invite import InviteService
from opi.services.catalog.invite.config_model import InviteConfig
from opi.services.services_enums import ServiceType, UIEvent
from pydantic import ValidationError


def _production_invite() -> dict[str, Any]:
    """One invite in the exact shape of the four production files (underscore keys)."""
    return {
        "key": "invulhulpen",
        "realm_roles": ["allowed-user"],
        "application_url": "https://app.example.nl",
        "contact_email": "help@example.nl",
        "message": {"nl": "Welkom", "en": "Welcome"},
        "success_title": {"nl": "Account aangemaakt", "en": "Account created"},
        "success_button": {"nl": "Ga naar applicatie", "en": "Go to application"},
    }


def _invite_service_project(active: list[dict[str, Any]], *, keycloak: bool = True) -> dict[str, Any]:
    services: list[Any] = [{"name": "invite", "config": {"active": active}}]
    if keycloak:
        services.insert(0, "keycloak")
    return {"name": "proj", "services": services}


# ---------------------------------------------------------------------------
# Config model (task 2)
# ---------------------------------------------------------------------------


class TestInviteConfigModel:
    @pytest.mark.parametrize(
        "key", ["invulhulpen", "welcome-to-desa-portfolio", "welcome-to-docs", "welcome-to-openproject"]
    )
    def test_validates_the_four_production_configs(self, key: str) -> None:
        invite = _production_invite()
        invite["key"] = key
        config = InviteConfig.model_validate({"default-language": "nl", "active": [invite]})
        assert config.active[0].key == key
        assert config.active[0].realm_roles == ["allowed-user"]

    def test_accepts_both_hyphen_and_underscore_keys(self) -> None:
        hyphen = InviteConfig.model_validate(
            {"active": [{"key": "a", "realm-roles": ["r"], "contact-email": "a@b.nl"}]}
        )
        underscore = InviteConfig.model_validate(
            {"active": [{"key": "a", "realm_roles": ["r"], "contact_email": "a@b.nl"}]}
        )
        assert hyphen.active[0].realm_roles == underscore.active[0].realm_roles == ["r"]
        assert hyphen.active[0].contact_email == underscore.active[0].contact_email == "a@b.nl"

    def test_rejects_a_stray_key(self) -> None:
        with pytest.raises(ValidationError):
            InviteConfig.model_validate({"active": [{"key": "a", "bogus": 1}]})

    def test_rejects_unknown_auth_method(self) -> None:
        with pytest.raises(ValidationError):
            InviteConfig.model_validate({"active": [{"key": "a", "auth-methods": ["carrier-pigeon"]}]})

    def test_dump_by_alias_is_hyphenated(self) -> None:
        config = InviteConfig.model_validate({"active": [{"key": "a", "realm_roles": ["r"]}]})
        dumped = config.model_dump(by_alias=True, exclude_unset=True)
        assert dumped["active"][0] == {"key": "a", "realm-roles": ["r"]}


# ---------------------------------------------------------------------------
# Key generation + validation (tasks 7, 8)
# ---------------------------------------------------------------------------


class TestKeyGeneration:
    def test_empty_key_is_generated_self_chosen_is_kept(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # token_urlsafe can start with '-'/'_', which the validator rejects; force that case
        # first so the test deterministically exercises the regenerate-until-alphanumeric path.
        tokens = iter(["-leadinghyphen1234567", "AbcdEfghIjklMnopQrst12"])
        monkeypatch.setattr("opi.services.catalog.invite.secrets.token_urlsafe", lambda _n: next(tokens))
        project = _invite_service_project([{"realm-roles": ["allowed-user"]}, {"key": "mijn-sleutel"}])
        InviteService()._generate_missing_keys(project, {})
        active = project["services"][-1]["config"]["active"]
        assert active[1]["key"] == "mijn-sleutel"  # self-chosen untouched
        generated = active[0]["key"]
        assert generated[0].isalnum()  # never starts with '-' or '_'
        assert InviteKeyValidator().validate(generated) == []

    def test_de_api_weg_genereert_de_sleutel_ook(self) -> None:
        """Buiten het formulier om gebeurde dit niet, en dat was de kern van de klacht.

        Een uitnodiging die via de API werd aangemaakt met een lege sleutel hield die lege
        string, en haar link was letterlijk ``/invite/``. Nu loopt de catalogusrondgang
        (die elke API-schrijfweg draait) door dezelfde generator.
        """
        from opi.services.registry import generate_missing_values

        project = _invite_service_project([{"key": "", "realm-roles": ["allowed-user"]}])

        generated = generate_missing_values(project)

        sleutel = project["services"][-1]["config"]["active"][0]["key"]
        assert sleutel, "een lege sleutel hoort ingevuld te worden"
        assert InviteKeyValidator().validate(sleutel) == []
        # En de aanroeper hoort te horen WAT er ingevuld is: zonder die waarde heeft hij
        # een uitnodiging die hij niet kan versturen.
        assert generated == {"services/invite/config/active[0]/key": sleutel}

    def test_een_zelfgekozen_sleutel_levert_niets_op_om_te_melden(self) -> None:
        from opi.services.registry import generate_missing_values

        project = _invite_service_project([{"key": "mijn-sleutel"}])

        assert generate_missing_values(project) == {}
        assert project["services"][-1]["config"]["active"][0]["key"] == "mijn-sleutel"

    def test_generated_key_always_passes_its_own_validator(self) -> None:
        """A generated key must never start with '-'/'_': token_urlsafe (base64url) can,
        and the validator (start with a letter or digit) would reject it. Regression for
        a ~3%-of-the-time invalid key. Many iterations to exercise the random path."""
        from opi.services.catalog.invite import _generate_invite_key

        for _ in range(500):
            key = _generate_invite_key()
            assert len(key) == 22
            assert key[0].isalnum()
            assert InviteKeyValidator().validate(key) == []


class TestDeSchrijfwegMeldtDeGegenereerdeCode:
    """``configure_service`` moet de code teruggeven die het zelf verzon.

    Anders staat er wel een geldige uitnodiging in het bestand, maar heeft de aanroeper
    hem niet en kan hij hem dus niet versturen -- precies de klacht die dit oplost.
    """

    def _manager(self) -> Any:
        with (
            patch("opi.manager.project_manager.KubectlConnector"),
            patch("opi.handlers.sops.SopsHandler"),
            patch("opi.generation.manifests.ManifestGenerator"),
            patch("opi.manager.argo_manager.ArgoManager", return_value=MagicMock()),
            patch("opi.manager.bootstrap_manager.BootstrapManager", return_value=MagicMock()),
            patch("opi.manager.delete_project_manager.DeleteProjectManager", return_value=MagicMock()),
            patch("opi.manager.keycloak_manager.KeycloakManager", return_value=MagicMock()),
            patch("opi.manager.minio_manager.MinioManager", return_value=MagicMock()),
            patch("opi.manager.redis_manager.RedisManager", return_value=MagicMock()),
            patch("opi.manager.pvc_manager.PVCManager", return_value=MagicMock()),
        ):
            from opi.manager.project_manager import ProjectManager

            return ProjectManager()

    async def test_een_lege_sleutel_wordt_gevuld_en_teruggemeld(self) -> None:
        pm = self._manager()
        project_data = {"name": "proj", "services": ["keycloak"], "components": [], "deployments": []}
        pm.get_contents = AsyncMock(return_value=project_data)
        pm.get_name = AsyncMock(return_value="proj")
        pm.save_and_commit_project = AsyncMock()

        result = await pm.configure_service(
            ServiceType.INVITE.value, "project", {"active": [{"key": "", "realm-roles": ["allowed-user"]}]}
        )

        assert result["success"] is True
        opgeslagen = ProjectFileHandler().extract_invites_config(project_data)["active"][0]["key"]
        assert opgeslagen, "de opslag hoort een echte sleutel te hebben"
        assert result["generated"] == {"services/invite/config/active[0]/key": opgeslagen}

    async def test_een_zelfgekozen_sleutel_meldt_niets(self) -> None:
        pm = self._manager()
        project_data = {"name": "proj", "services": ["keycloak"], "components": [], "deployments": []}
        pm.get_contents = AsyncMock(return_value=project_data)
        pm.get_name = AsyncMock(return_value="proj")
        pm.save_and_commit_project = AsyncMock()

        result = await pm.configure_service(ServiceType.INVITE.value, "project", {"active": [{"key": "zelf-gekozen"}]})

        assert result["generated"] == {}


class TestInviteKeyValidator:
    def test_empty_is_allowed(self) -> None:
        assert InviteKeyValidator().validate("") == []

    @pytest.mark.parametrize("key", ["ab", "a" * 65, "has space", "has/slash", "-leadinghyphen", "has%pct"])
    def test_rejects_malformed(self, key: str) -> None:
        assert InviteKeyValidator().validate(key) != []

    @pytest.mark.parametrize("key", ["invulhulpen", "welcome-to-docs", "abc", "a1_b-2", "Ab3_x-Y9zz"])
    def test_accepts_valid(self, key: str) -> None:
        assert InviteKeyValidator().validate(key) == []


# ---------------------------------------------------------------------------
# Cross-project uniqueness (task 8)
# ---------------------------------------------------------------------------


class _FakeStore:
    def __init__(self, projects: list[Any]) -> None:
        self._projects = projects

    def get_all(self) -> list[Any]:
        return self._projects


def _summary(name: str, data: dict[str, Any]) -> Any:
    return SimpleNamespace(name=name, data=data)


class TestUniqueInviteKeyEnforcer:
    async def test_collision_with_other_project_raises_on_the_key_field(self, monkeypatch: Any) -> None:
        other = _summary("other", _invite_service_project([{"key": "shared"}]))
        monkeypatch.setattr("opi.services.project_store.get_project_store", lambda: _FakeStore([other]))
        data = _invite_service_project([{"key": "shared"}])
        with pytest.raises(FieldError) as exc:
            await UniqueInviteKeyEnforcer().enforce(data, {"project_name": "proj"})
        assert exc.value.field_path == "services/invite/config/active[0]/key"
        assert "other" not in str(exc.value)  # must not leak the other project's name

    async def test_own_project_is_skipped(self, monkeypatch: Any) -> None:
        mine = _summary("proj", _invite_service_project([{"key": "shared"}]))
        monkeypatch.setattr("opi.services.project_store.get_project_store", lambda: _FakeStore([mine]))
        data = _invite_service_project([{"key": "shared"}])
        assert await UniqueInviteKeyEnforcer().enforce(data, {"project_name": "proj"}) is data

    async def test_within_form_duplicate_raises(self, monkeypatch: Any) -> None:
        monkeypatch.setattr("opi.services.project_store.get_project_store", lambda: _FakeStore([]))
        data = _invite_service_project([{"key": "dup"}, {"key": "dup"}])
        with pytest.raises(FieldError) as exc:
            await UniqueInviteKeyEnforcer().enforce(data, {"project_name": None})
        assert exc.value.field_path == "services/invite/config/active[1]/key"


# ---------------------------------------------------------------------------
# Realm-role options provider (task 6)
# ---------------------------------------------------------------------------


def _keycloak_project(*, realm_roles: list[str] | None = None, wall_role: str | None = None) -> dict[str, Any]:
    kc_config: dict[str, Any] = {}
    if realm_roles is not None:
        kc_config["realm-roles"] = [{"name": r} for r in realm_roles]
    if wall_role is not None:
        kc_config["restrict-access"] = {"realm-role": wall_role}
    return {"services": [{"name": "keycloak", "config": kc_config}]}


class TestInviteRealmRoleOptionsProvider:
    def test_reads_custom_realm_roles(self) -> None:
        opts = InviteRealmRoleOptionsProvider(_keycloak_project(realm_roles=["editor", "viewer"])).get_options()
        values = [o["value"] for o in opts]
        assert values == ["", "editor", "viewer"]

    def test_reads_authorization_wall_role(self) -> None:
        opts = InviteRealmRoleOptionsProvider(_keycloak_project(wall_role="allowed-user")).get_options()
        assert [o["value"] for o in opts] == ["", "allowed-user"]

    def test_no_keycloak_gives_only_the_no_role_option(self) -> None:
        opts = InviteRealmRoleOptionsProvider({}).get_options()
        assert opts == [{"value": "", "label": "Geen rol toekennen"}]

    def test_stored_but_unknown_value_is_kept_and_flagged(self) -> None:
        opts = InviteRealmRoleOptionsProvider(
            _keycloak_project(wall_role="allowed-user"), current_value="removed"
        ).get_options()
        removed = [o for o in opts if o["value"] == "removed"]
        assert removed
        assert "bestaat niet meer" in removed[0]["label"]


# ---------------------------------------------------------------------------
# Detail-page block (task 5)
# ---------------------------------------------------------------------------


class TestDetailPageSections:
    @staticmethod
    def _sections(project: dict[str, Any], user_role: str) -> list:
        return InviteService().handle_ui(
            UIEvent.PROJECT_SECTIONS, ProjectPageContext(project_data=project, user_role=user_role)
        )

    def test_admin_sees_the_block(self) -> None:
        project = _invite_service_project([_production_invite_config_entry()])
        sections = self._sections(project, "admin")
        assert len(sections) == 1
        assert sections[0].template == "invite/section-detail.html.j2"
        assert sections[0].context["invites"][0]["key"] == "invulhulpen"
        assert "allowed-user" in sections[0].context["invites"][0]["realm_roles"]

    def test_developer_sees_nothing(self) -> None:
        project = _invite_service_project([_production_invite_config_entry()])
        assert self._sections(project, "developer") == []

    def test_no_invites_gives_nothing(self) -> None:
        assert self._sections(_invite_service_project([]), "admin") == []


def _production_invite_config_entry() -> dict[str, Any]:
    """The production invite in on-disk (hyphen) shape, as it lives under the service config."""
    return {
        "key": "invulhulpen",
        "realm-roles": ["allowed-user"],
        "contact-email": "help@example.nl",
        "application-url": "https://app.example.nl",
    }


# ---------------------------------------------------------------------------
# Both-locations read (task 3)
# ---------------------------------------------------------------------------


class TestExtractInvitesConfig:
    def test_reads_new_service_location(self) -> None:
        project = _invite_service_project([_production_invite_config_entry()])
        invite = ProjectFileHandler().get_invite_by_key(project, "invulhulpen")
        assert invite is not None
        assert invite["realm_roles"] == ["allowed-user"]  # normalized to underscore
        assert invite["contact_email"] == "help@example.nl"

    def test_reads_legacy_top_level_location(self) -> None:
        project = {
            "name": "legacy",
            "invites": {"settings": {"default_language": "en"}, "active": [_production_invite()]},
        }
        handler = ProjectFileHandler()
        invite = handler.get_invite_by_key(project, "invulhulpen")
        assert invite is not None
        assert invite["realm_roles"] == ["allowed-user"]
        assert handler.get_invite_settings(project) == {"default_language": "en"}

    def test_both_locations_yield_the_same_invite(self) -> None:
        legacy = {"name": "l", "invites": {"active": [_production_invite()]}}
        new = _invite_service_project([_production_invite_config_entry()])
        handler = ProjectFileHandler()
        assert (
            handler.get_invite_by_key(legacy, "invulhulpen")["realm_roles"]
            == (handler.get_invite_by_key(new, "invulhulpen")["realm_roles"])
        )


# ---------------------------------------------------------------------------
# Redemption role-not-assigned detection (task 6 / decision 11)
# ---------------------------------------------------------------------------


class TestRealmRolesUnassigned:
    def test_true_when_named_role_not_found(self) -> None:
        assert _realm_roles_unassigned({"roles": [], "errors": ["Realm roles not found: ['editor']"]}) is True

    def test_false_when_no_errors(self) -> None:
        assert _realm_roles_unassigned({"roles": ["allowed-user"]}) is False

    def test_false_for_a_deliberately_role_less_invite(self) -> None:
        # A role-less invite never attempts an assignment, so it never records this error.
        assert _realm_roles_unassigned({"roles": [], "client_roles": {}, "groups": []}) is False


# ---------------------------------------------------------------------------
# Registration completeness (task 1)
# ---------------------------------------------------------------------------


def test_service_is_registered_and_selectable() -> None:
    from opi.services.registry import get_service

    service = get_service(ServiceType.INVITE)
    assert isinstance(service, InviteService)
    assert service.definition.name == "Uitnodiging"
    assert "services/keycloak" in service.definition.requires
    assert service.definition.hidden is False
    assert service.config_form_section(ConfigLayer.PROJECT).section_id == "invite-config"
