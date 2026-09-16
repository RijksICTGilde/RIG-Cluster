"""
Tests for opi.manager.invite_manager module.

Tests invite validation, email domain checks, password validation, and language detection.
"""

from typing import ClassVar
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from keycloak.exceptions import KeycloakError
from opi.manager.invite_manager import (
    InviteDomainError,
    InviteError,
    InviteManager,
)


class TestValidateEmailDomain:
    """Tests for InviteManager.validate_email_domain."""

    def _make_manager(self):
        return InviteManager(project_file_handler=MagicMock())

    def test_no_domain_restriction(self):
        """No restrict_domain means all emails are valid."""
        manager = self._make_manager()
        assert manager.validate_email_domain("user@anything.com", {}) is True

    def test_matching_domain(self):
        """Email matching the domain passes."""
        manager = self._make_manager()
        assert manager.validate_email_domain("user@example.com", {"restrict_domain": "example.com"}) is True

    def test_domain_with_at_prefix(self):
        """Domain with @ prefix should work."""
        manager = self._make_manager()
        assert manager.validate_email_domain("user@example.com", {"restrict_domain": "@example.com"}) is True

    def test_non_matching_domain_raises(self):
        """Non-matching domain raises InviteDomainError."""
        manager = self._make_manager()
        with pytest.raises(InviteDomainError):
            manager.validate_email_domain("user@other.com", {"restrict_domain": "example.com"})

    def test_case_insensitive_domain(self):
        """Domain matching should be case-insensitive."""
        manager = self._make_manager()
        assert manager.validate_email_domain("user@Example.COM", {"restrict_domain": "example.com"}) is True


class TestValidatePassword:
    """Tests for InviteManager._validate_password."""

    def _make_manager(self):
        return InviteManager(project_file_handler=MagicMock())

    def test_valid_password(self):
        """Valid password should not raise."""
        manager = self._make_manager()
        manager._validate_password("StrongPass1234")

    def test_too_short_password(self):
        """Password under 12 characters raises error."""
        manager = self._make_manager()
        with pytest.raises(InviteError, match="at least 12 characters"):
            manager._validate_password("Short1A")

    def test_no_uppercase(self):
        """Password without uppercase raises error."""
        manager = self._make_manager()
        with pytest.raises(InviteError, match="uppercase"):
            manager._validate_password("alllowercase1234")

    def test_no_lowercase(self):
        """Password without lowercase raises error."""
        manager = self._make_manager()
        with pytest.raises(InviteError, match="lowercase"):
            manager._validate_password("ALLUPPERCASE1234")

    def test_no_digit(self):
        """Password without digit raises error."""
        manager = self._make_manager()
        with pytest.raises(InviteError, match="digit"):
            manager._validate_password("NoDigitsHereABC")


class TestDetectLanguage:
    """Tests for InviteManager.detect_language."""

    def _make_manager(self):
        return InviteManager(project_file_handler=MagicMock())

    def test_explicit_lang_parameter(self):
        """Explicit ?lang= parameter takes priority."""
        manager = self._make_manager()
        assert manager.detect_language("en", "nl", default="nl") == "en"

    def test_accept_language_header(self):
        """Accept-Language header is used when no explicit parameter."""
        manager = self._make_manager()
        assert manager.detect_language(None, "en-US,en;q=0.9,nl;q=0.8") == "en"

    def test_accept_language_dutch(self):
        """Accept-Language with Dutch first returns nl."""
        manager = self._make_manager()
        assert manager.detect_language(None, "nl-NL,nl;q=0.9,en;q=0.8") == "nl"

    def test_default_language(self):
        """Falls back to default when no other info."""
        manager = self._make_manager()
        assert manager.detect_language(None, None) == "nl"


class TestCompleteLocalInviteVerificatiemail:
    """De bevestigingsmail hoort METEEN te komen, niet pas bij de eerste login (RC-191).

    De gebruiker geeft zijn adres en wachtwoord op en leest op de volgende pagina dat hij
    moet bevestigen. Kwam de mail pas bij zijn eerste login, dan klopt die pagina niet.
    """

    FORM: ClassVar[dict[str, str]] = {
        "email": "iemand@example.org",
        "first_name": "Ie",
        "last_name": "Mand",
        "password": "GeheimGeheim1",
    }

    def _keycloak(self) -> AsyncMock:
        keycloak = AsyncMock()
        keycloak.get_user_by_email.return_value = None
        keycloak.get_user_by_username.return_value = None
        keycloak.create_user.return_value = {"id": "u1"}
        return keycloak

    async def _complete(self, keycloak: AsyncMock) -> dict:
        manager = InviteManager(project_file_handler=MagicMock())
        manager.assign_invite_permissions = AsyncMock(return_value={"roles": [], "errors": []})
        with patch("opi.manager.invite_manager.create_keycloak_connector", AsyncMock(return_value=keycloak)):
            return await manager.complete_local_invite(
                project_data={},
                project_name="demo",
                invite={},
                form_data=dict(self.FORM),
                realm_name="rig-demo",
            )

    @pytest.mark.asyncio
    async def test_de_mail_wordt_gevraagd_voor_de_zojuist_aangemaakte_gebruiker(self):
        keycloak = self._keycloak()

        result = await self._complete(keycloak)

        keycloak.send_verify_email.assert_awaited_once_with("rig-demo", "u1")
        assert result["verification_mail_sent"] is True

    @pytest.mark.asyncio
    async def test_een_mislukte_mail_breekt_de_registratie_niet(self):
        """Het account draagt de required action, dus Keycloak probeert het bij de eerste
        login alsnog. De succespagina moet het wel kunnen vertellen."""
        keycloak = self._keycloak()
        keycloak.send_verify_email.side_effect = KeycloakError("relay weg")

        result = await self._complete(keycloak)

        assert result["user_id"] == "u1"
        assert result["created"] is True
        assert result["verification_mail_sent"] is False
        keycloak.assign_realm_roles_to_user.assert_not_called()

    @pytest.mark.asyncio
    async def test_de_rechten_worden_ook_toegekend_als_de_mail_faalt(self):
        keycloak = self._keycloak()
        keycloak.send_verify_email.side_effect = KeycloakError("relay weg")
        manager = InviteManager(project_file_handler=MagicMock())
        manager.assign_invite_permissions = AsyncMock(return_value={"roles": ["lezer"], "errors": []})

        with patch("opi.manager.invite_manager.create_keycloak_connector", AsyncMock(return_value=keycloak)):
            result = await manager.complete_local_invite(
                project_data={},
                project_name="demo",
                invite={},
                form_data=dict(self.FORM),
                realm_name="rig-demo",
            )

        assert result["assigned"] == {"roles": ["lezer"], "errors": []}
