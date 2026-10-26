"""Welke kinds een deployments-repo rendert, en onder welke kubectl-typen ze te vragen zijn.

Dit is de maatstaf voor "is deze namespace leeg": niet elk type dat het cluster serveert,
maar de kinds die wij er zelf in zetten. In een Capsule-tenant mag de serviceaccount zo'n
90 van de ~322 typen niet lijsten, en een inventaris die daarop onvolledig heet keurt een
verwijdering af die wel afkwam (features/futures/verwijderpaden-in-kaart.md, geval 4).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

_DOCUMENT_SEPARATOR = re.compile(r"(?m)^---")

#: ``apiVersion:`` or ``kind:`` at the start of a line, so at the top of a document and
#: never the nested ``kind:`` of an ownerReference or a roleRef.
_TOP_LEVEL_FIELD = re.compile(r"(?m)^(apiVersion|kind):[ \t]*['\"]?([^'\"\s#]*)")


def rendered_kinds(
    deployments_repo: Path, cluster: str, skipped_projects: frozenset[str] = frozenset()
) -> tuple[set[str], list[str]]:
    """Every kind the deployments repo renders for this cluster, as ``Kind.group`` (core: ``Kind``).

    Read from the top-level ``apiVersion:`` and ``kind:`` lines rather than parsed: the
    helmfile projects carry Helm chart templates, which are not YAML until ArgoCD renders
    them. An ``apiVersion`` that is a template gives ``Kind.*``, every group serving that
    kind. A ``kind`` that is a template cannot be read at all; those files come back as
    the second value, to be reported rather than silently left out.
    """
    kinds = set()
    unreadable = []
    for path in sorted((deployments_repo / cluster).rglob("*.yaml")):
        parts = path.relative_to(deployments_repo).parts
        if any(part.startswith(".") for part in parts) or parts[1] in skipped_projects:
            continue
        for document in _DOCUMENT_SEPARATOR.split(path.read_text(errors="replace")):
            fields = dict(_TOP_LEVEL_FIELD.findall(document))
            kind = fields.get("kind", "")
            if not kind:
                continue
            if kind.startswith("ENC["):
                # A SOPS file with its kind encrypted. OPI encrypts only what it writes as
                # .to-sops.yaml, and those are Secrets: on odcn-production, 9 October 2026,
                # all 707 carried a 6-byte kind and a 2-byte apiVersion.
                kinds.add("Secret")
            elif not kind.isalnum():
                unreadable.append(path.relative_to(deployments_repo).as_posix())
            elif "{{" in fields.get("apiVersion", ""):
                kinds.add(f"{kind}.*")
            else:
                group = fields.get("apiVersion", "").rpartition("/")[0]
                kinds.add(f"{kind}.{group}" if group else kind)
    return kinds, unreadable


def rendered_types(kinds: set[str], served: dict[str, str]) -> list[str]:
    """The kubectl type names of the rendered kinds this cluster serves.

    A rendered kind the cluster does not serve cannot have been created there.
    """
    types = set()
    for kind in kinds:
        if kind.endswith(".*"):
            name = kind[:-2]
            types |= {type_name for key, type_name in served.items() if key.partition(".")[0] == name}
        elif kind in served:
            types.add(served[kind])
    return sorted(types)
