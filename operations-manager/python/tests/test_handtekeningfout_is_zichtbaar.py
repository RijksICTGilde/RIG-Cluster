"""Een geweigerde commit-handtekening moet in het portaal leesbaar zijn (RC-352).

ArgoCD verpakt dat in dezelfde ComparisonError als een renderfout. Zonder vertaling zou
het als "often two resources have the same name" lezen - een uitleg naar de manifesten,
terwijl de manifesten goed zijn. Deze test meet dat de handtekeningfout apart op de
statuskaart komt.

Meet de conditie letterlijk zoals ArgoCD die geeft (sandbox 19-10-2026).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from opi.services import event_interpreter
from opi.services.deployment_diagnostics import conditions_to_errors
from opi.services.event_interpreter import interpret_argocd_errors
from opi.utils.git_signing import GitSigningIdentity

if TYPE_CHECKING:
    import pytest

REVISIE = "72cf2eaac22f7195506e049a5472478c65aef0b6"

GEMETEN_STATUS = {
    "status": {
        "sync": {"status": "OutOfSync", "revision": REVISIE},
        "health": {"status": "Healthy"},
        "conditions": [
            {
                "type": "ComparisonError",
                "message": (
                    f"GIT/GPG: Failed verifying revision {REVISIE} by "
                    "'Robbert Uittenbroek <robbert.uittenbroek@rijksoverheid.nl>': unsigned (key_id=)"
                ),
            }
        ],
    }
}


def test_de_conditie_komt_als_fout_uit_de_status() -> None:
    """De statuskaart leest de condities los van health, en health staat hier op Healthy."""
    errors = conditions_to_errors(GEMETEN_STATUS)
    assert [error["resource"] for error in errors] == ["ComparisonError"]


def test_de_weigering_leest_als_handtekeningfout_en_niet_als_renderfout() -> None:
    errors = interpret_argocd_errors(conditions_to_errors(GEMETEN_STATUS))
    assert len(errors) == 1
    error = errors[0]
    assert error["resource"] == "Commit niet ondertekend"
    assert "dezelfde naam" not in error["suggestion"]
    assert error["severity"] == "actionable"


def test_de_melding_houdt_de_revisie_en_de_sleutel_vast() -> None:
    """Zonder de revisie is de melding niet te herleiden tot een commit, en ``key_id=``
    zonder waarde is het verschil tussen "niet ondertekend" en "met de verkeerde sleutel"."""
    error = interpret_argocd_errors(conditions_to_errors(GEMETEN_STATUS))[0]
    assert REVISIE in error["message"]
    assert "key_id=" in error["message"]


def test_een_verkeerde_sleutel_krijgt_dezelfde_uitleg() -> None:
    """Dezelfde conditie, maar met een handtekening van een sleutel die niet vertrouwd is."""
    errors = interpret_argocd_errors(
        [
            {
                "resource": "ComparisonError",
                "message": f"GIT/GPG: Failed verifying revision {REVISIE} by 'Iemand "
                "<iemand@example.com>': untrusted (key_id=ABCDEF0123456789)",
            }
        ]
    )
    assert errors[0]["resource"] == "Commit niet ondertekend"
    assert "ABCDEF0123456789" in errors[0]["message"]


def test_de_uitleg_noemt_de_sleutel_die_argocd_verwacht(monkeypatch: pytest.MonkeyPatch) -> None:
    """De key ID uit signatureKeys, zodat zichtbaar is met welke sleutel het wel had gemoeten."""
    monkeypatch.setattr(
        event_interpreter,
        "get_git_signing_identity",
        lambda: GitSigningIdentity(fingerprint="0" * 24 + "FEDCBA9876543210", gnupg_home="/tmp/leeg"),
    )
    error = interpret_argocd_errors(conditions_to_errors(GEMETEN_STATUS))[0]
    assert "FEDCBA9876543210" in error["suggestion"]


def test_zonder_signing_key_blijft_de_uitleg_staan(monkeypatch: pytest.MonkeyPatch) -> None:
    """Een cluster waar OPI niet ondertekent heeft geen sleutel om te noemen."""
    monkeypatch.setattr(event_interpreter, "get_git_signing_identity", lambda: None)
    error = interpret_argocd_errors(conditions_to_errors(GEMETEN_STATUS))[0]
    assert error["resource"] == "Commit niet ondertekend"
    assert "sleutel" in error["suggestion"]
