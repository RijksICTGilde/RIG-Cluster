"""De velden die een nieuwe deployment nodig heeft maar geen formulier uitvraagt.

Cluster en repository werden gekopieerd van een bestaande deployment. Dat werkt vanaf de
tweede, en elk project werd geboren met een eerste, dus het gat viel niet op. Een project
dat via de API is aangemaakt heeft er geen: de deployment die je daarna toevoegt kreeg
dan geen van beide, en dat kwam pas veel later boven als "Repository configuration not
found: None" bij het verwerken.
"""

from __future__ import annotations

from typing import Any

from opi.forms.wizard.save import _system_fields_for_new_deployment

PROJECT = "cli-test-abc"


def _project(**extra: Any) -> dict[str, Any]:
    """Een project zoals de API het aanmaakt: een repository, een cluster, geen deployment."""
    return {
        "name": PROJECT,
        "clusters": ["local"],
        "repositories": [{"name": "main-repo", "url": "https://forgejo.example.test/rig/apps.git"}],
        **extra,
    }


def test_de_eerste_deployment_erft_van_het_project() -> None:
    data = _project(deployments=[{"name": "productie"}])

    _system_fields_for_new_deployment(data, PROJECT)

    nieuw = data["deployments"][-1]
    assert nieuw["repository"] == "main-repo"
    assert nieuw["cluster"] == "local"
    assert nieuw["namespace"] == PROJECT


def test_een_volgende_deployment_erft_van_zijn_buur() -> None:
    """Het bestaande gedrag: een zusterdeployment gaat voor de projectdeclaratie."""
    data = _project(
        deployments=[
            {"name": "test", "cluster": "ander-cluster", "repository": "andere-repo"},
            {"name": "productie"},
        ]
    )

    _system_fields_for_new_deployment(data, PROJECT)

    nieuw = data["deployments"][-1]
    assert nieuw["repository"] == "andere-repo"
    assert nieuw["cluster"] == "ander-cluster"


def test_wat_al_ingevuld_is_blijft_staan() -> None:
    data = _project(deployments=[{"name": "productie", "cluster": "eigen", "repository": "eigen-repo"}])

    _system_fields_for_new_deployment(data, PROJECT)

    nieuw = data["deployments"][-1]
    assert nieuw["repository"] == "eigen-repo"
    assert nieuw["cluster"] == "eigen"


def test_bij_meer_dan_een_keuze_wordt_er_niet_geraden() -> None:
    """Kiezen zou een gok zijn; de validatie weigert het bestand en zegt waarom."""
    data = {
        "name": PROJECT,
        "clusters": ["local", "odcn-production"],
        "repositories": [{"name": "main-repo"}, {"name": "tweede-repo"}],
        "deployments": [{"name": "productie"}],
    }

    _system_fields_for_new_deployment(data, PROJECT)

    nieuw = data["deployments"][-1]
    assert "repository" not in nieuw
    assert "cluster" not in nieuw
