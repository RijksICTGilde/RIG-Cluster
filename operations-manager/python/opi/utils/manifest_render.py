"""The shared Jinja environment the manifest templates render through.

Here and not in :mod:`opi.generation.manifests`: the kubectl connector renders a manifest
that arrives as text, and a second ``Environment()`` next to this one is a second place to
forget a filter.
"""

import functools
from typing import Any

from jinja2 import Environment, FileSystemLoader, Template

from opi.core.config import settings
from opi.utils.yaml_util import dump_yaml_double_quoted_scalar


def yaml_scalar(value: object) -> str:
    """Render ``value`` as a YAML double-quoted scalar that survives a round-trip.

    Tenant values reach a Secret manifest verbatim, so the quoting has to hold for every
    legal string. Do not replace this with manual ``"{{ value }}"`` quoting (a backslash
    becomes an invalid YAML escape and SOPS refuses the whole file) nor with ``| tojson``
    (an astral character becomes a surrogate pair, which SOPS rejects).

    Only a ``str``. A non-string here is a template fault: every optional value sits in an
    ``{% if %}``, so a ``None`` or a missing variable means that guard is absent. The
    emitter would otherwise write ``!!null "null"`` or ``!!int "5"`` into a manifest
    unnoticed.
    """
    if not isinstance(value, str):
        raise TypeError(
            f"yaml_scalar verwacht een str, kreeg {type(value).__name__}. Een ontbrekende of "
            "optionele sjabloonvariabele hoort achter een if-blok, niet door dit filter."
        )
    return dump_yaml_double_quoted_scalar(value)


#: The filters every manifest template may use. A second environment (a test that wants
#: ``StrictUndefined``) registers them from here instead of retyping the list.
MANIFEST_FILTERS: dict[str, Any] = {"yaml_scalar": yaml_scalar}


@functools.cache
def manifest_environment() -> Environment:
    """The shared Jinja2 environment for all manifest templates.

    The FileSystemLoader is there for ``{% include %}`` of sidecar templates.
    ``autoescape=False`` is what a YAML template needs.
    """
    env = Environment(
        loader=FileSystemLoader(settings.MANIFESTS_PATH),
        trim_blocks=True,
        lstrip_blocks=True,
        # Keeps the trailing newline of an included partial, or the next line sticks to it.
        keep_trailing_newline=True,
        autoescape=False,  # noqa: S701  # output is YAML, escaping goes through yaml_scalar
    )
    env.filters.update(MANIFEST_FILTERS)
    return env


@functools.lru_cache(maxsize=256)
def compiled_template(source: str) -> Template:
    """A compiled template, cached on the source text.

    The cache key is the text, not the path: a changed file on disk yields different text
    and so a fresh compilation.
    """
    return manifest_environment().from_string(source)


def render_template_source(source: str, variables: dict[str, Any]) -> str:
    """Render manifest source through the shared environment.

    The only render point for a template that arrives as TEXT instead of as a filename; an
    ``Environment()`` of its own next to this one is a second place to forget a filter.
    """
    return compiled_template(source).render(**variables)
