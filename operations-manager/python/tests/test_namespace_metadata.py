"""Tenant-namespace labels en annotaties komen per cluster uit cluster_config.

De egress-gateway-annotatie stond hardgecodeerd in de template en ging dus mee
naar elk cluster. Op ODCN bewaakt Kyverno die waarde en wordt de namespace
onbruikbaar bij een verkeerde, dus een overgeërfde annotatie is daar niet inert
maar fout. Deze tests borgen dat de keuze aan het cluster hangt.
"""

from __future__ import annotations

from unittest.mock import patch

from opi.core.cluster_config import get_namespace_metadata
from opi.generation.manifests import render_template
from ruamel.yaml import YAML

_EGRESS = "egress.projectcalico.org/egressGatewayPolicy"


def _render_for(cluster: str) -> dict:
    with patch("opi.generation.manifests.settings") as mock_settings:
        mock_settings.MANIFESTS_PATH = "manifests"
        mock_settings.CLUSTER_MANAGER = cluster
        result = render_template("namespace.yaml.jinja", {"namespace": "rig-myproject"})
    return YAML().load(result)


def test_odcn_keeps_its_egress_annotation() -> None:
    doc = _render_for("odcn-production")
    assert doc["metadata"]["annotations"][_EGRESS] == "internet"


def test_other_clusters_get_no_annotations_block() -> None:
    """Zonder annotaties hoort de sleutel helemaal weg te blijven, niet leeg te zijn."""
    doc = _render_for("sandboxed-local")
    assert "annotations" not in doc["metadata"]
    assert doc["metadata"]["labels"]["created-by"] == "operations-manager"


def test_unknown_cluster_inherits_nothing() -> None:
    """Een nieuw cluster erft geen platformafspraak van een ander cluster."""
    doc = _render_for("nog-niet-geconfigureerd")
    assert "annotations" not in doc["metadata"]


def test_configured_labels_land_next_to_the_base_label() -> None:
    """Labels uit de config komen erbij, ze vervangen created-by niet."""
    metadata = {"labels": {"fundament.io/project-id": "abc-123"}, "annotations": {}}
    with patch("opi.generation.manifests.get_namespace_metadata", return_value=metadata):
        doc = _render_for("odcn-production")
    assert doc["metadata"]["labels"]["created-by"] == "operations-manager"
    assert doc["metadata"]["labels"]["fundament.io/project-id"] == "abc-123"


def test_accessor_shape_is_stable() -> None:
    """Elke cluster levert beide sleutels, ook als ze leeg zijn."""
    for cluster in ("local", "sandboxed-local", "odcn-production", "bestaat-niet"):
        metadata = get_namespace_metadata(cluster)
        assert set(metadata) == {"labels", "annotations"}
