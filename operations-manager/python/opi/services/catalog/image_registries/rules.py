"""Eén regelvorm voor het oplossen van een image, gevoed door twee bronnen.

De clustertabel (de gedeelde proxy-caches die het platform aanbiedt) en een private
registry van een project doen bijna hetzelfde: een upstream-prefix wordt vervangen door
een bestemming, en bij die bestemming hoort een pull-secret. Het namespace-segment dat
bij de privévariant wegvalt is geen apart gedrag; het volgt uit een langere ``match``::

    gedeeld:  match code.overheid.nl             -> to rcr.rijksapps.nl/code-overheid-rig
              code.overheid.nl/robbert/demo:tag  => rcr.rijksapps.nl/code-overheid-rig/robbert/demo:tag

    privé:    match code.overheid.nl/robbert     -> to rcr.rijksapps.nl/codeoverheid-rig-<project>
              code.overheid.nl/robbert/demo:tag  => rcr.rijksapps.nl/codeoverheid-rig-<project>/demo:tag

Daarom is er één regelvorm en één functie, en is "wie wint" een gewone eerste-match op
één lijst: de projectregels staan vooraan, dus de eigen registry van een project wint van
de gedeelde proxy voor dezelfde upstream. Volgorde is data, geen code.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

#: Wat een image zonder host krijgt voorgezet. Docker Hub is de enige registry waarvoor
#: een kale naam een geldige verwijzing is, en de admission-webhook van ODCN behandelt
#: ``nginx:alpine`` ook zo -- gemeten: die maakt er ``rcr.rijksapps.nl/dockerhub-rig/
#: library/nginx`` van. Zonder dezelfde normalisatie mist zo'n image hier zijn secret.
DEFAULT_REGISTRY = "docker.io"
#: Het pad-segment dat Docker Hub zelf voor een naam zonder namespace invult.
DEFAULT_NAMESPACE = "library"


@dataclass(frozen=True)
class RegistryRule:
    """Eén regel: welke upstream-prefix gaat waarheen, en met welk pull-secret."""

    #: Upstream-prefix, met of zonder namespace-segment (``code.overheid.nl``,
    #: ``code.overheid.nl/robbert.uittenbroek``).
    match: str
    #: Bestemming: host plus organisatie (``rcr.rijksapps.nl/codeoverheid-rig-demo``).
    to: str
    #: Het pull-secret dat bij die bestemming hoort.
    secret: str


@dataclass(frozen=True)
class ResolvedImage:
    """Wat er van een image geworden is, en welk secret erbij hoort."""

    image: str
    secret: str | None


def normalize_image(image: str) -> str:
    """Vul de impliciete Docker Hub-delen aan: ``nginx:alpine`` -> ``docker.io/library/nginx:alpine``.

    Alleen de VERWIJZING wordt aangevuld, en alleen waar Docker dat zelf ook doet: een
    eerste segment zonder punt, zonder dubbele punt en niet ``localhost`` is geen host maar
    een pad, dus er staat geen registry in de verwijzing.
    """
    if not image:
        return image

    first, separator, rest = image.partition("/")
    if not separator or not _looks_like_host(first):
        # Geen host in de verwijzing: het hele ding is een pad op Docker Hub.
        path = image
        if "/" not in path:
            path = f"{DEFAULT_NAMESPACE}/{path}"
        return f"{DEFAULT_REGISTRY}/{path}"

    return image


def normalize_prefix(prefix: str) -> str:
    """Hetzelfde voor een upstream-PREFIX, met het verschil dat een prefix geen naam is.

    ``normalize_image`` vult voor een verwijzing zonder host ``docker.io/library/`` aan,
    want ``nginx`` is daar de NAAM van een repository. Een prefix is een PAD: ``ghcr.io``
    is al compleet, en er ``docker.io/library/`` voor zetten maakt er een repository van
    die nergens bij past -- gemeten: ``normalize_image('ghcr.io')`` geeft
    ``docker.io/library/ghcr.io``, dus een upstream zonder pad matchte nooit een image.

    Een eerste segment dat er als host uitziet is dus klaar. Staat er geen host, dan is het
    een pad op Docker Hub en gaat alleen de registry ervoor -- geen ``library``, want een
    prefix noemt een namespace en geen image.
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
    """Of ``image`` onder ``prefix`` valt -- op segmentgrens, niet op tekens.

    Zonder de grens zou ``code.overheid.nl-anders/x`` onder ``code.overheid.nl`` vallen.
    """
    return image == prefix or image.startswith(prefix + "/")


def resolve_image(image: str, rules: Sequence[RegistryRule]) -> ResolvedImage:
    """Los een image op tegen de regellijst: eerste match wint.

    Drie dingen die hier horen en nergens anders:

    1. **Normalisatie van korte namen**, zodat ``nginx:alpine`` de ``docker.io``-regel
       raakt (zie ``normalize_image``).
    2. **Een image die al op de bestemming staat** wordt niet herschreven, maar krijgt wel
       het secret van die regel. Dat is het dp-bn7-geval, en het is hier één regel code in
       plaats van een aparte uitzondering.
    3. **Geen match betekent image ongewijzigd en geen secret.** De ONGENORMALISEERDE
       verwijzing komt terug: op een cluster zonder tabel verandert er dan niets aan een
       gegenereerd manifest.
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
    """De omgekeerde weg: van een bestemming terug naar de upstream die de afnemer kent.

    De weergavekant (diagnostiek, logregels, event-uitleg) toont een gebruiker zijn eigen
    registry in plaats van de kale proxy-URL. Zonder match komt de invoer ongewijzigd terug.
    """
    for rule in rules:
        if _has_prefix(image, rule.to):
            return rule.match + image[len(rule.to) :]
    return image
