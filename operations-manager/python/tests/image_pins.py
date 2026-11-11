"""De container-images zoals ``infrastructure/`` en ``bootstrap/`` ze pinnen.

Een test die een container start, start de versie die productie draait. De pin staat in het
manifest, dus die leest deze module daar; een tag in een testbestand drijft stil af van wat
er uitgerold is. ``tests/test_test_images_follow_pins.py`` bewaakt dat.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]

#: De mappen waar een pin mag staan: wat hier staat, is wat er uitgerold wordt.
PIN_ROOTS = ("infrastructure", "bootstrap")

#: De velden waarin een manifest een image noemt. CNPG gebruikt ``imageName``.
IMAGE_FIELDS = ("image", "imageName")


def _images_in(document: object) -> list[str]:
    found: list[str] = []
    if isinstance(document, dict):
        for key, value in document.items():
            if key in IMAGE_FIELDS and isinstance(value, str) and value:
                found.append(value)
            else:
                found.extend(_images_in(value))
    elif isinstance(document, list):
        for item in document:
            found.extend(_images_in(item))
    return found


def images_in_manifest(manifest: str | Path) -> list[str]:
    """Elke image die dit manifest noemt, in de volgorde waarin hij er staat."""
    path = Path(manifest)
    if not path.is_absolute():
        path = REPO_ROOT / path
    found: list[str] = []
    for document in yaml.safe_load_all(path.read_text()):
        found.extend(_images_in(document))
    return found


def pinned_image(manifest: str | Path, *, contains: str | None = None) -> str:
    """De image die dit manifest pint.

    Args:
        contains: nodig zodra het manifest meer dan een image noemt; kiest de enige die
            deze tekst bevat.
    """
    found = [image for image in images_in_manifest(manifest) if contains is None or contains in image]
    unique = sorted(set(found))
    if len(unique) != 1:
        raise ValueError(f"{manifest} noemt {len(unique)} images{f' met {contains!r}' if contains else ''}: {unique}")
    return unique[0]


@cache
def pinned_images() -> dict[str, list[str]]:
    """Elke gepinde image onder ``infrastructure/`` en ``bootstrap/``, met waar hij staat."""
    found: dict[str, list[str]] = {}
    for root in PIN_ROOTS:
        for path in sorted((REPO_ROOT / root).rglob("*.yaml")):
            try:
                images = images_in_manifest(path)
            except yaml.YAMLError:
                # Een template met Kustomize- of SOPS-syntax is geen manifest met een pin.
                continue
            for image in images:
                found.setdefault(image, []).append(str(path.relative_to(REPO_ROOT)))
    return found


def base_image_from_dockerfile(dockerfile: str | Path) -> str:
    """De image van de LAATSTE ``FROM`` in dit Dockerfile: de basis van het resultaat."""
    path = Path(dockerfile)
    if not path.is_absolute():
        path = REPO_ROOT / path
    froms = [
        line.split()[1]
        for line in path.read_text().splitlines()
        if line.strip().upper().startswith("FROM ") and not line.strip().split()[1].startswith("--")
    ]
    if not froms:
        raise ValueError(f"{dockerfile} heeft geen FROM")
    return froms[-1]
