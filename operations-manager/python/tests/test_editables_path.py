from __future__ import annotations

import pytest
from opi.forms.editables.path import get_value, resolve_path, set_value


class TestGetValue:
    def test_simple_key(self):
        data = {"name": "test-project"}
        assert get_value(data, "name") == "test-project"

    def test_hyphenated_key(self):
        data = {"display-name": "Test Project"}
        assert get_value(data, "display-name") == "Test Project"

    def test_nested_dict(self):
        data = {"config": {"age-public-key": "age1abc..."}}
        assert get_value(data, "config/age-public-key") == "age1abc..."

    def test_deep_nesting(self):
        data = {"a": {"b": {"c": {"d": 42}}}}
        assert get_value(data, "a/b/c/d") == 42

    def test_concrete_index(self):
        data = {"users": [{"email": "a@b.c"}, {"email": "d@e.f"}]}
        assert get_value(data, "users[0]/email") == "a@b.c"
        assert get_value(data, "users[1]/email") == "d@e.f"

    def test_wildcard_sequence(self):
        data = {"users": [{"email": "a@b.c"}, {"email": "d@e.f"}]}
        assert get_value(data, "users[*]/email") == ["a@b.c", "d@e.f"]

    def test_nested_wildcards(self):
        data = {
            "deployments": [
                {"components": [{"image": "img1"}, {"image": "img2"}]},
                {"components": [{"image": "img3"}]},
            ]
        }
        result = get_value(data, "deployments[*]/components[*]/image")
        assert result == [["img1", "img2"], ["img3"]]

    def test_wildcard_no_remaining_path(self):
        data = {"users": [{"email": "a@b.c"}, {"email": "d@e.f"}]}
        result = get_value(data, "users[*]")
        # Should return the list items themselves
        assert result == [{"email": "a@b.c"}, {"email": "d@e.f"}]

    def test_missing_key_returns_none(self):
        assert get_value({}, "nonexistent") is None

    def test_missing_nested_key_returns_none(self):
        data = {"config": {}}
        assert get_value(data, "config/missing-key") is None

    def test_missing_intermediate_returns_none(self):
        data = {"a": "not-a-dict"}
        assert get_value(data, "a/b/c") is None

    def test_index_out_of_bounds_returns_none(self):
        data = {"users": [{"email": "a@b.c"}]}
        assert get_value(data, "users[5]/email") is None

    def test_wildcard_on_non_list_returns_none(self):
        data = {"users": "not-a-list"}
        assert get_value(data, "users[*]/email") is None

    def test_empty_path_returns_data(self):
        data = {"name": "test"}
        assert get_value(data, "") == data

    def test_plain_key_returns_full_value(self):
        """A path to a dict returns the entire dict."""
        data = {"config": {"a": 1, "b": 2}}
        assert get_value(data, "config") == {"a": 1, "b": 2}

    def test_plain_key_returns_list(self):
        """A path to a list returns the entire list."""
        data = {"clusters": ["local", "staging"]}
        assert get_value(data, "clusters") == ["local", "staging"]


class TestSetValue:
    def test_simple_key(self):
        data: dict = {}
        result = set_value(data, "name", "test")
        assert result["name"] == "test"

    def test_hyphenated_key(self):
        data: dict = {}
        set_value(data, "display-name", "Test")
        assert data["display-name"] == "Test"

    def test_create_nested_intermediate(self):
        data: dict = {}
        set_value(data, "config/age-public-key", "age1abc")
        assert data["config"]["age-public-key"] == "age1abc"

    def test_overwrite_existing(self):
        data = {"name": "old"}
        set_value(data, "name", "new")
        assert data["name"] == "new"

    def test_concrete_index_in_list(self):
        data = {"users": [{"email": "old@b.c"}]}
        set_value(data, "users[0]/email", "new@b.c")
        assert data["users"][0]["email"] == "new@b.c"

    def test_extend_list_for_index(self):
        data = {"users": [{"email": "a@b.c"}]}
        set_value(data, "users[2]/email", "c@d.e")
        assert len(data["users"]) == 3
        assert data["users"][2]["email"] == "c@d.e"

    def test_wildcard_raises_value_error(self):
        data = {"users": []}
        with pytest.raises(ValueError, match="wildcard"):
            set_value(data, "users[*]/email", "x@y.z")

    def test_returns_modified_data(self):
        data = {"a": 1}
        result = set_value(data, "b", 2)
        assert result is data  # Same dict, mutated in place
        assert result["b"] == 2

    def test_create_list_for_index(self):
        """If path has [0] but key doesn't exist, create list."""
        data: dict = {}
        set_value(data, "users[0]/email", "a@b.c")
        assert isinstance(data["users"], list)
        assert data["users"][0]["email"] == "a@b.c"


class TestResolvePath:
    def test_single_wildcard(self):
        assert resolve_path("users[*]/email", 2) == "users[2]/email"

    def test_multiple_wildcards_first_only(self):
        assert resolve_path("a[*]/b[*]/c", 1) == "a[1]/b[*]/c"

    def test_no_wildcard_unchanged(self):
        assert resolve_path("name", 5) == "name"

    def test_none_index_unchanged(self):
        assert resolve_path("users[*]/email", None) == "users[*]/email"

    def test_zero_index(self):
        assert resolve_path("items[*]", 0) == "items[0]"
