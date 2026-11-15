"""The manifest pass: resolve every image in a pod spec and attach the right secret.

A walk and not a second mechanism: sidecars, backup, restore, db-console and job pods do not
go through the component loop, but use the same rules and the same ``resolve_image()`` here.
Idempotent, because an already resolved image is at its destination.

Reading and writing go through ``opi/utils/yaml_util.py``, the only writer: it keeps comments
and multi-line strings (a literal block) intact. A manifest where nothing resolved is left
untouched.
"""

from __future__ import annotations

import glob
import logging
import os
from typing import TYPE_CHECKING, Any

from opi.services.catalog.image_registries.rules import resolve_image
from opi.utils.sops import SOPS_SUFFIX, TO_SOPS_SUFFIX
from opi.utils.yaml_util import dump_yaml_documents_to_string, load_yaml_documents_from_string

if TYPE_CHECKING:
    from collections.abc import Sequence

    from opi.services.catalog.image_registries.rules import RegistryRule

logger = logging.getLogger(__name__)

#: Kinds that carry a pod spec at ``spec.template.spec``.
_POD_TEMPLATE_KINDS = {"Deployment", "StatefulSet", "DaemonSet", "Job"}
_CRONJOB_KIND = "CronJob"

_SKIP_SUFFIXES = (SOPS_SUFFIX, TO_SOPS_SUFFIX)
_SKIP_NAMES = ("kustomization.yaml", "decrypt-sops.yaml")


def pod_spec_of(manifest: dict[str, Any]) -> dict[str, Any] | None:
    """The pod spec in this manifest, or None when it carries none."""
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
    """Resolve every container image in this manifest and add its secret.

    Mutates and returns the same manifest.
    """
    _apply_rules(manifest, rules)
    return manifest


def _apply_rules(manifest: dict[str, Any], rules: Sequence[RegistryRule]) -> bool:
    """The pass itself. True when anything in this manifest changed."""
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
    """Run the pass over every document in a file. True when anything changed."""
    changed = False
    for document in documents:
        if isinstance(document, dict) and _apply_rules(document, rules):
            changed = True
    return changed


def apply_rules_to_directory(target_path: str, rules: Sequence[RegistryRule]) -> None:
    """Run the pass over every plain YAML manifest in a directory.

    Encrypted secrets and kustomize files are left alone.
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
        # Serialise fully in memory first and only then touch the file: a dumper that blows
        # up halfway must not leave a truncated manifest behind.
        rewritten = dump_yaml_documents_to_string(documents)
        with open(file_path, "w", encoding="utf-8") as handle:
            handle.write(rewritten)


def apply_rules_to_document(document: str, rules: Sequence[RegistryRule]) -> str:
    """Run the pass over a YAML text, for a bare pod applied on its own.

    A text with more than one document counts too.
    """
    if not rules or not document.strip():
        return document
    documents = load_yaml_documents_from_string(document)
    if documents is None or not _resolve_documents(documents, rules):
        return document
    return dump_yaml_documents_to_string(documents)


def _ensure_pull_secrets(pod_spec: dict[str, Any], secrets: Sequence[str]) -> bool:
    """Add the pull secrets that are not there yet, keeping what was already present.

    True when anything was added.
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
