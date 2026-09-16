"""Eén regelvorm voor het oplossen van een image, gevoed door twee bronnen.

De clustertabel en een private registry doen dezelfde bewerking, alleen met een kortere
of langere ``match``. "Wie wint" is daarmee een eerste-match op één lijst, met de
projectregels vooraan.
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
    """Eén regel: welke upstream-prefix gaat waarheen, en met welk pull-secret."""

    #: Upstream-prefix, met of zonder namespace-segment.
    match: str
    #: Bestemming: host plus organisatie.
    to: str
    secret: str


@dataclass(frozen=True)
class ResolvedImage:
    """Wat er van een image geworden is, en welk secret erbij hoort."""

    image: str
    secret: str | None


def normalize_image(image: str) -> str:
    """Vul de impliciete Docker Hub-delen aan: ``nginx:alpine`` -> ``docker.io/library/nginx:alpine``.

    De admission-webhook van ODCN behandelt zo'n korte naam ook zo; zonder dezelfde
    normalisatie mist de image hier zijn secret.
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
    """Hetzelfde voor een upstream-PREFIX: alleen de registry ervoor, geen ``library``.

    Een prefix is een pad en geen repositorynaam, dus het aanvullen dat
    ``normalize_image`` doet zou van ``ghcr.io`` een pad maken dat nooit matcht.
    """
    if not prefix:
        return prefix

    first, _, _ = prefix.partition("/")
    if _looks_like_host(first):
        return prefix
    return f"{DEFAULT_REGISTRY}/{prefix}"


def _looks_like_host(segment: str) -> bool:
    """Of een eerste padsegment een registry-host is in plaats van een pad-segment."""
    return "." in segment or ":" in segment or segment == "localhost"


def _has_prefix(image: str, prefix: str) -> bool:
    """Of ``image`` onder ``prefix`` valt, op segmentgrens en niet op tekens."""
    return image == prefix or image.startswith(prefix + "/")


def resolve_image(image: str, rules: Sequence[RegistryRule]) -> ResolvedImage:
    """Los een image op tegen de regellijst: eerste match wint.

    Een image die al op de bestemming staat wordt niet herschreven maar krijgt wel het
    secret van die regel. Zonder match komt de ONGENORMALISEERDE verwijzing terug, zodat
    een cluster zonder tabel niets aan een manifest verandert.
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
    """De omgekeerde weg, voor de weergavekant: van bestemming terug naar upstream."""
    for rule in rules:
        if _has_prefix(image, rule.to):
            return rule.match + image[len(rule.to) :]
    return image
