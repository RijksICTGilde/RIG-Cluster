"""De operations-namespace draagt het tenant-label niet.

`created-by: operations-manager` is de selector waarmee policies tenant-namespaces
herkennen. Staat het label op de operations-namespace, dan laat elke tenant-regel die
namespace ook toe, en dan is de versmalling van de ingress van OPI (issue #195) zonder
effect. Issue #83 vraagt dit label en wordt daarom afgewezen.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ruamel.yaml import YAML
from tests.netpol.policies import REPO_ROOT

if TYPE_CHECKING:
    from pathlib import Path

TENANT_LABEL = "created-by"
OVERLAYS = REPO_ROOT / "bootstrap" / "rig-system" / "kustomize" / "overlays"


def _namespace_manifests() -> list[tuple[Path, dict]]:
    return [
        (path, doc)
        for path in sorted(OVERLAYS.rglob("namespace*.yaml"))
        for doc in YAML().load_all(path.read_text())
        if doc and doc.get("kind") == "Namespace"
    ]


def test_er_zijn_namespace_manifests_gevonden():
    assert _namespace_manifests(), f"geen Namespace-manifest onder {OVERLAYS}"


def test_geen_operations_namespace_draagt_het_tenant_label():
    for path, doc in _namespace_manifests():
        labels = (doc.get("metadata") or {}).get("labels") or {}
        assert labels.get(TENANT_LABEL) != "operations-manager", (
            f"{path.relative_to(REPO_ROOT)} zet {TENANT_LABEL}: operations-manager op "
            f"{doc['metadata']['name']}. Daarmee laat elke tenant-regel de "
            "operations-namespace toe; selecteer die namespace op kubernetes.io/metadata.name."
        )
