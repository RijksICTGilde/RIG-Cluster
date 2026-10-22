"""Tests for opi.utils.age's project-KEK naad (RC-334).

De projectsleutel wordt niet meer rechtstreeks met de platformsleutel in- en uitgepakt,
maar via ``wrap_project_key``/``unwrap_project_key``: de KEK zodra die er is, anders de
platformsleutel. Dit bestand meet precies dat gedrag, los van elke aanroeper.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest
from opi.utils.age import (
    project_key_candidates,
    project_key_candidates_for,
    project_wrapping_public_key,
    unwrap_project_key,
    unwrap_project_key_sync,
    wrap_project_key,
    wrap_project_key_sync,
)

if TYPE_CHECKING:
    from collections.abc import Callable


@pytest.fixture
def two_keypairs(make_age_keypair: Callable[[], tuple[str, str]]) -> tuple[tuple[str, str], tuple[str, str]]:
    """A platform keypair and a KEK keypair, distinct so a mix-up is visible."""
    return make_age_keypair(), make_age_keypair()


class TestProjectKeyCandidatesFor:
    """project_key_candidates_for: the KEK first, the platform key second, missing ones dropped."""

    def test_both_present_the_kek_comes_first(self):
        candidates = project_key_candidates_for("PLATFORM-PRIVATE", "KEK-PRIVATE")
        assert candidates == [("project-kek", "KEK-PRIVATE"), ("platformsleutel", "PLATFORM-PRIVATE")]

    def test_without_a_kek_only_the_platform_key_remains(self):
        assert project_key_candidates_for("PLATFORM-PRIVATE", None) == [("platformsleutel", "PLATFORM-PRIVATE")]

    def test_without_a_platform_key_only_the_kek_remains(self):
        assert project_key_candidates_for(None, "KEK-PRIVATE") == [("project-kek", "KEK-PRIVATE")]

    def test_neither_key_is_an_empty_list(self):
        assert project_key_candidates_for(None, None) == []


class TestProjectKeyCandidates:
    """project_key_candidates reads settings.SOPS_AGE_PRIVATE_KEY and settings.PROJECT_KEK_PRIVATE_KEY."""

    def test_without_a_kek_setting_only_the_platform_key_is_a_candidate(self, monkeypatch):
        import opi.utils.age as age_module

        monkeypatch.setattr(age_module.settings, "SOPS_AGE_PRIVATE_KEY", "PLATFORM-PRIVATE", raising=False)
        monkeypatch.setattr(age_module.settings, "PROJECT_KEK_PRIVATE_KEY", None, raising=False)

        assert project_key_candidates() == [("platformsleutel", "PLATFORM-PRIVATE")]

    def test_with_a_kek_setting_it_is_tried_first(self, monkeypatch):
        import opi.utils.age as age_module

        monkeypatch.setattr(age_module.settings, "SOPS_AGE_PRIVATE_KEY", "PLATFORM-PRIVATE", raising=False)
        monkeypatch.setattr(age_module.settings, "PROJECT_KEK_PRIVATE_KEY", "KEK-PRIVATE", raising=False)

        assert project_key_candidates() == [("project-kek", "KEK-PRIVATE"), ("platformsleutel", "PLATFORM-PRIVATE")]


class TestProjectWrappingPublicKey:
    """project_wrapping_public_key: the KEK as soon as it is set, else the platform key."""

    def test_the_kek_public_key_wins_when_set(self, monkeypatch):
        import opi.utils.age as age_module

        monkeypatch.setattr(age_module.settings, "SOPS_AGE_PUBLIC_KEY", "age1platform", raising=False)
        monkeypatch.setattr(age_module.settings, "PROJECT_KEK_PUBLIC_KEY", "age1kek", raising=False)

        assert project_wrapping_public_key() == "age1kek"

    def test_falls_back_to_the_platform_public_key_without_a_kek(self, monkeypatch):
        import opi.utils.age as age_module

        monkeypatch.setattr(age_module.settings, "SOPS_AGE_PUBLIC_KEY", "age1platform", raising=False)
        monkeypatch.setattr(age_module.settings, "PROJECT_KEK_PUBLIC_KEY", None, raising=False)

        assert project_wrapping_public_key() == "age1platform"

    def test_raises_without_either_public_key(self, monkeypatch):
        import opi.utils.age as age_module

        monkeypatch.setattr(age_module.settings, "SOPS_AGE_PUBLIC_KEY", None, raising=False)
        monkeypatch.setattr(age_module.settings, "PROJECT_KEK_PUBLIC_KEY", None, raising=False)

        with pytest.raises(ValueError, match="No AGE public key"):
            project_wrapping_public_key()


class TestUnwrapProjectKey:
    """unwrap_project_key(_sync): tries every candidate key, the KEK first, until one opens it."""

    @pytest.mark.asyncio
    async def test_a_key_wrapped_with_the_kek_opens_with_the_kek_candidate(self, two_keypairs):
        (_platform_private, _platform_public), (kek_private, kek_public) = two_keypairs
        wrapped = await wrap_project_key("the-plaintext-project-key", kek_public)

        plaintext = await unwrap_project_key(wrapped, [("project-kek", kek_private)])

        assert plaintext == "the-plaintext-project-key"

    def test_falls_back_to_the_platform_key_when_the_kek_does_not_open_it(self, two_keypairs):
        """Existing project files are still on the platform key; the fallback keeps them readable."""
        (platform_private, platform_public), (kek_private, _kek_public) = two_keypairs
        wrapped = wrap_project_key_sync("the-plaintext-project-key", platform_public)

        plaintext = unwrap_project_key_sync(
            wrapped, [("project-kek", kek_private), ("platformsleutel", platform_private)]
        )

        assert plaintext == "the-plaintext-project-key"

    def test_de_terugval_logt_geen_fout(self, two_keypairs, caplog):
        """De stand waarin de overgang het langst staat mag geen ERROR per gelezen sleutel geven."""
        (platform_private, platform_public), (kek_private, _kek_public) = two_keypairs
        wrapped = wrap_project_key_sync("the-plaintext-project-key", platform_public)

        with caplog.at_level(logging.DEBUG, logger="opi.utils.age"):
            unwrap_project_key_sync(wrapped, [("project-kek", kek_private), ("platformsleutel", platform_private)])

        assert not [record for record in caplog.records if record.levelno >= logging.ERROR]

    def test_the_kek_is_tried_before_the_platform_key(self, two_keypairs):
        """A field wrapped with the KEK must not depend on the platform key being correct too."""
        (platform_private, _platform_public), (kek_private, kek_public) = two_keypairs
        wrapped = wrap_project_key_sync("the-plaintext-project-key", kek_public)

        # A deliberately wrong "platform" candidate: if it were tried first this would raise.
        plaintext = unwrap_project_key_sync(
            wrapped, [("project-kek", kek_private), ("platformsleutel", "AGE-SECRET-KEY-1WRONGWRONGWRONG")]
        )

        assert plaintext == "the-plaintext-project-key"

    def test_opening_with_neither_candidate_raises(self, two_keypairs):
        (_platform_private, platform_public), (kek_private, _kek_public) = two_keypairs
        wrapped = wrap_project_key_sync("the-plaintext-project-key", platform_public)

        with pytest.raises(ValueError, match="Could not unwrap"):
            unwrap_project_key_sync(
                wrapped, [("project-kek", kek_private), ("platformsleutel", "AGE-SECRET-KEY-1WRONGWRONGWRONG")]
            )

    def test_without_any_candidate_key_raises(self):
        with pytest.raises(ValueError, match="No AGE private key available"):
            unwrap_project_key_sync("AGE-SECRET-KEY-1WHATEVER" + "irrelevant", [])

    def test_a_missing_wrapped_value_raises(self, two_keypairs):
        (_platform_private, _platform_public), (kek_private, _kek_public) = two_keypairs

        with pytest.raises(ValueError, match="Missing age-private-key"):
            unwrap_project_key_sync("", [("project-kek", kek_private)])


class TestWrapProjectKey:
    """wrap_project_key(_sync): wraps for the KEK as soon as one is given, by explicit public key."""

    @pytest.mark.asyncio
    async def test_wraps_for_the_given_public_key(self, two_keypairs):
        (_platform_private, _platform_public), (kek_private, kek_public) = two_keypairs

        wrapped = await wrap_project_key("the-plaintext-project-key", kek_public)

        assert await unwrap_project_key(wrapped, [("project-kek", kek_private)]) == "the-plaintext-project-key"

    def test_uses_the_configured_wrapping_key_without_an_explicit_one(self, two_keypairs, monkeypatch):
        import opi.utils.age as age_module

        (platform_private, platform_public), (kek_private, kek_public) = two_keypairs
        monkeypatch.setattr(age_module.settings, "SOPS_AGE_PUBLIC_KEY", platform_public, raising=False)
        monkeypatch.setattr(age_module.settings, "PROJECT_KEK_PUBLIC_KEY", kek_public, raising=False)

        wrapped = wrap_project_key_sync("the-plaintext-project-key")

        # Wrapped for the KEK, not the platform key: the platform key must not open it.
        assert unwrap_project_key_sync(wrapped, [("project-kek", kek_private)]) == "the-plaintext-project-key"
        with pytest.raises(ValueError, match="Could not unwrap"):
            unwrap_project_key_sync(wrapped, [("platformsleutel", platform_private)])
