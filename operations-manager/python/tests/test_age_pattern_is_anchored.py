"""``AGE_ENCRYPTED_PATTERN`` matches a whole value, not a marker somewhere inside it.

Unanchored, a field whose only rule is "an AGE marker appears in it" accepts a value that
carries plaintext next to the block. Every reader then treats the whole string as the
secret, writes it to git as it is, and hands age something it cannot open.

The pattern feeds two engines with different defaults (pydantic's rust-regex and the
JSON-schema validator's ``re``), so each case is asserted on both: a rule that is strict in
only one of them is a hole in the other.
"""

from __future__ import annotations

import json
import time
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from opi.services.catalog.keycloak.config_model import KeycloakRealm
from opi.services.project_schema import SCHEMA_PATH
from opi.utils.age import AGE_BLOCK_BEGIN, AGE_BLOCK_END, encrypt_age_content_sync, generate_age_key_pair

BLOCK = f"{AGE_BLOCK_BEGIN}\nWU9VUiBTRUNSRVQ=\n{AGE_BLOCK_END}"
B64 = "base64+age:WU9VUiBTRUNSRVQ="
REAL_BLOCK = encrypt_age_content_sync("hunter2", generate_age_key_pair()[1])

ACCEPTED = {
    "an armored block": BLOCK,
    "an armored block with the trailing newline a block scalar leaves": f"{BLOCK}\n",
    "the one-line prefixed form": B64,
    "a block straight out of encrypt_age_content": REAL_BLOCK,
}

REFUSED = {
    "text before the block": f"zie hieronder\n{BLOCK}",
    "text after the block": f"{BLOCK}\nwachtwoord: hunter2",
    "a block without its END marker": f"{AGE_BLOCK_BEGIN}\nWU9VUiBTRUNSRVQ=\n",
    "a block without its BEGIN marker": f"WU9VUiBTRUNSRVQ=\n{AGE_BLOCK_END}",
    "plaintext between the markers": f"{AGE_BLOCK_BEGIN}\nwachtwoord: hunter2!\n{AGE_BLOCK_END}",
    "a second END marker with plaintext in front of it": (
        f"{AGE_BLOCK_BEGIN}\nWU9VUiBTRUNSRVQ=\n{AGE_BLOCK_END}\nwachtwoord: hunter2!\n{AGE_BLOCK_END}"
    ),
    "the prefixed form with a space in it": "base64+age:WU9VUiBT RUNSRVQ=",
    "the prefixed form with a non-base64 character": "base64+age:WU9VUiBT#RUNSRVQ=",
    "the prefixed form with text in front": f"wachtwoord {B64}",
    "the prefixed form with nothing behind it": "base64+age:",
    "a plain-marked value": "plain:hunter2",
    "bare text": "hunter2",
}


def _schema_accepts(value: str) -> bool:
    """Whether ``$defs/age-encrypted`` of the project schema takes this value."""
    schema: dict[str, Any] = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = Draft202012Validator({"$ref": "#/$defs/age-encrypted", "$defs": schema["$defs"]})
    return not list(validator.iter_errors(value))


def _model_accepts(value: str) -> bool:
    """Whether the pydantic side takes this value, via a field that carries the pattern."""
    try:
        KeycloakRealm(host="https://kc", realm="p-c", username="admin", password=value)
    except ValueError:
        return False
    return True


@pytest.mark.parametrize("value", ACCEPTED.values(), ids=list(ACCEPTED))
def test_a_stored_form_is_accepted(value: str) -> None:
    assert _model_accepts(value)
    assert _schema_accepts(value)


@pytest.mark.parametrize("value", REFUSED.values(), ids=list(REFUSED))
def test_anything_that_is_not_a_whole_stored_form_is_refused(value: str) -> None:
    assert not _model_accepts(value)
    assert not _schema_accepts(value)


def test_a_long_refused_value_does_not_make_the_schema_engine_backtrack() -> None:
    """The pattern stays linear on ``re``, which the JSON-schema side runs on.

    Two overlapping quantifiers around the block body (``\\s+`` in front of a class that
    already holds whitespace) turn a refusal into quadratic backtracking: 80k characters
    took 15 seconds, enough to hold the event loop on one value.
    """
    value = f"{AGE_BLOCK_BEGIN}{' ' * 80_000}"

    started = time.perf_counter()
    accepted = _schema_accepts(value)
    elapsed = time.perf_counter() - started

    assert not accepted
    assert elapsed < 1.0, f"refusing {len(value)} characters took {elapsed:.1f}s"
