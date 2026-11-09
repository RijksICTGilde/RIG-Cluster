"""
Value converters for form field transformation.

Converters handle bidirectional transformation between form values
and storage/display formats. Common use cases:
- AGE encryption/decryption for sensitive fields
- Date/time format conversion
- JSON serialization/deserialization
"""

from typing import Any, Protocol


class Converter(Protocol):
    """
    Protocol for value converters.

    Converters transform values between form display format
    and storage/processing format.
    """

    async def to_form(self, value: Any) -> Any:
        """
        Convert value for form display.

        Args:
            value: Value from storage/model

        Returns:
            Value suitable for form display
        """
        ...

    async def from_form(self, value: Any) -> Any:
        """
        Convert form value back to storage format.

        Args:
            value: Value from form submission

        Returns:
            Value suitable for storage/model
        """
        ...


class IdentityConverter:
    """
    Identity converter that returns values unchanged.

    Used as the default converter when no transformation is needed.
    """

    async def to_form(self, value: Any) -> Any:
        """Return value unchanged."""
        return value

    async def from_form(self, value: Any) -> Any:
        """Return value unchanged."""
        return value


class StringConverter:
    """
    Converter that ensures values are strings.

    Converts None to empty string and other types to their string representation.
    """

    async def to_form(self, value: Any) -> str:
        """Convert value to string for form display."""
        if value is None:
            return ""
        return str(value)

    async def from_form(self, value: Any) -> str:
        """Convert form value to string."""
        if value is None:
            return ""
        return str(value).strip()


class IntegerConverter:
    """
    Converter for integer values.

    Handles string-to-int conversion for form submissions.
    """

    def __init__(self, default: int = 0) -> None:
        """
        Initialize with default value for empty/invalid input.

        Args:
            default: Default value if conversion fails
        """
        self.default = default

    async def to_form(self, value: Any) -> str:
        """Convert integer to string for form display."""
        if value is None:
            return ""
        return str(value)

    async def from_form(self, value: Any) -> int:
        """Convert form value to integer."""
        if value is None or value == "":
            return self.default
        try:
            return int(value)
        except ValueError, TypeError:
            return self.default


class BooleanConverter:
    """
    Converter for boolean values.

    Handles checkbox/boolean field conversions.
    """

    async def to_form(self, value: Any) -> bool:
        """Convert value to boolean for form display."""
        if value is None:
            return False
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.lower() in ("true", "1", "yes", "on")
        return bool(value)

    async def from_form(self, value: Any) -> bool:
        """Convert form value to boolean."""
        if value is None:
            return False
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.lower() in ("true", "1", "yes", "on")
        return bool(value)


class ListConverter:
    """
    Converter for list/multi-select values.

    Handles conversion between list and form array formats.
    """

    async def to_form(self, value: Any) -> list[str]:
        """Convert value to list for form display."""
        if value is None:
            return []
        if isinstance(value, list):
            return [str(v) for v in value]
        if isinstance(value, str):
            return [v.strip() for v in value.split(",") if v.strip()]
        return [str(value)]

    async def from_form(self, value: Any) -> list[str]:
        """Convert form value to list."""
        if value is None:
            return []
        if isinstance(value, list):
            return [str(v).strip() for v in value if v]
        if isinstance(value, str):
            return [v.strip() for v in value.split(",") if v.strip()]
        return [str(value)]


class IntegerListConverter:
    """
    Converter for comma-separated integer lists.

    Handles conversion between list[int] and comma-separated string format.
    Used for port numbers and similar integer list fields.
    """

    def to_form(self, value: Any) -> str:
        """Convert list[int] to comma-separated string for form display."""
        if value is None:
            return ""
        if isinstance(value, list):
            return ", ".join(str(v) for v in value)
        if isinstance(value, str):
            return value
        return str(value)

    def from_form(self, value: Any) -> list[int]:
        """Convert comma-separated string from form to list[int]."""
        if value is None or value == "":
            return []
        if isinstance(value, list):
            return self._parse_int_list(value)
        if isinstance(value, str):
            return self._parse_int_list(value.split(","))
        return self._try_parse_single_int(value)

    def _parse_int_list(self, values: list[Any]) -> list[int]:
        """Parse a list of values to integers, skipping invalid ones."""
        result: list[int] = []
        for v in values:
            parsed = self._try_parse_single_int(v)
            result.extend(parsed)
        return result

    def _try_parse_single_int(self, value: Any) -> list[int]:
        """Try to parse a single value as an integer, return empty list on failure."""
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return []
        try:
            return [int(value)]
        except ValueError, TypeError:
            return []


class YAMLConverter:
    """
    Converter for YAML-formatted values.

    Converts between YAML strings and Python dicts/lists.
    """

    async def to_form(self, value: Any) -> str:
        """Convert value to YAML string for form display."""
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        try:
            import yaml

            return yaml.dump(value, default_flow_style=False)
        except ImportError:
            return str(value)

    async def from_form(self, value: Any) -> dict[str, Any] | list[Any] | str:
        """Convert YAML string from form to Python object."""
        if value is None or value == "":
            return {}
        if not isinstance(value, str):
            return value
        try:
            import yaml

            parsed = yaml.safe_load(value)
            return parsed if parsed is not None else {}
        except ImportError, yaml.YAMLError:
            return value


class KeyValueConverter:
    """
    Converter for KEY=VALUE formatted environment variables.

    Converts between KEY=VALUE string format and dict.
    """

    async def to_form(self, value: Any) -> str:
        """Convert dict to KEY=VALUE string for form display."""
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            lines = [f"{k}={v}" for k, v in value.items()]
            return "\n".join(lines)
        return str(value)

    async def from_form(self, value: Any) -> dict[str, str]:
        """Convert KEY=VALUE string from form to dict."""
        if value is None or value == "":
            return {}
        if isinstance(value, dict):
            return {str(k): str(v) for k, v in value.items()}
        if isinstance(value, str):
            result: dict[str, str] = {}
            for line in value.strip().split("\n"):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, val = line.split("=", 1)
                    result[key.strip()] = val.strip()
                elif ":" in line:
                    # Also support YAML format
                    key, val = line.split(":", 1)
                    result[key.strip()] = val.strip()
            return result
        return {}


# Registry of available converters
CONVERTER_REGISTRY: dict[str, type[Converter]] = {
    "IdentityConverter": IdentityConverter,
    "StringConverter": StringConverter,
    "IntegerConverter": IntegerConverter,
    "BooleanConverter": BooleanConverter,
    "ListConverter": ListConverter,
    "IntegerListConverter": IntegerListConverter,
    "YAMLConverter": YAMLConverter,
    "KeyValueConverter": KeyValueConverter,
}


def get_converter(name: str, **kwargs: Any) -> Converter:
    """
    Get a converter instance by name.

    Args:
        name: Converter class name
        **kwargs: Arguments to pass to converter constructor

    Returns:
        Converter instance

    Raises:
        KeyError: If converter not found in registry
    """
    if name not in CONVERTER_REGISTRY:
        raise KeyError(f"Unknown converter: {name}")
    return CONVERTER_REGISTRY[name](**kwargs)
