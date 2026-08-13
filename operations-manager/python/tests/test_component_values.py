"""The storage core behind the env-vars/aliases API (RC-55).

Two services own a key/value property on a component, and they do NOT store it the same
way: ``user-env-vars`` is one AGE block for the whole set, ``aliases`` is a mapping with
every value encrypted on its own. Every write is decrypt -> change -> re-encrypt, so the
shape is not a detail: get it wrong and either the UI can no longer read what the API
wrote, or a secret lands in git as plaintext.

Everything here runs against the real ``age`` binary. A mocked cipher would let a
"one block" and a "per value" implementation look identical, which is precisely the
difference these tests exist to see.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest
from opi.forms.editables.converters import KeyValueConverter
from opi.services.component_values import (
    ComponentValuesError,
    ValuesOperation,
    apply_operation,
    decode,
    encode,
    validate_key,
    validate_value,
    validate_value_for_storage,
)
from opi.utils.age import encrypt_age_content_sync, is_age_encrypted
from opi.utils.env_vars import validate_and_parse_env_vars

pytestmark = pytest.mark.skipif(
    shutil.which("age") is None or shutil.which("age-keygen") is None,
    reason="age/age-keygen binary not available",
)


def _keypair() -> tuple[str, str]:
    result = subprocess.run(["age-keygen"], capture_output=True, text=True, check=True)
    lines = result.stdout.splitlines() + result.stderr.splitlines()
    private_key = next(line for line in lines if line.startswith("AGE-SECRET-KEY"))
    public_key = next(line.split(": ", 1)[1].strip() for line in lines if "public key:" in line.lower())
    return public_key, private_key


@pytest.fixture
def project(monkeypatch) -> dict:
    """A project whose config carries a real AGE keypair, wired like a real one.

    The project private key sits in the file encrypted with the SYSTEM key, which is how
    every project file stores it, so the decrypt path under test is the real two-step one
    and not a shortcut that only works in a test.
    """
    system_public, system_private = _keypair()
    project_public, project_private = _keypair()
    monkeypatch.setattr("opi.core.config.settings.SOPS_AGE_PRIVATE_KEY", system_private)
    return {
        "name": "demo",
        "config": {
            "age-public-key": project_public,
            "age-private-key": encrypt_age_content_sync(project_private, system_public),
        },
        "_project_public_key": project_public,
    }


class TestTheOneStorageShape:
    """One shape for both properties since RC-106: ONE AGE block of KEY=value lines."""

    def test_env_vars_land_as_one_age_block(self, project: dict) -> None:
        stored = encode({"A": "1", "B": "2"}, project)

        assert isinstance(stored, str)
        assert is_age_encrypted(stored), "user-env-vars must be stored as a single AGE block"
        assert stored.count("BEGIN AGE ENCRYPTED FILE") == 1, "one block for the set, not one per entry"
        assert decode(stored, project) == {"A": "1", "B": "2"}

    def test_aliases_land_as_one_age_block_too(self, project: dict) -> None:
        # The change RC-106 makes: aliases used to be a mapping with each value encrypted
        # on its own, which is what made every reader depend on a decrypt step of its own.
        stored = encode({"POSTGRES_HOST": "$DATABASE_SERVER_HOST"}, project)

        assert isinstance(stored, str), "aliases are one string, not a mapping"
        assert not isinstance(stored, dict)
        assert is_age_encrypted(stored)
        assert stored.count("BEGIN AGE ENCRYPTED FILE") == 1
        assert "POSTGRES_HOST" not in stored, "the names live inside the block, not next to it"
        assert decode(stored, project) == {"POSTGRES_HOST": "$DATABASE_SERVER_HOST"}

    def test_a_full_round_trip_gives_back_the_same_map(self, project: dict) -> None:
        values = {"POSTGRES_HOST": "$DATABASE_SERVER_HOST", "POSTGRES_PORT": "$DATABASE_SERVER_PORT"}

        assert decode(encode(values, project), project) == values

    def test_no_plaintext_value_survives(self, project: dict) -> None:
        stored = encode({"TOKEN": "rc55-plaintext-secret"}, project)

        assert "rc55-plaintext-secret" not in str(stored)

    def test_an_empty_map_means_remove_the_property(self, project: dict) -> None:
        assert encode({}, project) is None


class TestFailClosed:
    def test_no_public_key_writes_nothing(self, project: dict) -> None:
        project["config"].pop("age-public-key")

        with pytest.raises(ComponentValuesError, match="publieke sleutel"):
            encode({"A": "1"}, project)

    def test_a_block_that_cannot_be_decrypted_raises_instead_of_passing_ciphertext_through(self, project: dict) -> None:
        # Encrypted for somebody else's key. The tempting failure mode is to hand the
        # armored block back as if it were the value, which would re-store the ciphertext
        # as plaintext on the very next write.
        other_public, _ = _keypair()
        foreign = encrypt_age_content_sync("A=1", other_public)

        with pytest.raises(ComponentValuesError, match="ontsleutelen"):
            decode(foreign, project)

    def test_a_per_value_mapping_that_cannot_be_decrypted_raises(self, project: dict) -> None:
        # The shape aliases used to be written in, encrypted for somebody else's key.
        other_public, _ = _keypair()
        foreign = {"HOST": encrypt_age_content_sync("secret", other_public)}

        with pytest.raises(ComponentValuesError, match="ontsleutelen"):
            decode(foreign, project)


class TestReadingWhatIsAlreadyThere:
    def test_absent_property_is_an_empty_map(self, project: dict) -> None:
        assert decode(None, project) == {}
        assert decode("", project) == {}
        assert decode({}, project) == {}

    def test_a_legacy_plaintext_env_var_block_is_read(self, project: dict) -> None:
        # A hand-written project file, or one from before encryption. Read, not refused;
        # the first write through the API stores it encrypted.
        assert decode("A=1\nB=2", project) == {"A": "1", "B": "2"}

    def test_a_legacy_mapping_env_var_value_is_read(self, project: dict) -> None:
        assert decode({"A": "1"}, project) == {"A": "1"}

    def test_an_unencrypted_alias_mapping_is_read(self, project: dict) -> None:
        # Still a supported shape in the project schema, so it passes through as stored.
        assert decode({"HOST": "$DATABASE_SERVER_HOST"}, project) == {"HOST": "$DATABASE_SERVER_HOST"}

    def test_a_per_value_encrypted_mapping_is_still_read(self, project: dict) -> None:
        # The shape aliases were written in before RC-106. Read, never written again --
        # without this such a project shows ciphertext on the component card.
        public = project["_project_public_key"]
        stored = {"HOST": encrypt_age_content_sync("$DATABASE_SERVER_HOST", public)}

        assert decode(stored, project) == {"HOST": "$DATABASE_SERVER_HOST"}

    def test_both_read_shapes_give_the_same_values(self, project: dict) -> None:
        # The point of RC-106: the unencrypted mapping and the one AGE block are two
        # spellings of one set, and a reader must not be able to tell them apart.
        values = {"POSTGRES_HOST": "$DATABASE_SERVER_HOST", "OIDC": "$OIDC_URL"}

        assert decode(values, project) == decode(encode(values, project), project) == values


class TestTheUIReadsWhatTheAPIWrites:
    """The round trip that keeps the wizard and the API on one storage form."""

    def test_the_editor_can_read_an_api_written_env_var_block(self, project: dict) -> None:
        stored = encode({"A": "1", "B": "2"}, project)

        shown = KeyValueConverter(fmt="env", write_as="string").read(stored, context_data=project)

        assert shown == "A=1\nB=2"

    def test_the_editor_can_read_api_written_aliases(self, project: dict) -> None:
        stored = encode({"POSTGRES_HOST": "$DATABASE_SERVER_HOST"}, project)

        shown = KeyValueConverter(fmt="env", write_as="string").read(stored, context_data=project)

        assert shown == "POSTGRES_HOST=$DATABASE_SERVER_HOST"

    def test_the_api_can_read_what_the_editor_wrote(self, project: dict) -> None:
        # The other direction, which is the one that breaks silently: the editor writes,
        # the API reads it back to change one entry.
        env_written = KeyValueConverter(fmt="env", write_as="string").write("A=1\nB=2", context_data=project)
        alias_written = KeyValueConverter(fmt="env", write_as="string").write(
            "POSTGRES_HOST=$DATABASE_SERVER_HOST", context_data=project
        )

        assert decode(env_written, project) == {"A": "1", "B": "2"}
        assert decode(alias_written, project) == {"POSTGRES_HOST": "$DATABASE_SERVER_HOST"}


class TestTheOperations:
    def test_add_puts_new_names_in(self) -> None:
        assert apply_operation({"A": "1"}, ValuesOperation.ADD, values={"B": "2"}) == {"A": "1", "B": "2"}

    def test_add_refuses_a_name_that_is_already_there(self) -> None:
        # Without this a typo silently overwrites a value, and since the stored form is
        # encrypted the diff shows nothing a reviewer could catch.
        with pytest.raises(ComponentValuesError, match="Bestaat al"):
            apply_operation({"A": "1"}, ValuesOperation.ADD, values={"A": "2"})

    def test_patch_changes_an_existing_value(self) -> None:
        assert apply_operation({"A": "1"}, ValuesOperation.PATCH, values={"A": "2"}) == {"A": "2"}

    def test_patch_refuses_a_name_that_is_not_there(self) -> None:
        with pytest.raises(ComponentValuesError, match="Onbekende naam"):
            apply_operation({"A": "1"}, ValuesOperation.PATCH, values={"B": "2"})

    def test_delete_removes_by_name(self) -> None:
        assert apply_operation({"A": "1", "B": "2"}, ValuesOperation.DELETE, keys=["A"]) == {"B": "2"}

    def test_delete_refuses_a_name_that_is_not_there_and_removes_nothing(self) -> None:
        with pytest.raises(ComponentValuesError, match="Onbekende naam"):
            apply_operation({"A": "1"}, ValuesOperation.DELETE, keys=["A", "B"])

    def test_clear_empties_the_whole_layer(self) -> None:
        assert apply_operation({"A": "1", "B": "2"}, ValuesOperation.CLEAR) == {}

    def test_bulk_is_the_base_form_not_a_special_case(self) -> None:
        assert apply_operation({}, ValuesOperation.ADD, values={"A": "1", "B": "2", "C": "3"}) == {
            "A": "1",
            "B": "2",
            "C": "3",
        }

    def test_the_input_map_is_not_mutated(self) -> None:
        current = {"A": "1"}
        apply_operation(current, ValuesOperation.ADD, values={"B": "2"})
        assert current == {"A": "1"}


class TestNameAndValueRules:
    @pytest.mark.parametrize("key", ["A", "_a", "A1_b", "_"])
    def test_valid_names_pass(self, key: str) -> None:
        validate_key(key)

    @pytest.mark.parametrize("key", ["1A", "a-b", "a b", "", "a.b", "A$"])
    def test_invalid_names_are_refused(self, key: str) -> None:
        with pytest.raises(ComponentValuesError):
            validate_key(key)

    @pytest.mark.parametrize("value", ["", "plain", "with spaces", "$DATABASE_SERVER_HOST", "a=b=c"])
    def test_ordinary_values_pass(self, value: str) -> None:
        validate_value("K", value)

    @pytest.mark.parametrize("value", ["two\nlines", "carriage\rreturn", "nul\x00byte"])
    def test_a_value_that_cannot_travel_as_a_key_value_line_is_refused(self, value: str) -> None:
        with pytest.raises(ComponentValuesError):
            validate_value("K", value)

    def test_a_refusal_never_quotes_the_value_back(self) -> None:
        with pytest.raises(ComponentValuesError) as caught:
            validate_value("TOKEN", "rc55-secret\nsmuggled")
        assert "rc55-secret" not in str(caught.value)
        assert "TOKEN" in str(caught.value)


class TestStorageFidelity:
    """A value must read back byte for byte, or it is refused (RC-55 review).

    Two normalisations sit between the write and the next read. Decryption strips the
    plaintext, and the block's ``KEY=value`` lines are read back with
    ``validate_and_parse_env_vars``, which removes one pair of surrounding quotes. A value
    that does not survive would come back different from what was written AND would never
    equal what is stored, so every write of it would commit again in ``zad-projects`` --
    the exact churn the design forbids.

    Since RC-106 this holds for aliases too: they are stored the same way, so the quote
    rule that used to apply to user-env-vars alone now applies to both.
    """

    @pytest.mark.parametrize("value", ["", "plain", "with inner spaces", "a=b=c", "it's"])
    def test_a_value_that_survives_passes(self, value: str) -> None:
        validate_value_for_storage("K", value)

    @pytest.mark.parametrize("value", [" x ", "x ", " x", "\t", " ", "x\t"])
    def test_edge_whitespace_is_refused(self, value: str) -> None:
        # age decryption strips its plaintext, so this is lost whichever way it is stored.
        with pytest.raises(ComponentValuesError):
            validate_value_for_storage("K", value)

    @pytest.mark.parametrize("value", ['"q"', "'q'", '""'])
    def test_surrounding_quotes_are_refused(self, value: str) -> None:
        with pytest.raises(ComponentValuesError):
            validate_value_for_storage("K", value)

    def test_the_refusal_names_the_key_and_not_the_value(self) -> None:
        with pytest.raises(ComponentValuesError) as caught:
            validate_value_for_storage("TOKEN", " rc55-secret ")
        assert "rc55-secret" not in str(caught.value)
        assert "TOKEN" in str(caught.value)

    def test_every_accepted_value_round_trips_through_the_real_block(self) -> None:
        """The guard is measured against the storage form, not against its description."""
        accepted = {"A": "plain", "B": "", "C": "a=b=c", "D": 'say "hi" now', "E": "with inner spaces"}
        for key, value in accepted.items():
            validate_value_for_storage(key, value)
        block = "\n".join(f"{key}={value}" for key, value in accepted.items())
        assert validate_and_parse_env_vars(block) == accepted
