"""Tests for opi.utils.env_vars module."""

import pytest
from opi.utils.env_vars import (
    _detect_env_var_format,
    detect_circular_references,
    extract_variable_references,
    substitute_variables,
    validate_and_parse_env_vars,
)
from ruamel.yaml.comments import CommentedMap


class TestDetectEnvVarFormat:
    """Tests for _detect_env_var_format."""

    def test_keyvalue_format(self):
        text = "DATABASE_URL=postgresql://localhost\nAPI_KEY=secret"
        assert _detect_env_var_format(text) == "keyvalue"

    def test_yaml_format(self):
        text = "database_url: postgresql://localhost\napi_key: secret"
        assert _detect_env_var_format(text) == "yaml"

    def test_yaml_with_document_start(self):
        text = "---\nkey: value"
        assert _detect_env_var_format(text) == "yaml"

    def test_mixed_more_keyvalue(self):
        text = "A=1\nB=2\nc: 3"
        assert _detect_env_var_format(text) == "keyvalue"

    def test_mixed_more_yaml(self):
        text = "a: 1\nb: 2\nC=3"
        assert _detect_env_var_format(text) == "yaml"

    def test_skips_empty_lines(self):
        text = "\n\nDATABASE_URL=value\n\n"
        assert _detect_env_var_format(text) == "keyvalue"

    def test_skips_comments(self):
        text = "# comment\nDATABASE_URL=value"
        assert _detect_env_var_format(text) == "keyvalue"

    def test_only_comments_and_blanks(self):
        text = "# comment\n\n# another"
        assert _detect_env_var_format(text) == "keyvalue"

    def test_lowercase_keyvalue_not_misdetected_as_yaml(self):
        """Lowercase key=value lines should count as keyvalue, not fall through to yaml."""
        text = "my_var=hello\nother_key: world"
        assert _detect_env_var_format(text) == "keyvalue", (
            "lowercase key=value should count as keyvalue indicator"
        )

    def test_yaml_with_curly_brace_start(self):
        text = "{\nkey: value\n}"
        assert _detect_env_var_format(text) == "yaml"

    def test_yaml_with_bracket_start(self):
        text = "[\nvalue1\n]"
        assert _detect_env_var_format(text) == "yaml"


class TestValidateAndParseEnvVars:
    """Tests for validate_and_parse_env_vars."""

    # None/empty inputs
    def test_none_input(self):
        assert validate_and_parse_env_vars(None) == {}

    def test_empty_string(self):
        assert validate_and_parse_env_vars("") == {}

    # KEY=VALUE format
    def test_basic_keyvalue(self):
        result = validate_and_parse_env_vars("DATABASE_URL=postgresql://localhost")
        assert result == {"DATABASE_URL": "postgresql://localhost"}

    def test_multiple_keyvalue(self):
        text = "KEY1=value1\nKEY2=value2"
        result = validate_and_parse_env_vars(text)
        assert result == {"KEY1": "value1", "KEY2": "value2"}

    def test_keyvalue_with_double_quotes(self):
        result = validate_and_parse_env_vars('MY_VAR="hello world"')
        assert result == {"MY_VAR": "hello world"}

    def test_keyvalue_with_single_quotes(self):
        result = validate_and_parse_env_vars("MY_VAR='hello world'")
        assert result == {"MY_VAR": "hello world"}

    def test_keyvalue_with_equals_in_value(self):
        result = validate_and_parse_env_vars("CONNECTION=host=db;port=5432")
        assert result == {"CONNECTION": "host=db;port=5432"}

    def test_keyvalue_with_comments(self):
        text = "# This is a comment\nKEY=value\n# Another comment"
        result = validate_and_parse_env_vars(text)
        assert result == {"KEY": "value"}

    def test_keyvalue_empty_value(self):
        result = validate_and_parse_env_vars("EMPTY_VAR=")
        assert result == {"EMPTY_VAR": ""}

    def test_keyvalue_quoted_empty_double(self):
        result = validate_and_parse_env_vars('EMPTY_VAR=""')
        assert result == {"EMPTY_VAR": ""}

    def test_keyvalue_quoted_empty_single(self):
        result = validate_and_parse_env_vars("EMPTY_VAR=''")
        assert result == {"EMPTY_VAR": ""}

    def test_keyvalue_skips_blank_lines(self):
        text = "\nKEY=value\n\n"
        result = validate_and_parse_env_vars(text)
        assert result == {"KEY": "value"}

    # YAML format
    def test_yaml_basic(self):
        text = "database_url: postgresql://localhost\napi_key: secret"
        result = validate_and_parse_env_vars(text)
        assert result == {"database_url": "postgresql://localhost", "api_key": "secret"}

    def test_yaml_with_numbers(self):
        text = "PORT: 8080\nREPLICAS: 3"
        result = validate_and_parse_env_vars(text)
        assert result == {"PORT": "8080", "REPLICAS": "3"}

    def test_yaml_with_booleans(self):
        text = "DEBUG: true\nVERBOSE: false"
        result = validate_and_parse_env_vars(text)
        assert result == {"DEBUG": "true", "VERBOSE": "false"}

    def test_yaml_with_none_value(self):
        text = "OPTIONAL_VAR: null"
        result = validate_and_parse_env_vars(text)
        assert result == {"OPTIONAL_VAR": ""}

    def test_yaml_with_none_no_value(self):
        # "OPTIONAL_VAR:" alone is not detected as YAML (regex requires value after colon),
        # so we need additional YAML context to trigger YAML parsing
        text = "---\nOPTIONAL_VAR:"
        result = validate_and_parse_env_vars(text)
        assert result == {"OPTIONAL_VAR": ""}

    # Error cases
    def test_invalid_format_no_equals(self):
        with pytest.raises(ValueError, match="Invalid format"):
            validate_and_parse_env_vars("INVALID_LINE")

    def test_empty_key(self):
        with pytest.raises(ValueError, match="key cannot be empty"):
            validate_and_parse_env_vars("=value")

    def test_invalid_key_format(self):
        with pytest.raises(ValueError, match="Invalid key format"):
            validate_and_parse_env_vars("123BAD=value")

    def test_invalid_key_with_spaces(self):
        with pytest.raises(ValueError, match="Invalid key format"):
            validate_and_parse_env_vars("BAD KEY=value")

    # CommentedMap input
    def test_commented_map_input(self):
        cm = CommentedMap()
        cm["KEY1"] = "value1"
        cm["KEY2"] = "value2"
        result = validate_and_parse_env_vars(cm)
        assert result == {"KEY1": "value1", "KEY2": "value2"}
        assert isinstance(result, dict)
        assert not isinstance(result, CommentedMap)


class TestExtractVariableReferences:
    """Tests for extract_variable_references."""

    def test_dollar_var_syntax(self):
        result = extract_variable_references("$HOST:$PORT")
        assert result == ["HOST", "PORT"]

    def test_braced_var_syntax(self):
        result = extract_variable_references("${USER}@${HOST}")
        assert result == ["HOST", "USER"]

    def test_mixed_syntax(self):
        result = extract_variable_references("$VAR1 and ${VAR2}")
        assert result == ["VAR1", "VAR2"]

    def test_empty_string(self):
        assert extract_variable_references("") == []

    def test_none_input(self):
        assert extract_variable_references(None) == []

    def test_no_variables(self):
        assert extract_variable_references("plain text") == []

    def test_duplicate_references(self):
        result = extract_variable_references("$VAR and $VAR again")
        assert result == ["VAR"]

    def test_result_is_sorted(self):
        result = extract_variable_references("$ZEBRA $ALPHA $MIDDLE")
        assert result == ["ALPHA", "MIDDLE", "ZEBRA"]

    def test_underscore_in_name(self):
        result = extract_variable_references("$MY_VAR_NAME")
        assert result == ["MY_VAR_NAME"]

    def test_var_with_numbers(self):
        result = extract_variable_references("$VAR1 ${VAR2}")
        assert result == ["VAR1", "VAR2"]


class TestSubstituteVariables:
    """Tests for substitute_variables."""

    def test_simple_dollar_substitution(self):
        result = substitute_variables("$HOST:$PORT", {"HOST": "localhost", "PORT": "5432"})
        assert result == "localhost:5432"

    def test_braced_substitution(self):
        result = substitute_variables("${USER}@${HOST}", {"USER": "admin", "HOST": "db.svc"})
        assert result == "admin@db.svc"

    def test_escaped_dollar(self):
        result = substitute_variables("Price: $$10", {})
        assert result == "Price: $10"

    def test_escaped_dollar_with_vars(self):
        result = substitute_variables("$$literal and $VAR", {"VAR": "value"})
        assert result == "$literal and value"

    def test_missing_variable_raises(self):
        with pytest.raises(ValueError, match="not found in context"):
            substitute_variables("$MISSING", {"OTHER": "value"})

    def test_empty_template(self):
        result = substitute_variables("", {"VAR": "value"})
        assert result == ""

    def test_none_template(self):
        result = substitute_variables(None, {"VAR": "value"})
        assert result is None

    def test_no_variables_in_template(self):
        result = substitute_variables("plain text", {})
        assert result == "plain text"

    def test_mixed_syntax_substitution(self):
        result = substitute_variables(
            "$A and ${B}",
            {"A": "first", "B": "second"},
        )
        assert result == "first and second"

    def test_value_with_backslash_digit(self):
        """Values containing backslash-digit (e.g. \\1) must not crash re.sub."""
        result = substitute_variables("$CONN", {"CONN": r"jdbc:host?opt=\1"})
        assert result == r"jdbc:host?opt=\1"


class TestDetectCircularReferences:
    """Tests for detect_circular_references."""

    def test_no_cycle(self):
        aliases = {"A": "$B", "B": "literal_value"}
        detect_circular_references(aliases)  # should not raise

    def test_simple_cycle(self):
        aliases = {"A": "$B", "B": "$A"}
        with pytest.raises(ValueError, match="Circular reference detected"):
            detect_circular_references(aliases)

    def test_longer_cycle(self):
        aliases = {"A": "$B", "B": "$C", "C": "$A"}
        with pytest.raises(ValueError, match="Circular reference detected"):
            detect_circular_references(aliases)

    def test_empty_input(self):
        detect_circular_references({})  # should not raise

    def test_no_dependencies_on_other_aliases(self):
        aliases = {"A": "$EXTERNAL", "B": "literal"}
        detect_circular_references(aliases)  # should not raise

    def test_self_referencing(self):
        aliases = {"A": "$A"}
        with pytest.raises(ValueError, match="Circular reference detected"):
            detect_circular_references(aliases)

    def test_chain_without_cycle(self):
        aliases = {"A": "$B", "B": "$C", "C": "final"}
        detect_circular_references(aliases)  # should not raise

    def test_cycle_message_contains_path(self):
        aliases = {"X": "$Y", "Y": "$Z", "Z": "$X"}
        with pytest.raises(ValueError, match=r"X.*Y.*Z.*X|Y.*Z.*X.*Y|Z.*X.*Y.*Z"):
            detect_circular_references(aliases)
