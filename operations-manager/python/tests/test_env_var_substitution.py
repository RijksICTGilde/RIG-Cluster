"""Variable resolution inside user-env-vars (Marc's request, 1 August).

OPI already had the substitution engine, but only aliases used it; user-env-vars had a
hand-rolled special case for PUBLIC_HOST and PUBLIC_HOSTNAME with a note to extend the
alias system if more were ever needed. These tests pin that extension, and above all the
property that makes it safe to apply to values a user wrote: a dollar in a password must
never break a deploy.
"""

from __future__ import annotations

import pytest
from opi.utils.env_vars import (
    extract_uppercase_references,
    substitute_known_variables,
    substitute_variables,
)

CONTEXT = {"PUBLIC_HOST": "app.example.nl", "PUBLIC_HOSTNAME": "app", "DATABASE_HOST": "db.svc"}


class TestUppercaseReferences:
    """Only the uppercase form OPI exposes counts as a reference."""

    @pytest.mark.parametrize("value", ["$wL7nQr4", "$k", "$x8cc5ls", "a$Bc", "$lower"])
    def test_password_noise_is_not_a_reference(self, value: str) -> None:
        """The three real values here come from production project files.

        regel-k4c's ADMIN_PASSWORD contains ``$wL7nQr4``, rijks-595's DJANGO_SECRET_KEY
        contains ``$k`` and ``$x8cc5ls``. Treating those as references is what would
        break a deploy over someone's password.
        """
        assert extract_uppercase_references(value) == []

    @pytest.mark.parametrize("value", ["$PUBLIC_HOST", "${PUBLIC_HOST}", "$DB_2", "${A_B}"])
    def test_uppercase_forms_are_references(self, value: str) -> None:
        assert extract_uppercase_references(value) != []


class TestSubstituteKnownVariables:
    def test_resolves_both_syntaxes(self) -> None:
        assert substitute_known_variables("https://$PUBLIC_HOST/x", CONTEXT) == "https://app.example.nl/x"
        assert substitute_known_variables("https://${PUBLIC_HOST}/x", CONTEXT) == "https://app.example.nl/x"

    def test_longer_name_is_not_eaten_by_the_shorter_one(self) -> None:
        # $PUBLIC_HOST is a prefix of $PUBLIC_HOSTNAME; a naive replace would corrupt it.
        assert substitute_known_variables("$PUBLIC_HOSTNAME", CONTEXT) == "app"

    def test_a_production_password_survives_untouched(self) -> None:
        password = "Xy$wL7nQr4Zq"
        assert substitute_known_variables(password, CONTEXT) == password

    def test_an_unknown_uppercase_reference_is_left_as_is(self) -> None:
        """Probably a typo, so it is warned about, but it must not fail the deploy."""
        assert substitute_known_variables("$NOPE/x", CONTEXT) == "$NOPE/x"

    def test_the_warning_never_prints_the_whole_reference(self, caplog) -> None:
        # The reference sits inside a value that may be a secret, so only a hint is logged.
        with caplog.at_level("WARNING"):
            substitute_known_variables("$SUPERSECRETNAME", CONTEXT)
        assert "SUPERSECRETNAME" not in caplog.text
        assert "$SUP..." in caplog.text

    def test_double_dollar_still_escapes(self) -> None:
        assert substitute_known_variables("Price: $$10", CONTEXT) == "Price: $10"

    def test_aliases_keep_the_strict_behaviour(self) -> None:
        """An alias author meant to reference something, so a miss stays an error."""
        with pytest.raises(ValueError, match="not found in context"):
            substitute_variables("$NOPE", CONTEXT)
