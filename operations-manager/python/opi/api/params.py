"""Shared declarations for the parameters that recur across the API.

Measured on 2026-08-04: the OpenAPI document carried 178 parameters of which 32 had a
description. The distribution is extremely concentrated -- ``project_name`` alone appears
84 times (7 described), ``deployment_name`` 26 times (none described), ``component_name``
14 times (none) -- so five names cover 134 of the 178.

Writing that text at every endpoint would mean 134 chances to phrase it differently, and a
generated client shows whichever variant it happened to hit. One definition per concept
keeps the meaning in a single place, and an endpoint that needs to say something extra can
still do so on top.

Each name comes in the flavours it actually occurs in: ``project_name`` is a path segment
on most endpoints but a query parameter on the restore endpoints, and the two cannot share
one declaration.
"""

from typing import Annotated

from fastapi import Path, Query
from opi.utils.patterns import DNS1123_LABEL, DNS1123_SUBDOMAIN

#: A project's technical name, as used in the project file and in every path.
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

#: Path segments that name a Kubernetes resource end up in a generated manifest and, for the
#: restore endpoints, in the Kopia repository key. The pattern is what Kubernetes itself
#: accepts, so a value that could never name a resource is refused with a 422 instead of
#: travelling onwards.
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

#: A PVC name or a backup reference name. Both are DNS-1123 subdomains: a PVC name is a
#: Kubernetes object name, and a reference is either a component service reference or
#: ``{deployment}-{service prefix}`` (see ``_fallback_reference_names``), so neither
#: carries an underscore or any other character outside this set.
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
