"""Naming rules only this service knows, so the folder stays movable."""

from __future__ import annotations

import hashlib

from opi.utils.naming import sanitize_kubernetes_name, upstream_host

#: The operator appends this to the name of the pull secret it creates.
PULL_SECRET_POSTFIX = "robot-pull-secret"  # noqa: S105  # a secret-name suffix, not a secret

#: The username we fill in when the consumer gave none.
#:
#: Something MUST be there: a ``kubernetes.io/dockerconfigjson`` carries an ``auth`` of
#: ``base64(username:password)`` per registry, and registries refuse ``:token`` as an empty
#: username. ghcr.io does not look at the value; Docker Hub and Quay do, so there the consumer
#: has to fill it in. ``x-access-token`` is the name GitLab and GitHub use for this gap.
#: Computed at build time, never stored.
PULL_USERNAME_PLACEHOLDER = "x-access-token"

UPSTREAM_HASH_LENGTH = 8


def friendly_name(upstream: str) -> str:
    """The ``friendlyName`` for the Quay organization: the host without TLD and without dots.

    Quay forbids a dot in that field. The TLD drops out because the shared organizations on
    ODCN (``ghcr-rig``, ``code-overheid-rig``) have that same shape.
    """
    host = upstream_host(upstream).split(":", 1)[0]
    labels = host.split(".")
    stem = labels[:-1] if len(labels) > 1 else labels
    return sanitize_kubernetes_name("".join(stem))


def upstream_hash(upstream: str) -> str:
    """The distinguishing part of the suffix.

    A hash and not the readable namespace, because both parts of the suffix go through
    ``sanitize_kubernetes_name``, which turns ``.`` and ``/`` into ``-``: without a
    fixed-length part at the end the join is not reversible and project ``demo`` +
    ``ghcr.io/team`` collides with project ``demo-team`` + ``ghcr.io``. Over the WHOLE
    upstream, because ``friendly_name`` drops the TLD.
    """
    return hashlib.sha256(upstream.strip().strip("/").encode()).hexdigest()[:UPSTREAM_HASH_LENGTH]


def organization_suffix(upstream: str, project_name: str) -> str:
    """What we put on the ``Organization`` as ``spec.suffix``.

    The name the operator builds around it carries only the host, so without this part
    ``ghcr.io/orga`` and ``ghcr.io/orgb`` of the same project land on one organization.
    """
    return f"{sanitize_kubernetes_name(project_name)}-{upstream_hash(upstream)}"


def organization_name(upstream: str, customer_name: str, project_name: str) -> str:
    """``<friendlyName>-<customerName>-<suffix>``, the name the operator assembles."""
    return sanitize_kubernetes_name(
        f"{friendly_name(upstream)}-{customer_name}-{organization_suffix(upstream, project_name)}"
    )


def pull_secret_name(upstream: str, customer_name: str, project_name: str) -> str:
    """The pull secret name, set explicitly because the operator leaves the suffix out."""
    return sanitize_kubernetes_name(f"{organization_name(upstream, customer_name, project_name)}-{PULL_SECRET_POSTFIX}")


def registry_destination(upstream: str, registry_host: str, customer_name: str, project_name: str) -> str:
    """Where images from this upstream go: ``<rcr-host>/<organization>``."""
    return f"{registry_host}/{organization_name(upstream, customer_name, project_name)}"


def direct_secret_name(project_name: str, registry_name: str) -> str:
    """The dockerconfigjson secret on a cluster without proxy operator, project wide."""
    return sanitize_kubernetes_name(f"{project_name}-{registry_name}-registry")


#: How many variants we try before giving up; with more registries than this in one project
#: something other than a name collision is going on.
SLUG_ATTEMPTS = 100


def registry_slug(label: str, used: set[str]) -> str:
    """The reference belonging to a free-text label, unique within the project.

    ``REGISTRY_NAME_PATTERN`` demands a lowercase LETTER up front, so the name is never read
    as a YAML number; a label starting with a digit therefore gets an ``r``. The truncation
    after that can end on a hyphen again, which the pattern refuses on a field the consumer
    never sees. Hence the ``rstrip`` on the new boundary.
    """
    base = sanitize_kubernetes_name(label, max_length=60)
    if not base[0].isalpha():
        base = f"r{base}"[:60].rstrip("-")
    if base not in used:
        return base
    for sequence_number in range(2, SLUG_ATTEMPTS + 1):
        candidate = f"{base}-{sequence_number}"
        if candidate not in used:
            return candidate
    msg = f"Geen vrije registrynaam te maken voor label '{label}'"
    raise ValueError(msg)
