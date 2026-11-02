"""redact_sensitive_headers must keep credentials out of logs.

Regression guard for the ArgoCD connector leaking a live admin Bearer JWT via
`logger.debug(f"Request headers: {headers}")`.
"""

import logging

from opi.services.project_leden import TOKEN_PREFIX, nieuw_token
from opi.utils.logging_redact import (
    SecretRedactionFilter,
    redact_sensitive_headers,
    redact_structure,
    redact_text,
)


def test_masks_bearer_token_value() -> None:
    token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.secret.signature"
    out = redact_sensitive_headers({"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    assert token not in str(out)
    assert out["Authorization"] == "***REDACTED***"
    # Non-sensitive headers pass through so the log still shows request shape.
    assert out["Content-Type"] == "application/json"


def test_case_insensitive_and_multiple_sensitive_headers() -> None:
    out = redact_sensitive_headers(
        {
            "authorization": "Basic abc123",
            "X-API-Key": "supersecret",
            "Cookie": "session=deadbeef",
            "Accept": "application/json",
        }
    )
    assert out["authorization"] == "***REDACTED***"
    assert out["X-API-Key"] == "***REDACTED***"
    assert out["Cookie"] == "***REDACTED***"
    assert out["Accept"] == "application/json"
    # No secret value survives anywhere in the output.
    for leaked in ("abc123", "supersecret", "deadbeef"):
        assert leaked not in str(out)


def test_does_not_mutate_input() -> None:
    headers = {"Authorization": "Bearer x"}
    redact_sensitive_headers(headers)
    assert headers == {"Authorization": "Bearer x"}  # original untouched


def test_redact_text_maskeert_een_geheim_in_vrije_tekst() -> None:
    # Los aanroepbaar voor tekst die niet via een logregel gaat: het error_message van een
    # async taak gaat naar de database en naar de client.
    uit = redact_text('Kan niet verbinden: postgresql://u@h/db?password=geheim en {"password": "geheim"}')
    assert "geheim" not in uit
    assert uit.count("***REDACTED***") == 2


def test_redact_text_laat_een_fouttekst_zonder_waarde_heel() -> None:
    tekst = "Failed to decrypt registry password: invalid key"
    assert redact_text(tekst) == tekst


def test_het_filter_laat_een_record_met_niet_passende_args_door() -> None:
    # record.getMessage() raises wanneer het bericht meer placeholders heeft dan args:
    # een fout in de aanroep, niet in het filter. Het logverkeer mag daar niet op afbreken.
    record = logging.LogRecord(
        name="opi.x", level=logging.INFO, pathname=__file__, lineno=1, msg="%s en %s", args=("een",), exc_info=None
    )
    assert SecretRedactionFilter().filter(record) is True


def test_het_filter_maskeert_stack_info() -> None:
    # stack_info komt los van exc_info en gaat door dezelfde formatter naar de log.
    record = logging.LogRecord(
        name="opi.x", level=logging.INFO, pathname=__file__, lineno=1, msg="een regel", args=None, exc_info=None
    )
    record.stack_info = "  File \"x.py\", line 1\n    connect(password='markerwachtwoord')"
    SecretRedactionFilter().filter(record)
    assert record.stack_info is not None
    assert "markerwachtwoord" not in record.stack_info


def test_het_filter_maskeert_een_al_geformatteerde_traceback() -> None:
    # Een tweede handler ziet exc_text al staan; die tekst moet ook gemaskeerd worden.
    record = logging.LogRecord(
        name="opi.x", level=logging.ERROR, pathname=__file__, lineno=1, msg="mislukt", args=None, exc_info=None
    )
    record.exc_text = "Traceback:\nRuntimeError: password='markerwachtwoord'"
    SecretRedactionFilter().filter(record)
    assert record.exc_text is not None
    assert "markerwachtwoord" not in record.exc_text


def test_redact_structure_maskeert_elke_tekst_in_een_dict() -> None:
    # Het result-veld van een taak: genest, met een lijst en met niet-tekstwaarden ernaast.
    resultaat = {
        "status": "failed",
        "error": "password=markerwachtwoord",
        "stappen": ["ok", "password=markerwachtwoord"],
        "detail": {"cmd": "psql password=markerwachtwoord"},
        "pogingen": 3,
        "klaar": None,
    }
    geredigeerd = redact_structure(resultaat)
    assert "markerwachtwoord" not in str(geredigeerd)
    assert geredigeerd["pogingen"] == 3
    assert geredigeerd["klaar"] is None
    assert geredigeerd["stappen"][0] == "ok"
    assert resultaat["error"] == "password=markerwachtwoord"


def test_een_api_token_valt_ook_buiten_een_header() -> None:
    """Het vangnet voor de volgende logregel die een ``zad_``-token meeneemt (RC-322).

    Het voorvoegsel maakt de vorm zelfstandig herkenbaar, dus dit werkt ook waar het token
    niet in een bekende credential-header staat.
    """
    token, _ = nieuw_token()

    assert token not in redact_text(f"Token {token} geweigerd voor project demo")
    assert token not in redact_text(f'{{"token": "{token}"}}')
    # De logregel houdt zijn vorm, zodat hij nog leesbaar is.
    assert "geweigerd voor project demo" in redact_text(f"Token {token} geweigerd voor project demo")


def test_het_voorvoegsel_van_het_patroon_is_dat_van_een_echt_token() -> None:
    """Verandert ``TOKEN_PREFIX``, dan maskeert het patroon niets meer."""
    assert nieuw_token()[0].startswith(TOKEN_PREFIX)
    assert redact_text(nieuw_token()[0]).startswith(TOKEN_PREFIX)
