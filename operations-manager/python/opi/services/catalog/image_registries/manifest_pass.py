"""De manifestpas: los elke image in een podspec op en hang er het juiste secret aan.

Een wandeling en geen tweede mechanisme: sidecars, backup-, restore-, db-console- en
jobpods lopen niet door de componentlus, maar gebruiken hier dezelfde regels en dezelfde
``resolve_image()``. Idempotent, want een al opgeloste image staat al op zijn bestemming.

Lezen en schrijven gaat door ``opi/utils/yaml_util.py``, de enige schrijver: die houdt
commentaar en meerregelige strings (een literal block) in stand. Een manifest waaraan
niets is opgelost wordt niet aangeraakt.
"""

from __future__ import annotations

import glob
import logging
import os
from typing import TYPE_CHECKING, Any

from opi.services.catalog.image_registries.rules import resolve_image
from opi.utils.yaml_util import dump_yaml_documents_to_string, load_yaml_documents_from_string

if TYPE_CHECKING:
    from collections.abc import Sequence

    from opi.services.catalog.image_registries.rules import RegistryRule

logger = logging.getLogger(__name__)

#: Kinds die een podspec op ``spec.template.spec`` dragen.
_POD_TEMPLATE_KINDS = {"Deployment", "StatefulSet", "DaemonSet", "Job"}
_CRONJOB_KIND = "CronJob"

_SKIP_SUFFIXES = (".sops.yaml", ".to-sops.yaml")
_SKIP_NAMES = ("kustomization.yaml", "decrypt-sops.yaml")


def pod_spec_of(manifest: dict[str, Any]) -> dict[str, Any] | None:
    """De podspec in dit manifest, of None als het er geen draagt."""
    kind = manifest.get("kind", "")
    if kind == "Pod":
        spec = manifest.get("spec")
        return spec if isinstance(spec, dict) else None
    if kind in _POD_TEMPLATE_KINDS:
        spec = manifest.get("spec", {}).get("template", {}).get("spec")
        return spec if isinstance(spec, dict) else None
    if kind == _CRONJOB_KIND:
        spec = manifest.get("spec", {}).get("jobTemplate", {}).get("spec", {}).get("template", {}).get("spec")
        return spec if isinstance(spec, dict) else None
    return None


def apply_rules(manifest: dict[str, Any], rules: Sequence[RegistryRule]) -> dict[str, Any]:
    """Los elke container-image in dit manifest op en zet zijn secret erbij.

    Muteert en retourneert hetzelfde manifest.
    """
    _apply_rules(manifest, rules)
    return manifest


def _apply_rules(manifest: dict[str, Any], rules: Sequence[RegistryRule]) -> bool:
    """De pas zelf. True als er aan dit manifest iets is veranderd.

    De schrijvers hieronder hebben dat antwoord nodig: alleen een manifest dat echt is
    opgelost wordt opnieuw geschreven, de rest komt letterlijk terug zoals hij binnenkwam.
    """
    pod_spec = pod_spec_of(manifest)
    if pod_spec is None or not rules:
        return False

    changed = False
    secrets: list[str] = []
    for container_list in ("containers", "initContainers"):
        for container in pod_spec.get(container_list, []) or []:
            if not isinstance(container, dict):
                continue
            image = container.get("image", "")
            if not isinstance(image, str) or not image:
                continue
            resolved = resolve_image(image, rules)
            if resolved.image != image:
                container["image"] = resolved.image
                changed = True
                logger.info(f"Image opgelost: {image} -> {resolved.image}")
            if resolved.secret and resolved.secret not in secrets:
                secrets.append(resolved.secret)

    if secrets and _ensure_pull_secrets(pod_spec, secrets):
        changed = True
    return changed


def _resolve_documents(documents: list[Any], rules: Sequence[RegistryRule]) -> bool:
    """Draai de pas over elk document uit een bestand. True als er iets is veranderd."""
    changed = False
    for document in documents:
        if isinstance(document, dict) and _apply_rules(document, rules):
            changed = True
    return changed


def apply_rules_to_directory(target_path: str, rules: Sequence[RegistryRule]) -> None:
    """Draai de pas over elk gewoon YAML-manifest in een map.

    Versleutelde secrets en kustomize-bestanden blijven ongemoeid.
    """
    if not rules:
        return
    for file_path in glob.glob(os.path.join(target_path, "*.yaml")):
        filename = os.path.basename(file_path)
        if filename in _SKIP_NAMES or any(filename.endswith(suffix) for suffix in _SKIP_SUFFIXES):
            continue
        with open(file_path, encoding="utf-8") as handle:
            original = handle.read()
        documents = load_yaml_documents_from_string(original)
        if documents is None:
            logger.warning(f"Kon manifest niet lezen voor de registrypas: {filename}")
            continue
        if not _resolve_documents(documents, rules):
            continue
        # Eerst volledig in het geheugen serialiseren, dan pas het bestand aanraken: een
        # dumper die halverwege blaast mag geen afgekapt manifest achterlaten.
        rewritten = dump_yaml_documents_to_string(documents)
        with open(file_path, "w", encoding="utf-8") as handle:
            handle.write(rewritten)


def apply_rules_to_document(document: str, rules: Sequence[RegistryRule]) -> str:
    """Draai de pas over een YAML-tekst, voor een kale pod die los wordt toegepast.

    Een tekst met meer dan een document telt mee, en wat niet is opgelost komt letterlijk
    terug: er wordt alleen opnieuw geschreven als er ook echt iets is veranderd.
    """
    if not rules or not document.strip():
        return document
    documents = load_yaml_documents_from_string(document)
    if documents is None or not _resolve_documents(documents, rules):
        return document
    return dump_yaml_documents_to_string(documents)


def _ensure_pull_secrets(pod_spec: dict[str, Any], secrets: Sequence[str]) -> bool:
    """Voeg de pull-secrets toe die er nog niet staan, met behoud van wat er al stond.

    True als er iets is bijgekomen.
    """
    existing = pod_spec.get("imagePullSecrets") or []
    present = {entry.get("name") for entry in existing if isinstance(entry, dict)}
    added = False
    for name in secrets:
        if name not in present:
            existing.append({"name": name})
            present.add(name)
            added = True
    if added:
        pod_spec["imagePullSecrets"] = existing
    return added
