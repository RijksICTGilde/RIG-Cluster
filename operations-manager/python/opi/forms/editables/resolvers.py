"""TransientValueResolver implementations and lookup utilities.

Resolvers provide transient values for fields when their value is None.
The resolved value is used during processing but NOT persisted to YAML.

The resolver map is built from editables once and passed through context,
so any code that needs a field value can resolve it on demand without
mutating the underlying data.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from opi.forms.editables.service_path import smart_get_value

if TYPE_CHECKING:
    from opi.forms.editables.editable import TransientValueResolver
    from opi.forms.visualizers.visualizer import EditableVisualizer


def build_resolver_map(editables: list[EditableVisualizer]) -> dict[str, TransientValueResolver]:
    """Build a {yaml_path: resolver} map from all editables, recursing into children."""
    resolvers: dict[str, TransientValueResolver] = {}
    for vis in editables:
        ed = vis.editable
        if ed.transient_value_when_none:
            resolvers[ed.yaml_path] = ed.transient_value_when_none
        if vis.children:
            resolvers.update(build_resolver_map(vis.children))
    return resolvers


def get_effective_value(
    yaml_data: dict[str, Any],
    path: str,
    resolvers: dict[str, TransientValueResolver] | None = None,
) -> Any:
    """Get a field value, resolving transient defaults when the value is None.

    Looks up the value at *path* in yaml_data. If None and a resolver exists
    for that path, resolves the transient value on demand. Never mutates yaml_data.
    """
    value = smart_get_value(yaml_data, path)
    if value is not None or not resolvers:
        return value
    resolver = resolvers.get(path)
    if resolver:
        return resolver.resolve(yaml_data)
    return None


class ClusterDefaultDomain:
    """Resolves the first supported base domain for the current cluster.

    Used on the base-domain select field: when the user hasn't explicitly
    selected a domain, this provides the cluster's default domain for
    dependent field evaluation (show_when conditions, enforcers, etc.).
    """

    def resolve(self, yaml_data: dict[str, Any]) -> Any:
        from opi.connectors.subdomain import get_supported_base_domains
        from opi.core.config import settings

        supported = get_supported_base_domains(settings.CLUSTER_MANAGER)
        if supported:
            return next(iter(supported))
        return None
