"""One rule shape for resolving an image, fed by two sources.

The cluster table and a private registry perform the same operation, only with a shorter or
longer ``match``. "Who wins" is therefore a first match on one list, with the project rules up
front.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

DEFAULT_REGISTRY = "docker.io"
DEFAULT_NAMESPACE = "library"


@dataclass(frozen=True)
class RegistryRule:
    """One rule: which upstream prefix goes where, and with which pull secret."""

    #: Upstream prefix, with or without a namespace segment.
    match: str
    #: Destination: host plus organization.
    to: str
    secret: str


@dataclass(frozen=True)
class ResolvedImage:
    """What became of an image, and which secret belongs to it."""

    image: str
    secret: str | None


def normalize_image(image: str) -> str:
    """Fill in the implicit Docker Hub parts: ``nginx:alpine`` -> ``docker.io/library/nginx:alpine``.

    The ODCN admission webhook treats such a short name the same way; without the same
    normalisation the image misses its secret here.
    """
    if not image:
        return image

    first, separator, rest = image.partition("/")
    if not separator or not _looks_like_host(first):
        path = image
        if "/" not in path:
            path = f"{DEFAULT_NAMESPACE}/{path}"
        return f"{DEFAULT_REGISTRY}/{path}"

    return image


def normalize_prefix(prefix: str) -> str:
    """The same for an upstream PREFIX: only the registry in front, no ``library``.

    A prefix is a path and not a repository name, so the filling in ``normalize_image`` does
    would turn ``ghcr.io`` into a path that never matches.
    """
    if not prefix:
        return prefix

    first, _, _ = prefix.partition("/")
    if _looks_like_host(first):
        return prefix
    return f"{DEFAULT_REGISTRY}/{prefix}"


def _looks_like_host(segment: str) -> bool:
    """Whether a first path segment is a registry host rather than a path segment."""
    return "." in segment or ":" in segment or segment == "localhost"


def _has_prefix(image: str, prefix: str) -> bool:
    """Whether ``image`` falls under ``prefix``, on a segment boundary and not on characters."""
    return image == prefix or image.startswith(prefix + "/")


def resolve_image(image: str, rules: Sequence[RegistryRule]) -> ResolvedImage:
    """Resolve an image against the rule list: first match wins.

    An image already at the destination is not rewritten but does get that rule's secret.
    Without a match the UNNORMALISED reference comes back, so a cluster without a table
    changes nothing in a manifest.
    """
    if not image:
        return ResolvedImage(image, None)

    normalized = normalize_image(image)
    for rule in rules:
        if _has_prefix(normalized, rule.to):
            return ResolvedImage(image, rule.secret)
        if _has_prefix(normalized, rule.match):
            return ResolvedImage(rule.to + normalized[len(rule.match) :], rule.secret)

    return ResolvedImage(image, None)


def original_image(image: str, rules: Sequence[RegistryRule]) -> str:
    """The way back, for the display side: from destination to upstream."""
    for rule in rules:
        if _has_prefix(image, rule.to):
            return rule.match + image[len(rule.to) :]
    return image
