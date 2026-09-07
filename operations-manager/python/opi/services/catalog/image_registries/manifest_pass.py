"""De manifestpas: los elke image in een podspec op en hang er het juiste secret aan.

Niet elke image loopt door de componentlus. Een sidecar staat als vaste waarde in zijn
sjabloon, en de backup-, restore-, db-console- en jobpods worden als kale ``Pod`` los
toegepast, buiten de manifestpijplijn om. Die plekken hebben dezelfde bewerking nodig, en
dat is precies waarom hier een WANDELING staat en geen tweede regelmechanisme: de regels
komen van dezelfde dienst en de omzetting is dezelfde ``resolve_image()``.

Dit verving ``RegistryRewriteExtension``. Die deed hetzelfde werk met een eigen tabel in
``extensions/odcn-registry-rewrite.yaml``, naast onze eigen ``imagePullSecretsMap`` en de
erfenis van de serviceaccount -- drie mechanismen die elkaar aanvulden, waarvan er twee
konden gaan afwijken. Nu is er één eigenaar van "welke registry, welk secret".

Idempotent: een image die de componentlus al heeft opgelost staat al op zijn bestemming,
en dan levert deze pas hetzelfde secret op en verandert er niets.
"""

from __future__ import annotations

import glob
import logging
import os
from typing import TYPE_CHECKING, Any

import yaml

from opi.services.catalog.image_registries.rules import resolve_image

if TYPE_CHECKING:
    from collections.abc import Sequence

    from opi.services.catalog.image_registries.rules import RegistryRule

logger = logging.getLogger(__name__)

#: Kinds die een podspec op ``spec.template.spec`` dragen.
_POD_TEMPLATE_KINDS = {"Deployment", "StatefulSet", "DaemonSet", "Job"}
#: CronJob draagt hem een niveau dieper.
_CRONJOB_KIND = "CronJob"

#: Bestanden die de wandeling overslaat: versleutelde secrets en de kustomize-plumbing.
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
    """Los elke container-image in dit manifest op en zorg dat zijn secret erbij staat.

    Muteert en retourneert hetzelfde manifest. Zonder podspec of zonder regels gebeurt er
    niets.
    """
    pod_spec = pod_spec_of(manifest)
    if pod_spec is None or not rules:
        return manifest

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
                logger.info(f"Image opgelost: {image} -> {resolved.image}")
            if resolved.secret and resolved.secret not in secrets:
                secrets.append(resolved.secret)

    if secrets:
        _ensure_pull_secrets(pod_spec, secrets)
    return manifest


def apply_rules_to_directory(target_path: str, rules: Sequence[RegistryRule]) -> None:
    """Draai de pas over elk gewoon YAML-manifest in een map.

    Slaat versleutelde secrets en kustomize-bestanden over: daar staat geen podspec in, en
    een .to-sops.yaml openen zou hem alleen maar kunnen beschadigen.
    """
    if not rules:
        return
    for file_path in glob.glob(os.path.join(target_path, "*.yaml")):
        filename = os.path.basename(file_path)
        if filename in _SKIP_NAMES or any(filename.endswith(suffix) for suffix in _SKIP_SUFFIXES):
            continue
        try:
            with open(file_path) as handle:
                manifest = yaml.safe_load(handle)
        except yaml.YAMLError:
            logger.warning(f"Kon manifest niet lezen voor de registrypas: {filename}")
            continue
        if not isinstance(manifest, dict):
            continue
        before = yaml.dump(manifest, default_flow_style=False, sort_keys=False)
        after = yaml.dump(apply_rules(manifest, rules), default_flow_style=False, sort_keys=False)
        if after != before:
            with open(file_path, "w") as handle:
                handle.write(after)


def apply_rules_to_document(document: str, rules: Sequence[RegistryRule]) -> str:
    """Draai de pas over één YAML-document als tekst, voor een kale pod die los wordt toegepast."""
    if not rules:
        return document
    text = document.strip()
    if not text:
        return document
    parsed = yaml.safe_load(text)
    if not isinstance(parsed, dict):
        return document
    return yaml.safe_dump(apply_rules(parsed, rules), default_flow_style=False, sort_keys=False)


def _ensure_pull_secrets(pod_spec: dict[str, Any], secrets: Sequence[str]) -> None:
    """Voeg de pull-secrets toe die er nog niet staan, met behoud van wat er al stond."""
    existing = pod_spec.get("imagePullSecrets") or []
    present = {entry.get("name") for entry in existing if isinstance(entry, dict)}
    for name in secrets:
        if name not in present:
            existing.append({"name": name})
            present.add(name)
    pod_spec["imagePullSecrets"] = existing
