"""From what a consumer pastes to the upstream we need.

What we do not recognise is left alone apart from the generic operations (protocol off,
lowercase, no trailing slash), so an unknown shape is rejected by the pattern instead of
silently mangled.
"""

from __future__ import annotations

import re
from typing import Any

#: Where GitHub packages really live; ``github.com`` is the BROWSER and not a registry.
GITHUB_REGISTRY = "ghcr.io"
#: The same for Docker Hub and for GitLab.com.
DOCKER_HUB_REGISTRY = "docker.io"
DOCKER_HUB_OFFICIAL_NAMESPACE = "library"
GITLAB_REGISTRY = "registry.gitlab.com"

_PROTOCOL = re.compile(r"^(?:https?|ssh|git)://")
#: A tag or digest behind the last path component. THIS is the only thing separating a full
#: image reference from an upstream with a path: ``code.overheid.nl/team`` is an upstream,
#: ``code.overheid.nl/team/app:1`` is an image.
_TAG_OR_DIGEST = re.compile(r"(?::[^/:@]+|@sha256:[0-9a-f]+)$")


def normalize_upstream(value: str) -> str:
    """The upstream belonging to *value*, or *value* itself when we do not know the shape."""
    # A pasted pull command (``docker pull [flags] <image>``) ends with the image.
    words = value.split()
    text = _PROTOCOL.sub("", (words[-1] if words else "").lower())
    # Userinfo (``git@host/x``) is deliberately kept: cutting it off would yield an upstream
    # that looks valid but points somewhere else.
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

    The user variant and the page of a single package too
    (``github.com/<owner>/<repo>/pkgs/container/<image>``): in all three cases the ghcr
    namespace is the OWNER, not the repository.
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

    ``_`` is where the official images live, and those sit in ``library``.
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
    """``gitlab.com/<group>/<project>/container_registry`` -> ``registry.gitlab.com/<group>/<project>``.

    For gitlab.com only. On a self-hosted GitLab the registry host is an installation choice we
    cannot know, and a guess would yield an upstream that exists nowhere.
    """
    if host != "gitlab.com" or "container_registry" not in segments:
        return None
    project = segments[: segments.index("container_registry")]
    # ``/-/container_registry`` is the modern shape; that separator does not belong in the path.
    project = [segment for segment in project if segment != "-"]
    return "/".join([GITLAB_REGISTRY, *project]) if project else None


def _forgejo_packages(host: str, segments: list[str]) -> str | None:
    """``code.overheid.nl/robbert/-/packages`` -> ``code.overheid.nl/robbert``.

    Forgejo and Gitea put ``/-/`` between the owner and what you are looking at, so everything
    from that marker on is browser noise.
    """
    if "-" not in segments:
        return None
    owner = segments[: segments.index("-")]
    return "/".join([host, *owner]) if owner else host


def _without_image_reference(host: str, segments: list[str]) -> str:
    """A full image reference back to the prefix it falls under.

    ``code.overheid.nl/team/app:1.2`` -> ``code.overheid.nl/team``: the tag drops out and with
    it the last path component, because that is the name of the repository and not of the
    namespace. Without a tag or digest the path is left untouched.
    """
    if not segments or not _TAG_OR_DIGEST.search(segments[-1]):
        return "/".join([host, *segments])
    return "/".join([host, *segments[:-1]])


def upstream_from_project_images(project_data: dict[str, Any]) -> str | None:
    """The upstream that follows from this project's images, or None when it does not.

    Only when the answer is unambiguous: with more than one prefix the question is the point.
    An image without a host (``nginx:alpine``) does not count: it lives on Docker Hub, and no
    private registry is needed for that.
    """
    prefixes = {
        prefix
        for image in _project_images(project_data)
        if (prefix := normalize_upstream(image)) and "." in prefix.split("/", 1)[0]
    }
    return prefixes.pop() if len(prefixes) == 1 else None


def _project_images(project_data: dict[str, Any]) -> list[str]:
    """Every image in this project file, on a component or on a deployment."""
    carriers = list(project_data.get("components", []) or [])
    for deployment in project_data.get("deployments", []) or []:
        if isinstance(deployment, dict):
            carriers.extend(deployment.get("components", []) or [])
    return [
        component["image"]
        for component in carriers
        if isinstance(component, dict) and isinstance(component.get("image"), str)
    ]
