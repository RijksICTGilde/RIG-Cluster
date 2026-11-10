"""Shared declarations for the parameters that recur across the API.

A parameter that several endpoints take is described here once, so a generated client does
not get a different wording per endpoint. An endpoint that needs to say something extra
still can, on top of this. Each name comes in the flavours it occurs in: ``project_name`` is
a path segment on most endpoints but a query parameter on the restore endpoints, and Path
and Query cannot share one declaration.
"""

from typing import Annotated

from fastapi import Path, Query

from opi.utils.patterns import DNS1123_LABEL, DNS1123_SUBDOMAIN

_PROJECT_NAME_DESCRIPTION = (
    "Technical name of the project, as it appears in the project file (e.g. 'wies'). Not the display name."
)
_DEPLOYMENT_NAME_DESCRIPTION = (
    "Name of the deployment within the project, as declared under deployments[].name (e.g. 'production')."
)
_COMPONENT_NAME_DESCRIPTION = "Name of the component, matching a components[].name in the project file (e.g. 'web')."

ProjectNamePath = Annotated[str, Path(description=_PROJECT_NAME_DESCRIPTION)]
ProjectNameQuery = Annotated[str, Query(description=_PROJECT_NAME_DESCRIPTION)]

DeploymentNamePath = Annotated[str, Path(description=_DEPLOYMENT_NAME_DESCRIPTION)]
DeploymentNameQuery = Annotated[str, Query(description=_DEPLOYMENT_NAME_DESCRIPTION)]

ComponentNamePath = Annotated[str, Path(description=_COMPONENT_NAME_DESCRIPTION)]

#: These segments end up in a generated manifest and, on the restore endpoints, in the Kopia
#: repository key, so the pattern is what Kubernetes itself accepts: a value that could never
#: name a resource is refused with a 422 instead of travelling onwards.
ClusterPath = Annotated[
    str,
    Path(
        description=(
            "Cluster this resource lives on (e.g. 'odcn-production'). Each Operations Manager "
            "instance only serves its own cluster."
        ),
        pattern=DNS1123_LABEL,
        max_length=63,
    ),
]

NamespacePath = Annotated[
    str,
    Path(
        description=(
            "Kubernetes namespace, which for a project deployment follows rig-{prefix}-{project}. "
            "Must belong to the project the API key authenticates."
        ),
        pattern=DNS1123_LABEL,
        max_length=63,
    ),
]

#: A PVC name or a backup reference name. A reference is either a component service reference
#: or ``{deployment}-{service prefix}`` (see ``_fallback_reference_names``), so like a PVC name
#: it is a DNS-1123 subdomain.
ResourceNamePath = Annotated[
    str,
    Path(
        description="Name of the Kubernetes resource or backup reference, as a DNS-1123 subdomain.",
        pattern=DNS1123_SUBDOMAIN,
        max_length=253,
    ),
]

TaskIdPath = Annotated[str, Path(description="Identifier of an async task, as returned in the 202 response.")]

LimitQuery = Annotated[int, Query(description="Maximum number of items to return.")]
OffsetQuery = Annotated[int, Query(description="Number of items to skip before returning results.")]
