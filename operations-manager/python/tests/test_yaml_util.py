"""Tests for opi.utils.yaml_util module."""

from opi.utils.yaml_util import (
    dump_yaml_to_string,
    find_value_by_jsonpath,
    load_yaml_from_string,
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
