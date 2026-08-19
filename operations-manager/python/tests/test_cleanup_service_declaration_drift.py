"""Opruimen mag niet afgaan op de huidige component-bedrading van een deployment.

Op odcn-production overleefden `mpfoa_e01_test` en `mpfoa_e2w_test` hun project omdat
`deployment_uses_service` alleen kijkt naar de componenten waar de deployment op DAT
moment naar verwijst. De database was aangemaakt toen de bedrading nog anders was, dus
de delete meldde "skipped" als succes en liet de database plus de rollen staan.

Deze tests leggen beide kanten vast: de smalle vraag mag `False` blijven geven, maar de
brede vraag die het opruimen stelt moet `True` zijn.
"""

import pytest
from opi.handlers.project_file_handler import ProjectFileHandler

# Vereenvoudigde weergave van mpfoa-e01 vlak voor de verwijdering: de deployment verwijst
# alleen nog naar 'mgzpg' (die postgres niet noemt), terwijl de catalogus- en
# projectniveaus de service nog wel voeren.
PROJECT_MET_DRIFT = {
    "name": "mpfoa-e01",
    "schema-version": "2.2",
    "services": ["publish-on-web", "postgresql-database", "attachments"],
    "components": [
        {"name": "mgzpg", "services": ["attachments"]},
        {"name": "mgztxlog", "services": ["postgresql-database", "attachments"]},
        {"name": "mgzmgr", "services": ["publish-on-web", "postgresql-database", "attachments"]},
    ],
    "deployments": [
        {
            "name": "test",
            "cluster": "odcn-production",
            "components": [{"reference": "mgzpg"}],
        }
    ],
}

# mpfoa-e2w ging nog een stap verder: geen enkel catalogus-component noemde postgres nog,
# alleen het projectniveau.
PROJECT_MET_DRIFT_TOT_PROJECTNIVEAU = {
    "name": "mpfoa-e2w",
    "schema-version": "2.2",
    "services": ["publish-on-web", "persistent-storage", "postgresql-database", "attachments"],
    "components": [
        {"name": "mgzpg", "services": ["persistent-storage", "attachments"]},
        {"name": "mgzmgr", "services": ["publish-on-web", "attachments"]},
    ],
    "deployments": [
        {
            "name": "test",
            "cluster": "odcn-production",
            "components": [{"reference": "mgzpg"}, {"reference": "mgzmgr"}],
        }
    ],
}

PROJECT_ZONDER_POSTGRES = {
    "name": "alleen-web",
    "schema-version": "2.2",
    "services": ["publish-on-web"],
    "components": [{"name": "web", "services": ["publish-on-web"]}],
    "deployments": [
        {
            "name": "productie",
            "cluster": "odcn-production",
            "components": [{"reference": "web"}],
        }
    ],
}

PG_TYPES = ["postgresql-database", "namespace-postgresql-database"]


@pytest.fixture
def handler() -> ProjectFileHandler:
    return ProjectFileHandler()


@pytest.mark.parametrize("project", [PROJECT_MET_DRIFT, PROJECT_MET_DRIFT_TOT_PROJECTNIVEAU])
def test_smalle_check_mist_de_service_na_herbedrading(handler: ProjectFileHandler, project: dict) -> None:
    """Dit is het gedrag dat de databases liet staan; het is correct voor provisioning."""
    assert handler.deployment_uses_service(project, "test", PG_TYPES) is False


@pytest.mark.parametrize("project", [PROJECT_MET_DRIFT, PROJECT_MET_DRIFT_TOT_PROJECTNIVEAU])
def test_brede_check_ziet_de_service_wel(handler: ProjectFileHandler, project: dict) -> None:
    """Opruimen stelt de brede vraag en moet de service dus wel vinden."""
    assert handler.project_declares_service(project, PG_TYPES) is True


def test_brede_check_blijft_negatief_zonder_de_service(handler: ProjectFileHandler) -> None:
    """Een project dat de service nergens noemt mag niet ineens opgeruimd worden."""
    assert handler.project_declares_service(PROJECT_ZONDER_POSTGRES, PG_TYPES) is False


def test_brede_check_vindt_de_service_op_deploymentniveau(handler: ProjectFileHandler) -> None:
    """v1-bestanden houden de service alleen in het deployment-blok bij."""
    project = {
        "name": "oud",
        "deployments": [
            {
                "name": "productie",
                "cluster": "odcn-production",
                "services": [{"reference": "postgresql-database"}],
                "components": [{"reference": "web"}],
            }
        ],
        "components": [{"name": "web"}],
    }
    assert handler.project_declares_service(project, PG_TYPES) is True


def test_brede_check_vindt_de_service_in_een_ander_deployment(handler: ProjectFileHandler) -> None:
    """Ook als alleen een zusterdeployment de service voert telt hij mee.

    Bewust ruim: de vraag is of het project deze resource ooit kan hebben aangemaakt,
    en het verwijderen zelf is idempotent en werkt op een deployment-specifieke naam.
    """
    project = {
        "name": "gemengd",
        "services": [],
        "components": [
            {"name": "web", "services": ["publish-on-web"]},
            {"name": "api", "services": ["postgresql-database"]},
        ],
        "deployments": [
            {"name": "productie", "cluster": "odcn-production", "components": [{"reference": "api"}]},
            {"name": "preview", "cluster": "odcn-production", "components": [{"reference": "web"}]},
        ],
    }
    assert handler.deployment_uses_service(project, "preview", PG_TYPES) is False
    assert handler.project_declares_service(project, PG_TYPES) is True
