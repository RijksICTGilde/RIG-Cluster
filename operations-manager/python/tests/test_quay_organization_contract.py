"""De Organization-CR wordt getoetst aan het schema van het cluster, niet aan onszelf.

Dit bestand bestaat omdat de eerste versie van het sjabloon door elke eigen test kwam en
door de API-server werd geweigerd: `friendlyName` stond op `spec` in plaats van onder
`proxyCache`, `credentialsSecretRef` heette `credentialsSecret`, `rotation` hing onder
`robot` in plaats van onder `robot.imagePullSecret`, en de twee verplichte velden
`customerName` en `tenants` ontbraken. Een test die vastlegt wat wij produceren bevestigt
onze eigen vergissing; deze legt ernaast wat het cluster accepteert.

Het schema ververs je met:
    kubectl get --raw /openapi/v3/apis/quay.k8s.rijksapps.nl/v1alpha1
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from opi.generation.manifests import render_template
from opi.services.catalog.base import ProjectManifestContext
from opi.services.registry import get_service
from opi.services.services_enums import ServiceType

_SCHEMA = json.loads(
    (Path(__file__).parent.parent / "opi/schemas/external/quay-organization.v1alpha1.json").read_text()
)["schema"]


def _afwijkingen(waarde: Any, schema: dict[str, Any], pad: str = "spec") -> list[str]:
    """Onbekende en ontbrekende velden, vergeleken met het schema van het cluster."""
    eigenschappen = schema.get("properties", {})
    fouten = [f"{pad}.{v} ontbreekt en is verplicht" for v in schema.get("required", []) if v not in waarde]
    for sleutel, sub in waarde.items():
        if sleutel not in eigenschappen:
            fouten.append(f"{pad}.{sleutel} bestaat niet in het schema")
            continue
        if isinstance(sub, dict):
            fouten.extend(_afwijkingen(sub, eigenschappen[sleutel], f"{pad}.{sleutel}"))
    return fouten


@pytest.fixture
def organization_manifest() -> dict[str, Any]:
    project = {
        "name": "zpa-cj8",
        "services": [
            {
                "name": "image-registries",
                "config": {
                    "registries": [
                        {
                            "name": "cod",
                            "upstream": "code.overheid.nl/robbert.uittenbroek",
                            "username": "robbert.uittenbroek",
                            "password": "plain:token",
                        }
                    ]
                },
            }
        ],
    }
    ctx = ProjectManifestContext(
        project_name="zpa-cj8",
        project_data=project,
        cluster="odcn-production",
        namespace="rig-prd-zpa-cj8",
    )
    specs = get_service(ServiceType.IMAGE_REGISTRIES).contribute_project_manifests(ctx)
    organization = next(s for s in specs if s.template_path == "quay-proxy-organization.yaml.jinja")
    return yaml.safe_load(render_template(organization.template_path, organization.values))


def test_het_gerenderde_manifest_past_op_het_schema_van_het_cluster(organization_manifest) -> None:
    fouten = _afwijkingen(organization_manifest["spec"], _SCHEMA["properties"]["spec"])
    assert fouten == [], "\n".join(fouten)


def test_de_apiversion_is_die_van_het_cluster(organization_manifest) -> None:
    assert organization_manifest["apiVersion"] == "quay.k8s.rijksapps.nl/v1alpha1"


def test_de_toets_vangt_een_veld_op_de_verkeerde_plek(organization_manifest) -> None:
    """Tegenproef: de fout die de eerste versie maakte, moet rood worden."""
    kapot = dict(organization_manifest["spec"])
    kapot["friendlyName"] = "codeoverheid"
    del kapot["customerName"]
    fouten = _afwijkingen(kapot, _SCHEMA["properties"]["spec"])
    assert any("friendlyName bestaat niet" in f for f in fouten)
    assert any("customerName ontbreekt" in f for f in fouten)
