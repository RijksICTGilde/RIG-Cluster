"""The localized texts of an invite, as the invite pages read them."""

import pytest
from opi.handlers.project_file_handler import ProjectFileHandler


@pytest.fixture
def handler() -> ProjectFileHandler:
    return ProjectFileHandler()


class TestMessage:
    def test_picks_the_requested_language(self, handler: ProjectFileHandler) -> None:
        invite = {"message": {"nl": "Welkom", "en": "Welcome"}}
        assert handler.get_invite_message(invite, "en") == "Welcome"

    def test_missing_language_falls_back_to_dutch(self, handler: ProjectFileHandler) -> None:
        invite = {"message": {"nl": "Welkom"}}
        assert handler.get_invite_message(invite, "en") == "Welkom"

    def test_a_plain_string_is_used_as_is(self, handler: ProjectFileHandler) -> None:
        assert handler.get_invite_message({"message": "Hallo"}, "en") == "Hallo"

    def test_absent_is_empty(self, handler: ProjectFileHandler) -> None:
        assert handler.get_invite_message({}, "nl") == ""


class TestSuccessTexts:
    def test_absent_title_gets_the_default(self, handler: ProjectFileHandler) -> None:
        assert handler.get_invite_success_title({}, "en") == "Account created"

    def test_set_title_wins_over_the_default(self, handler: ProjectFileHandler) -> None:
        invite = {"success_title": {"nl": "Gelukt"}}
        assert handler.get_invite_success_title(invite, "nl") == "Gelukt"

    def test_absent_button_gets_the_default(self, handler: ProjectFileHandler) -> None:
        assert handler.get_invite_success_button({}, "nl") == "Ga naar applicatie"
