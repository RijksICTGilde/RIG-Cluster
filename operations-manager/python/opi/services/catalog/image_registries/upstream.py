"""Van wat een afnemer plakt naar de upstream die wij nodig hebben.

Wat we niet herkennen laten we met rust op de generieke bewerkingen na (protocol eraf,
kleine letters, geen afsluitende schuine streep), zodat een onbekende vorm door het patroon
wordt afgewezen in plaats van stil verminkt.
"""

from __future__ import annotations

import re
from typing import Any

#: Waar GitHub-packages werkelijk staan; ``github.com`` is de BROWSER en geen registry.
GITHUB_REGISTRY = "ghcr.io"
#: Idem voor Docker Hub en voor GitLab.com.
DOCKER_HUB_REGISTRY = "docker.io"
DOCKER_HUB_OFFICIAL_NAMESPACE = "library"
GITLAB_REGISTRY = "registry.gitlab.com"

_PROTOCOL = re.compile(r"^(?:https?|ssh|git)://")
#: Een tag of digest achter de laatste padcomponent. Alleen DIT onderscheidt een volledige
#: image-referentie van een upstream met een pad: ``code.overheid.nl/team`` is een
#: upstream, ``code.overheid.nl/team/app:1`` is een image.
_TAG_OR_DIGEST = re.compile(r"(?::[^/:@]+|@sha256:[0-9a-f]+)$")


def normalize_upstream(value: str) -> str:
    """De upstream die bij *value* hoort, of *value* zelf als we de vorm niet kennen."""
    text = _PROTOCOL.sub("", value.strip().lower())
    # Userinfo (``git@host/x``) blijft bewust staan: wegknippen zou een upstream opleveren
    # die er geldig uitziet maar ergens anders heen wijst.
    text = text.split("?", 1)[0].split("#", 1)[0].strip("/")
    if not text:
        return text

    host, _, path = text.partition("/")
    segments = [segment for segment in path.split("/") if segment]

    for rewrite in (_github, _docker_hub, _gitlab, _forgejo_packages):
        rewritten = rewrite(host, segments)
        if rewritten is not None:
            return rewritten
    return _without_image_reference(host, segments)


def _github(host: str, segments: list[str]) -> str | None:
    """``github.com/orgs/<org>/packages`` -> ``ghcr.io/<org>``.

    Ook de gebruikersvariant en de pagina van een enkel package
    (``github.com/<owner>/<repo>/pkgs/container/<image>``): de ghcr-namespace is in alle
    drie de gevallen de EIGENAAR, niet de repository.
    """
    if host != "github.com" or not segments:
        return None
    if segments[0] in ("orgs", "users") and len(segments) >= 2:
        return f"{GITHUB_REGISTRY}/{segments[1]}"
    if "pkgs" in segments:
        return f"{GITHUB_REGISTRY}/{segments[0]}"
    return None


def _docker_hub(host: str, segments: list[str]) -> str | None:
    """``hub.docker.com/r/<owner>/<repo>`` -> ``docker.io/<owner>``.

    ``_`` is de plek van de officiele images, en die staan in ``library``.
    """
    if host != "hub.docker.com" or not segments:
        return None
    if segments[0] == "_":
        return f"{DOCKER_HUB_REGISTRY}/{DOCKER_HUB_OFFICIAL_NAMESPACE}"
    if segments[0] in ("r", "u", "repository") and len(segments) >= 2:
        owner = segments[2] if segments[1] == "docker" and len(segments) >= 3 else segments[1]
        return f"{DOCKER_HUB_REGISTRY}/{owner}"
    return None


def _gitlab(host: str, segments: list[str]) -> str | None:
    """``gitlab.com/<groep>/<project>/container_registry`` -> ``registry.gitlab.com/<groep>/<project>``.

    Alleen voor gitlab.com. Bij een eigen GitLab is de registryhost een installatiekeuze
    die wij niet kunnen weten, en een gok zou een upstream opleveren die nergens bestaat.
    """
    if host != "gitlab.com" or "container_registry" not in segments:
        return None
    project = segments[: segments.index("container_registry")]
    # ``/-/container_registry`` is de moderne vorm; dat scheidingsteken hoort niet in het pad.
    project = [segment for segment in project if segment != "-"]
    return "/".join([GITLAB_REGISTRY, *project]) if project else None


def _forgejo_packages(host: str, segments: list[str]) -> str | None:
    """``code.overheid.nl/robbert/-/packages`` -> ``code.overheid.nl/robbert``.

    Forgejo en Gitea zetten ``/-/`` tussen de eigenaar en wat je van hem bekijkt, dus alles
    vanaf dat teken is browserruis.
    """
    if "-" not in segments:
        return None
    owner = segments[: segments.index("-")]
    return "/".join([host, *owner]) if owner else host


def _without_image_reference(host: str, segments: list[str]) -> str:
    """Een volledige image-referentie terug naar de prefix waar hij onder valt.

    ``code.overheid.nl/team/app:1.2`` -> ``code.overheid.nl/team``: de tag valt weg en
    daarmee ook de laatste padcomponent, want dat is de naam van de repository en niet van
    de namespace. Zonder tag of digest raken we het pad niet aan.
    """
    if not segments or not _TAG_OR_DIGEST.search(segments[-1]):
        return "/".join([host, *segments])
    return "/".join([host, *segments[:-1]])


def upstream_from_project_images(project_data: dict[str, Any]) -> str | None:
    """De upstream die uit de images van dit project volgt, of None als hij niet volgt.

    Alleen als het antwoord eenduidig is: bij meer dan een prefix is de vraag juist het
    punt. Een image zonder host (``nginx:alpine``) telt
    niet mee: die staat op Docker Hub en daar heb je geen eigen registry voor nodig.
    """
    prefixes = {
        prefix
        for image in _project_images(project_data)
        if (prefix := normalize_upstream(image)) and "." in prefix.split("/", 1)[0]
    }
    return prefixes.pop() if len(prefixes) == 1 else None


def _project_images(project_data: dict[str, Any]) -> list[str]:
    """Elke image die in dit projectbestand staat, op een component of op een deployment."""
    dragers = list(project_data.get("components", []) or [])
    for deployment in project_data.get("deployments", []) or []:
        if isinstance(deployment, dict):
            dragers.extend(deployment.get("components", []) or [])
    return [
        component["image"]
        for component in dragers
        if isinstance(component, dict) and isinstance(component.get("image"), str)
    ]
