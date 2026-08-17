"""Tests for opi.utils.yaml_util module."""

import os
import tempfile

from opi.utils.yaml_util import (
    dump_yaml_to_string,
    find_value_by_jsonpath,
    load_yaml_from_string,
    save_yaml_to_path,
    update_value_by_jsonpath,
)


class TestLoadYamlFromString:
    """Tests for load_yaml_from_string."""

    def test_valid_yaml(self):
        yaml_string = "name: test\nvalue: 42\n"
        result = load_yaml_from_string(yaml_string)
        assert result is not None
        assert result["name"] == "test"
        assert result["value"] == 42

    def test_nested_yaml(self):
        yaml_string = "metadata:\n  name: my-app\n  namespace: default\n"
        result = load_yaml_from_string(yaml_string)
        assert result is not None
        assert result["metadata"]["name"] == "my-app"
        assert result["metadata"]["namespace"] == "default"

    def test_invalid_yaml_returns_none(self):
        yaml_string = ":\n  - :\n  invalid: [unbalanced"
        result = load_yaml_from_string(yaml_string)
        assert result is None

    def test_empty_string_returns_none(self):
        result = load_yaml_from_string("")
        assert result is None

    def test_yaml_with_list(self):
        yaml_string = "items:\n  - one\n  - two\n  - three\n"
        result = load_yaml_from_string(yaml_string)
        assert result is not None
        assert result["items"] == ["one", "two", "three"]


class TestDumpYamlToString:
    """Tests for dump_yaml_to_string."""

    def test_simple_dict(self):
        data = {"name": "test", "value": 42}
        result = dump_yaml_to_string(data)
        assert "name: test" in result
        assert "value: 42" in result

    def test_nested_dict(self):
        data = {"metadata": {"name": "my-app", "namespace": "default"}}
        result = dump_yaml_to_string(data)
        assert "metadata:" in result
        assert "name: my-app" in result
        assert "namespace: default" in result

    def test_empty_dict(self):
        result = dump_yaml_to_string({})
        assert isinstance(result, str)

    def test_roundtrip(self):
        original = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "test"}}
        yaml_str = dump_yaml_to_string(original)
        loaded = load_yaml_from_string(yaml_str)
        assert loaded["apiVersion"] == "v1"
        assert loaded["kind"] == "ConfigMap"
        assert loaded["metadata"]["name"] == "test"


class TestFindValueByJsonpath:
    """Tests for find_value_by_jsonpath."""

    def test_simple_key(self):
        data = {"name": "test", "value": 42}
        result = find_value_by_jsonpath(data, "$.name")
        assert result == "test"

    def test_nested_key(self):
        data = {"metadata": {"name": "my-app", "namespace": "default"}}
        result = find_value_by_jsonpath(data, "$.metadata.name")
        assert result == "my-app"

    def test_missing_key_returns_default(self):
        data = {"name": "test"}
        result = find_value_by_jsonpath(data, "$.missing")
        assert result is None

    def test_missing_key_returns_custom_default(self):
        data = {"name": "test"}
        result = find_value_by_jsonpath(data, "$.missing", default="fallback")
        assert result == "fallback"

    def test_empty_data_returns_default(self):
        result = find_value_by_jsonpath({}, "$.name")
        assert result is None

    def test_none_data_returns_default(self):
        result = find_value_by_jsonpath(None, "$.name")
        assert result is None

    def test_deeply_nested_key(self):
        data = {"a": {"b": {"c": "deep"}}}
        result = find_value_by_jsonpath(data, "$.a.b.c")
        assert result == "deep"


class TestUpdateValueByJsonpath:
    """Tests for update_value_by_jsonpath."""

    def test_update_existing_key(self):
        data = {"name": "old", "value": 1}
        result = update_value_by_jsonpath(data, "$.name", "new")
        assert result is True
        assert data["name"] == "new"

    def test_update_nested_key(self):
        data = {"metadata": {"name": "old-app"}}
        result = update_value_by_jsonpath(data, "$.metadata.name", "new-app")
        assert result is True
        assert data["metadata"]["name"] == "new-app"

    def test_missing_key_returns_false(self):
        data = {"name": "test"}
        result = update_value_by_jsonpath(data, "$.nonexistent", "value")
        assert result is False

    def test_empty_data_returns_false(self):
        result = update_value_by_jsonpath({}, "$.name", "value")
        assert result is False

    def test_none_data_returns_false(self):
        result = update_value_by_jsonpath(None, "$.name", "value")
        assert result is False

    def test_update_preserves_other_keys(self):
        data = {"name": "old", "keep": "this"}
        update_value_by_jsonpath(data, "$.name", "new")
        assert data["keep"] == "this"


class TestSaveYamlToPath:
    """Tests for save_yaml_to_path."""

    def test_bare_filename_without_directory(self):
        """save_yaml_to_path should succeed when file_path has no directory component."""
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = os.path.join(tmpdir, "test.yaml")
            result = save_yaml_to_path(file_path, {"key": "value"})
            assert result is True
            assert os.path.exists(file_path)

    def test_bare_filename_in_cwd(self):
        """save_yaml_to_path with a bare filename (no dir) should not fail due to empty dirname."""
        with tempfile.TemporaryDirectory() as tmpdir:
            original_cwd = os.getcwd()
            try:
                os.chdir(tmpdir)
                result = save_yaml_to_path("bare_file.yaml", {"key": "value"})
                assert result is True, "save_yaml_to_path should succeed with bare filename in CWD"
                assert os.path.exists(os.path.join(tmpdir, "bare_file.yaml"))
            finally:
                os.chdir(original_cwd)


AGE_BLOCK = (
    "-----BEGIN AGE ENCRYPTED FILE-----\n"
    "YWdlLWVuY3J5cHRpb24ub3JnL3YxCi0+IFgyNTUxOSBQd3RVbm85emtqV1ZJRnJ0\n"
    "ZklzVEpmbmFvWTRFbzhCQ1FOUEVBak1Rd1h3CkFKd0xxa0IyUFBwUmFpKzZRV0ho\n"
    "-----END AGE ENCRYPTED FILE-----"
)


class TestMultilineScalarStyle:
    """Multi-line values must be written as literal blocks by the canonical writer.

    Regression: a keycloak realm password went through the modal-edit wizard, whose
    session round-trips the project dict through JSON. That strips ruamel's
    LiteralScalarString, and the value landed in the project file as one quoted line
    full of ``\\n`` escapes.
    """

    def test_plain_multiline_string_becomes_literal_block(self):
        output = dump_yaml_to_string({"password": AGE_BLOCK})
        assert "password: |-" in output
        assert "\\n" not in output

    def test_survives_a_json_round_trip(self):
        import json

        from ruamel.yaml.scalarstring import LiteralScalarString

        data = {"password": LiteralScalarString(AGE_BLOCK)}
        output = dump_yaml_to_string(json.loads(json.dumps(data)))
        assert "password: |-" in output
        assert load_yaml_from_string(output)["password"] == AGE_BLOCK

    def test_committed_quoted_block_is_repaired_on_rewrite(self):
        escaped = AGE_BLOCK.replace("\n", "\\n")
        broken = load_yaml_from_string(f'password: "{escaped}"\n')
        output = dump_yaml_to_string(broken)
        assert "password: |-" in output
        assert load_yaml_from_string(output)["password"] == AGE_BLOCK

    def test_single_line_quoting_is_still_preserved(self):
        source = "status: 'requested'\nname: plain\n"
        assert dump_yaml_to_string(load_yaml_from_string(source)) == source

    def test_carriage_returns_stay_quoted_so_the_value_survives(self):
        # A block scalar writes the \r out raw and YAML normalizes line breaks on
        # read, which would silently turn "a\r\nb" into "a\nb".
        output = dump_yaml_to_string({"text": "a\r\nb"})
        assert load_yaml_from_string(output)["text"] == "a\r\nb"
