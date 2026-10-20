"""De vier ArgoCD-sjablonen coderen elke waarde als YAML-scalar.

Gedekt: ``argo-repository-https.yaml.jinja``, ``argo-repository.yaml.jinja``,
``argocd-application.yaml.jinja`` en ``argocd-appproject.yaml.jinja``. Dat zijn de drie
objecten die OPI per project in de argo-repo schrijft. Elke andere sjabloon in
``manifests/`` hoort bij ``tests/test_template_injection_sweep.py``.

De waarden komen uit het projectbestand (``repositories[].name``, ``.username``, ``.branch``,
het ontsleutelde wachtwoord) en belanden letterlijk in een Secret dat ArgoCD toepast.
"""

import copy
from typing import Any

import pytest
from opi.generation.manifests import render_template
from ruamel.yaml import YAML

#: Structuurtekens van YAML, een backslash, een dubbele quote en een astraal teken. Dat
#: laatste onderscheidt ``yaml_scalar`` van ``tojson``, dat er een surrogaatpaar van maakt
#: waar SOPS op afbreekt.
PAYLOAD = '" \n---\nkind: Secret\ningespoten: true\n# een \\ twee: drie\n\U0001f600'

HTTPS_REPOSITORY = {
    "name": "demo-main-repo",
    "namespace": "argocd",
    "type": "git",
    "repository_url": "https://forgejo.example/rig-admin/zad-deployments.git",
    "username": "rig-admin",
    "password": "een-token",
}

SSH_REPOSITORY = {
    "name": "demo-main-repo",
    "namespace": "argocd",
    "type": "git",
    "repository_url": "ssh://git@forgejo.example/rig-admin/zad-deployments.git",
    "ssh_private_key": "-----BEGIN OPENSSH PRIVATE KEY-----\nSLEUTEL==\n-----END OPENSSH PRIVATE KEY-----\n",
}

APPLICATION = {
    "name": "demo-deployment-1",
    "namespace": "argocd",
    "argo_project": "demo",
    "repoURL": "https://forgejo.example/rig-admin/zad-deployments.git",
    "targetRevision": "main",
    "repoPath": "odcn-production/demo/deployment-1",
    "labels": {"project": "demo"},
    "destination": {"namespace": "rig-prd-demo"},
}

APPPROJECT = {
    "name": "demo",
    "namespace": "argocd",
    "labels": {"project": "demo"},
    "destination": {"namespace": "rig-prd-demo"},
}

#: ``sjabloon -> (basisvariabelen, [(pad in de variabelen, paden in het document)])``
SJABLONEN: dict[str, tuple[dict[str, Any], list[tuple[tuple[str, ...], list[tuple[str | int, ...]]]]]] = {
    "argo-repository-https.yaml.jinja": (
        HTTPS_REPOSITORY,
        [
            (("name",), [("metadata", "name"), ("stringData", "name")]),
            (("namespace",), [("metadata", "namespace")]),
            (("type",), [("stringData", "type")]),
            (("repository_url",), [("stringData", "url")]),
            (("username",), [("stringData", "username")]),
            (("password",), [("stringData", "password")]),
        ],
    ),
    "argo-repository.yaml.jinja": (
        SSH_REPOSITORY,
        [
            (("name",), [("metadata", "name"), ("stringData", "name")]),
            (("namespace",), [("metadata", "namespace")]),
            (("type",), [("stringData", "type")]),
            (("repository_url",), [("stringData", "url")]),
            (("ssh_private_key",), [("stringData", "sshPrivateKey")]),
        ],
    ),
    "argocd-application.yaml.jinja": (
        APPLICATION,
        [
            (("name",), [("metadata", "name")]),
            (("namespace",), [("metadata", "namespace")]),
            (("labels", "project"), [("metadata", "labels", "project")]),
            (("argo_project",), [("spec", "project")]),
            (("repoURL",), [("spec", "source", "repoURL")]),
            (("targetRevision",), [("spec", "source", "targetRevision")]),
            (("repoPath",), [("spec", "source", "path")]),
            (("destination", "namespace"), [("spec", "destination", "namespace")]),
        ],
    ),
    "argocd-appproject.yaml.jinja": (
        APPPROJECT,
        [
            (("name",), [("metadata", "name")]),
            (("namespace",), [("metadata", "namespace")]),
            (("labels", "project"), [("metadata", "labels", "project")]),
            (("destination", "namespace"), [("spec", "destinations", 0, "namespace")]),
        ],
    ),
}

GEVALLEN = [
    pytest.param(
        sjabloon,
        basis,
        variabele,
        paden,
        id=f"{sjabloon.removesuffix('.yaml.jinja')}:{'.'.join(variabele)}",
    )
    for sjabloon, (basis, velden) in SJABLONEN.items()
    for variabele, paden in velden
]


def _documenten(rendered: str) -> list[Any]:
    return list(YAML(typ="safe").load_all(rendered))


def _een_document(rendered: str) -> Any:
    documenten = _documenten(rendered)
    assert len(documenten) == 1, f"de render leverde {len(documenten)} documenten op"
    return documenten[0]


def _met(variabelen: dict[str, Any], pad: tuple[str, ...], waarde: Any) -> dict[str, Any]:
    kopie = copy.deepcopy(variabelen)
    doel: Any = kopie
    for sleutel in pad[:-1]:
        doel = doel[sleutel]
    doel[pad[-1]] = waarde
    return kopie


def _op(document: Any, pad: tuple[str | int, ...]) -> Any:
    for sleutel in pad:
        document = document[sleutel]
    return document


def _alle_sleutels(document: Any) -> set[str]:
    """Elke mappingsleutel in het document, op elke diepte."""
    if isinstance(document, dict):
        return set(document) | {sleutel for waarde in document.values() for sleutel in _alle_sleutels(waarde)}
    if isinstance(document, list):
        return {sleutel for waarde in document for sleutel in _alle_sleutels(waarde)}
    return set()


@pytest.mark.parametrize(("sjabloon", "basis", "variabele", "paden"), GEVALLEN)
def test_een_waarde_met_structuurtekens_blijft_een_scalar(
    sjabloon: str, basis: dict[str, Any], variabele: tuple[str, ...], paden: list[tuple[str | int, ...]]
) -> None:
    gewoon = _een_document(render_template(sjabloon, basis))
    afwijkend = _een_document(render_template(sjabloon, _met(basis, variabele, PAYLOAD)))

    assert set(afwijkend) == set(gewoon)
    assert _alle_sleutels(afwijkend) == _alle_sleutels(gewoon)
    for pad in paden:
        assert _op(afwijkend, pad) == PAYLOAD


def test_de_sync_wave_blijft_een_string() -> None:
    """De annotatie is een string; een getal weigert de API-server."""
    for wave, verwacht in ((0, "0"), (1, "1")):
        document = _een_document(render_template("argocd-application.yaml.jinja", {**APPLICATION, "sync_wave": wave}))
        assert document["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"] == verwacht
