"""Value converters between the format a form field shows and the format that is stored."""

from typing import Any, Protocol

from opi.utils.yaml_util import YAMLError, dump_yaml_to_string, load_yaml_safe


class Converter(Protocol):
    """``to_form`` goes from storage to the form, ``from_form`` back from a submission to storage."""

    async def to_form(self, value: Any) -> Any: ...

    async def from_form(self, value: Any) -> Any: ...


class IdentityConverter:
    """The default converter, for a field that needs no transformation."""

    async def to_form(self, value: Any) -> Any:
        return value

    async def from_form(self, value: Any) -> Any:
        return value


class StringConverter:
    """``None`` becomes the empty string; ``from_form`` also strips the submitted value."""

    async def to_form(self, value: Any) -> str:
        if value is None:
            return ""
        return str(value)

    async def from_form(self, value: Any) -> str:
        if value is None:
            return ""
        return str(value).strip()


class IntegerConverter:
    """``default`` answers for an empty or unparseable submission."""

    def __init__(self, default: int = 0) -> None:
        self.default = default

    async def to_form(self, value: Any) -> str:
        if value is None:
            return ""
        return str(value)

    async def from_form(self, value: Any) -> int:
        if value is None or value == "":
            return self.default
        try:
            return int(value)
        except ValueError, TypeError:
            return self.default


class BooleanConverter:
    """A string counts as true when it reads ``true``, ``1``, ``yes`` or ``on``."""

    async def to_form(self, value: Any) -> bool:
        if value is None:
            return False
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.lower() in ("true", "1", "yes", "on")
        return bool(value)

    async def from_form(self, value: Any) -> bool:
        if value is None:
            return False
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.lower() in ("true", "1", "yes", "on")
        return bool(value)


class ListConverter:
    """A comma-separated string is split, anything else becomes a one-item list."""

    async def to_form(self, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, list):
            return [str(v) for v in value]
        if isinstance(value, str):
            return [v.strip() for v in value.split(",") if v.strip()]
        return [str(value)]

    async def from_form(self, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, list):
            return [str(v).strip() for v in value if v]
        if isinstance(value, str):
            return [v.strip() for v in value.split(",") if v.strip()]
        return [str(value)]


class IntegerListConverter:
    """Comma-separated on the form, ``list[int]`` in storage; an unparseable entry is skipped."""

    def to_form(self, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, list):
            return ", ".join(str(v) for v in value)
        if isinstance(value, str):
            return value
        return str(value)

    def from_form(self, value: Any) -> list[int]:
        if value is None or value == "":
            return []
        if isinstance(value, list):
            return self._parse_int_list(value)
        if isinstance(value, str):
            return self._parse_int_list(value.split(","))
        return self._try_parse_single_int(value)

    def _parse_int_list(self, values: list[Any]) -> list[int]:
        result: list[int] = []
        for v in values:
            parsed = self._try_parse_single_int(v)
            result.extend(parsed)
        return result

    def _try_parse_single_int(self, value: Any) -> list[int]:
        """One item, or nothing at all when the value is not an integer."""
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return []
        try:
            return [int(value)]
        except ValueError, TypeError:
            return []


class YAMLConverter:
    """YAML that does not parse comes back as the raw string, not as an error."""

    async def to_form(self, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        return dump_yaml_to_string(value)

    async def from_form(self, value: Any) -> dict[str, Any] | list[Any] | str:
        if value is None or value == "":
            return {}
        if not isinstance(value, str):
            return value
        try:
            parsed = load_yaml_safe(value)
        except YAMLError:
            return value
        return parsed if parsed is not None else {}


class KeyValueConverter:
    """Reads both ``KEY=VALUE`` and ``KEY: VALUE`` lines, and skips a line starting with ``#``."""

    async def to_form(self, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            lines = [f"{k}={v}" for k, v in value.items()]
            return "\n".join(lines)
        return str(value)

    async def from_form(self, value: Any) -> dict[str, str]:
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
                    key, val = line.split(":", 1)
                    result[key.strip()] = val.strip()
            return result
        return {}


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
    if name not in CONVERTER_REGISTRY:
        raise KeyError(f"Unknown converter: {name}")
    return CONVERTER_REGISTRY[name](**kwargs)
