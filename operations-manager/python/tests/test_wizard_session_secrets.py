"""Encrypted project values must not travel through the disk-backed wizard session.

Regression: editing only the authorization-wall banner wrote the keycloak realm admin
password, the api key and the attachment blocks into the session JSON under TEMP_DIR,
and the untouched values then travelled back out of that copy into the saved project
file -- which is how a realm password lost its literal-block formatting.
"""

from __future__ import annotations

import json
import os
import time
from typing import TYPE_CHECKING

import pytest
from opi.forms.wizard import session
from opi.forms.wizard.secrets import (
    REDACTED,
    _leaf_key,
    reachable_leaf_keys,
    redact_unreachable_secrets,
    restore_redacted_secrets,
)

if TYPE_CHECKING:
    from pathlib import Path

AGE_BLOCK = "-----BEGIN AGE ENCRYPTED FILE-----\nYWdlLWVuY3J5cHRpb24ub3JnL3YxCi0+\n-----END AGE ENCRYPTED FILE-----"
OTHER_AGE_BLOCK = "-----BEGIN AGE ENCRYPTED FILE-----\nZm9vYmFyYmF6cXV1eA==\n-----END AGE ENCRYPTED FILE-----"


def _project() -> dict:
    """Shaped like the project file that exposed this: a keycloak realm with an admin
    password, an attachment block, and the two project-level keys."""
    return {
        "name": "tas-yz7",
        "config": {"age-private-key": AGE_BLOCK, "api-key": OTHER_AGE_BLOCK},
        "services": [
            {
                "name": "keycloak",
                "config": {"realms": [{"realm": "tas-yz7-sandboxed-local", "password": AGE_BLOCK}]},
            },
            {"name": "authorization-wall", "config": {"banner": "Alleen op uitnodiging"}},
        ],
        "components": [{"name": "frontend", "user-env-vars": OTHER_AGE_BLOCK}],
    }


class _Editable:
    def __init__(self, yaml_path: str) -> None:
        self.yaml_path = yaml_path


class _Visualizer:
    def __init__(self, yaml_path: str, children: list | None = None) -> None:
        self.editable = _Editable(yaml_path)
        self.children = children or []


class TestReachableLeafKeys:
    def test_strips_index_and_filter_syntax(self):
        assert _leaf_key("deployments[0]/components{reference=api}/user-env-vars") == "user-env-vars"
        assert _leaf_key("config/api-key") == "api-key"
        assert _leaf_key("users") == "users"

    def test_collects_children(self):
        editables = [_Visualizer("services/keycloak/config/template", [_Visualizer("components/user-env-vars")])]
        assert reachable_leaf_keys(editables) == {"template", "user-env-vars"}


class TestRedaction:
    def test_removes_secrets_no_editable_can_reach(self):
        # An authorization-wall flow edits exactly one field.
        redacted, paths = redact_unreachable_secrets(_project(), {"banner"})

        assert redacted["services"][0]["config"]["realms"][0]["password"] == REDACTED
        assert redacted["config"]["api-key"] == REDACTED
        assert redacted["components"][0]["user-env-vars"] == REDACTED
        assert "services/0/config/realms/0/password" in paths

    def test_keeps_a_secret_the_flow_can_edit(self):
        redacted, _ = redact_unreachable_secrets(_project(), {"user-env-vars"})
        assert redacted["components"][0]["user-env-vars"] == OTHER_AGE_BLOCK

    def test_keeps_the_whole_map_behind_a_reachable_key(self):
        """Gemeld: in het aliassenveld stond letterlijk __opi-redacted-secret__.

        Aliassen zijn PER WAARDE versleuteld, dus onder de bereikbare sleutel "aliases"
        staan sleutels als POSTGRES_HOST -- en die kent geen editable. Er werd afgedaald
        en binnenin opnieuw geoordeeld, waardoor precies de waarden verdwenen die het
        formulier moest tonen. user-env-vars ontsprong de dans alleen omdat dat EEN blok
        is en dus nooit een afdaling werd; het onderscheid zat in de opslagvorm en niet
        in de bereikbaarheid, en dat is geen onderscheid dat iemand bedoeld heeft.
        """
        project = _project()
        project["components"][0]["aliases"] = {"POSTGRES_HOST": AGE_BLOCK, "POSTGRES_USER": OTHER_AGE_BLOCK}

        redacted, paths = redact_unreachable_secrets(project, {"aliases"})

        assert redacted["components"][0]["aliases"]["POSTGRES_HOST"] == AGE_BLOCK
        assert redacted["components"][0]["aliases"]["POSTGRES_USER"] == OTHER_AGE_BLOCK
        assert not [p for p in paths if "aliases" in p]

    def test_an_unreachable_map_still_goes(self):
        """De tegenhanger: bereikbaar is de enige reden om te bewaren."""
        project = _project()
        project["components"][0]["aliases"] = {"POSTGRES_HOST": AGE_BLOCK}

        redacted, _ = redact_unreachable_secrets(project, set())

        assert redacted["components"][0]["aliases"]["POSTGRES_HOST"] == REDACTED

    def test_always_keeps_the_key_that_decrypts_the_others(self):
        # The converters decrypt and re-encrypt field values with it; without it,
        # editing any encrypted field breaks.
        redacted, _ = redact_unreachable_secrets(_project(), set())
        assert redacted["config"]["age-private-key"] == AGE_BLOCK

    def test_leaves_the_input_untouched(self):
        project = _project()
        redact_unreachable_secrets(project, set())
        assert project["config"]["api-key"] == OTHER_AGE_BLOCK

    def test_nothing_encrypted_survives_serialization(self):
        redacted, _ = redact_unreachable_secrets(_project(), {"banner"})
        serialized = json.dumps(redacted)
        # Only the deliberately-kept private key may remain.
        assert serialized.count("BEGIN AGE ENCRYPTED FILE") == 1
        assert json.loads(serialized)["config"]["age-private-key"] == AGE_BLOCK


class TestRestore:
    def test_restores_from_the_stored_project(self):
        redacted, _ = redact_unreachable_secrets(_project(), {"banner"})
        restored = restore_redacted_secrets(redacted, _project())
        assert restored == _project()

    def test_pairs_list_items_by_identifier_not_position(self):
        stored = {"realms": [{"realm": "a", "password": AGE_BLOCK}, {"realm": "b", "password": OTHER_AGE_BLOCK}]}
        # The form reordered the list; restoring by position would swap the passwords.
        reordered = {"realms": [{"realm": "b", "password": REDACTED}, {"realm": "a", "password": REDACTED}]}
        restored = restore_redacted_secrets(reordered, stored)
        assert restored["realms"][0] == {"realm": "b", "password": OTHER_AGE_BLOCK}
        assert restored["realms"][1] == {"realm": "a", "password": AGE_BLOCK}

    def test_drops_the_placeholder_when_there_is_nothing_to_restore(self):
        # A realm the form added has no stored counterpart; writing the placeholder
        # into the project file would be worse than writing nothing.
        added = {"realms": [{"realm": "new", "password": REDACTED}]}
        restored = restore_redacted_secrets(added, {"realms": []})
        assert restored == {"realms": [{"realm": "new"}]}

    def test_keeps_an_edit_the_user_actually_made(self):
        redacted, _ = redact_unreachable_secrets(_project(), {"banner"})
        redacted["services"][1]["config"]["banner"] = "Nieuwe tekst"
        restored = restore_redacted_secrets(redacted, _project())
        assert restored["services"][1]["config"]["banner"] == "Nieuwe tekst"
        assert restored["services"][0]["config"]["realms"][0]["password"] == AGE_BLOCK

    def test_placeholder_never_reaches_the_output(self):
        redacted, _ = redact_unreachable_secrets(_project(), set())
        restored = restore_redacted_secrets(redacted, {})
        assert REDACTED not in json.dumps(restored)


def _expired(path: Path) -> Path:
    """Write a session file and backdate it past SESSION_MAX_AGE_SECONDS."""
    path.write_text("{}")
    long_ago = time.time() - (48 * 60 * 60)
    os.utime(path, (long_ago, long_ago))
    return path


class TestSessionFileSweep:
    @pytest.fixture(autouse=True)
    def store(self, tmp_path, monkeypatch):
        monkeypatch.setattr(session, "_STORE_DIR", str(tmp_path))
        monkeypatch.setattr(session, "_last_sweep_at", None)
        return tmp_path

    def test_removes_only_expired_files(self, store: Path):
        fresh = store / ("a" * 32 + ".json")
        fresh.write_text("{}")
        stale = _expired(store / ("b" * 32 + ".json"))

        assert session.purge_expired_states() == 1
        assert fresh.exists()
        assert not stale.exists()

    def test_creating_a_session_sweeps_at_most_once_per_interval(self, store: Path):
        stale = _expired(store / ("c" * 32 + ".json"))
        session.init_modal_state_tokenized("f", "s", ["s"], "p")
        assert not stale.exists()

        second_stale = _expired(store / ("d" * 32 + ".json"))
        session.init_modal_state_tokenized("f", "s", ["s"], "p")
        assert second_stale.exists(), "the interval should suppress the second sweep"


BASE64_AGE = "base64+age:LS0tLS1CRUdJTiBBR0UgRU5DUllQVEVEIEZJTEUtLS0tLQo="


class TestBothStoredFormsCount:
    """The schema declares two forms of an encrypted value and both must be redacted.

    Testing for the armored block alone was not enough. Real project files store the
    repository password, the api key and the project private key with the ``base64+age:``
    prefix, so those were left in the session file on the PVC untouched -- the very
    secrets this module exists to keep off disk.

    Which form a field uses is a storage decision (the armored block is multi-line and
    needs a literal scalar, the prefixed one is a plain string), not a secrecy one.
    """

    def _project_with_prefixed_secrets(self) -> dict:
        return {
            "name": "tas-yz7",
            "config": {"age-private-key": BASE64_AGE, "api-key": BASE64_AGE},
            "repositories": [{"name": "main-repo", "password": BASE64_AGE}],
        }

    def test_the_prefixed_form_is_redacted_too(self) -> None:
        redacted, removed = redact_unreachable_secrets(self._project_with_prefixed_secrets(), set())

        assert redacted["repositories"][0]["password"] == REDACTED
        assert redacted["config"]["api-key"] == REDACTED
        assert "repositories/0/password" in removed

    def test_the_private_key_survives_in_the_prefixed_form_as_well(self) -> None:
        """The converters decrypt field values with it; dropping it breaks editing every
        encrypted field. The exception is about the key's role, not about its form."""
        redacted, _ = redact_unreachable_secrets(self._project_with_prefixed_secrets(), set())

        assert redacted["config"]["age-private-key"] == BASE64_AGE

    def test_nothing_encrypted_survives_serialization_in_either_form(self) -> None:
        """The end state that matters: what lands in the JSON file under TEMP_DIR."""
        mixed = {
            "config": {"api-key": BASE64_AGE},
            "services": [{"name": "keycloak", "config": {"realms": [{"realm": "r", "password": AGE_BLOCK}]}}],
        }
        redacted, _ = redact_unreachable_secrets(mixed, set())
        written = json.dumps(redacted)

        assert "base64+age:" not in written
        assert "BEGIN AGE ENCRYPTED FILE" not in written

    def test_a_reachable_prefixed_secret_is_kept(self) -> None:
        """Default-deny, not blanket-deny: a field the flow can edit must keep its value,
        otherwise the form would show a placeholder where the password belongs."""
        flow = [_Visualizer("repositories[*]/password")]
        redacted, _ = redact_unreachable_secrets(self._project_with_prefixed_secrets(), reachable_leaf_keys(flow))

        assert redacted["repositories"][0]["password"] == BASE64_AGE

    def test_ordinary_values_are_left_alone(self) -> None:
        """Including ``plain:``, which marks a deliberately unencrypted password: it is
        sensitive but it is not an encrypted value, and treating it as one would redact a
        field the user is expected to read and edit."""
        data = {"a": "gewone tekst", "b": "plain:zichtbaar", "c": "", "d": 42, "e": None}
        redacted, removed = redact_unreachable_secrets(data, set())

        assert redacted == data
        assert removed == []


def test_the_detector_matches_the_schema_definition() -> None:
    """Drift guard. ``$defs/age-encrypted`` in the project schema is the declaration of
    what an encrypted value looks like; the detector must agree with it, or a third form
    added to one side would pass the other silently.
    """
    import json as _json
    import re
    from pathlib import Path

    from opi.utils.age import carries_encrypted_value

    schema = _json.loads((Path(__file__).parent.parent / "opi/schemas/project_v2.json").read_text())
    pattern = re.compile(schema["$defs"]["age-encrypted"]["pattern"])

    for value in (AGE_BLOCK, BASE64_AGE):
        assert pattern.search(value), f"schema no longer matches {value[:30]!r}"
        assert carries_encrypted_value(value), f"detector no longer matches {value[:30]!r}"

    for value in ("gewone tekst", "plain:zichtbaar", ""):
        assert not pattern.search(value)
        assert not carries_encrypted_value(value)
