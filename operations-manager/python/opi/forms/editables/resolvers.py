"""TransientValueResolver implementations and lookup utilities.

Resolvers provide transient values for fields when their value is None.
The resolved value is used during processing but NOT persisted to YAML.

The resolver map is built from editables once and passed through context,
so any code that needs a field value can resolve it on demand without
mutating the underlying data.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from opi.core import config as opi_config
from opi.core.cluster_config import get_ingress_postfix
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
    """Resolves the cluster's default (ingress) base domain.

    Used on the base-domain select field: when the user picks "Cluster
    standaard" (the empty option) the value is None, and dependent fields
    (show_when conditions, enforcers) need to know the effective domain.

    Returns the cluster's default ingress domain — NOT the first nice-URL
    domain. ``DomainNeedsRequestCondition`` compares the effective base
    domain against exactly this value to decide the domain is already the
    default (no request needed); returning a nice-URL domain instead made
    the request checkbox appear for the cluster default and let the hook
    materialise that domain into the saved project file.
    """

    def resolve(self, yaml_data: dict[str, Any]) -> Any:
        # Raises ValueError for an unknown cluster — a misconfigured
        # CLUSTER_MANAGER is a deployment error that must surface, not be
        # silently swallowed.
        postfix = get_ingress_postfix(opi_config.settings.CLUSTER_MANAGER)
        return postfix.lstrip(".") if postfix else None
