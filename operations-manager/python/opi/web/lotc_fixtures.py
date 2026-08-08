"""Voorbeeldprojecten om de schermen te kunnen bekijken zonder te deployen.

De rijkste pagina's van de applicatie tonen een project, en zonder project tonen ze
niets. Daardoor was juist het scherm waar de meeste componenten samenkomen het enige dat
niet te beoordelen viel. Deze module vult dat gat.

Twee dingen die het eerlijk houden:

1. **Het loopt door de echte code.** De YAML-bestanden in ``lotc_fixtures/`` gaan door
   dezelfde helpers als een echt projectbestand (``ServiceAdapter``, de
   bijlagen-extractie). Wat de pagina toont is dus wat de applicatie zou tonen, en niet
   een met de hand nagebouwde context die er toevallig op lijkt.

2. **Het is zichtbaar verzonnen.** Namen als "Voorbeeldproject" en waarden als
   "VOORBEELDWAARDE-geen-echt-geheim" zijn er zodat een screenshot van deze pagina nooit
   voor een echt project kan worden aangezien. Er staan geen versleutelde waarden in: de
   pagina toont de ontsleutelde vorm, dus voor de weergave maakt het niets uit, en zo
   heeft de proefopstelling geen sleutels nodig.

Wat hier NIET vandaan komt, komt van de infrastructuur: metrics, ArgoCD-status, backups.
Die staan op "niet beschikbaar", en dat is geen verzinsel maar een stand die de
templates zelf kennen en tonen.
"""

from pathlib import Path
from typing import Any

import yaml

from opi.handlers.project_file_handler import extract_attachment_catalog, extract_attachment_usage
from opi.services.services import ServiceAdapter

FIXTURES_DIR = Path(__file__).parent / "lotc_fixtures"


def available_projects() -> list[str]:
    """De namen van de voorbeeldprojecten, uit de bestandsnamen."""
    return sorted(path.stem for path in FIXTURES_DIR.glob("*.yaml"))


def load_project_data(name: str) -> dict[str, Any] | None:
    """Lees een voorbeeldproject; None als het niet bestaat.

    De naam komt uit de URL, dus hij wordt tegen de bestandslijst gehouden in plaats van
    aan een pad geplakt. Anders zou ``../`` elk YAML-bestand op de schijf kunnen openen.
    """
    if name not in available_projects():
        return None
    data: dict[str, Any] = yaml.safe_load((FIXTURES_DIR / f"{name}.yaml").read_text())
    return data


def build_project_details(project_data: dict[str, Any]) -> dict[str, Any]:
    """Bouw de ``project``-context zoals de echte detailpagina hem opbouwt.

    Bewust dezelfde vorm en dezelfde helpers als in ``opi/web/router.py``: wijkt die af,
    dan toont de proefopstelling iets anders dan de applicatie, en juist dat verschil zou
    hier onopgemerkt blijven.
    """
    service_names = ServiceAdapter.extract_service_names_from_project_services(project_data.get("services", []))
    services_with_info = [
        {"enum": service_enum, "value": service_name}
        for service_name in service_names
        if (service_enum := ServiceAdapter.get_service_by_value(service_name)) is not None
    ]

    attachment_usage = extract_attachment_usage(project_data)
    project_name = project_data.get("name", "voorbeeld")

    return {
        "name": project_name,
        "display_name": project_data.get("display-name", project_name),
        "description": project_data.get("description", "Geen beschrijving beschikbaar"),
        "users": project_data.get("users", []),
        "user_role": "admin",
        "services": services_with_info,
        "clusters": project_data.get("clusters", []),
        "components": project_data.get("components", []),
        "deployments": project_data.get("deployments", []),
        "repositories": project_data.get("repositories", []),
        "config": project_data.get("config", {}),
        "helm_charts": project_data.get("helm-charts", []),
        "helmfile": project_data.get("helmfile", []),
        "attachments": [
            {
                "id": entry["id"],
                "filename": entry.get("filename", entry["id"]),
                "used_by": attachment_usage.get(entry["id"], []),
            }
            for entry in extract_attachment_catalog(project_data).values()
        ],
    }


def build_details_context(name: str) -> dict[str, Any] | None:
    """De volledige templatecontext voor de detailpagina van een voorbeeldproject.

    De sleutels die van draaiende infrastructuur afhangen staan op hun "niets te melden"-
    stand. Dat is precies wat een echte instantie ook doorgeeft als Prometheus of ArgoCD
    niet bereikbaar is, dus de pagina komt niet in een toestand die anders nooit voorkomt.
    """
    project_data = load_project_data(name)
    if project_data is None:
        return None

    from opi.forms.visualizers.flows import SERVICE_CONFIG_MODAL_FLOWS

    return {
        "title": f"Project Details - {project_data.get('display-name', name)}",
        "project": build_project_details(project_data),
        "user": {"email": "beheerder@voorbeeld.nl", "name": "Voorbeeldbeheerder"},
        "user_role": "admin",
        "ServiceAdapter": ServiceAdapter,
        "service_binding_label": {},
        "service_config_hint": {},
        "prometheus_available": False,
        "argocd_available": False,
        "approval_notices": [],
        "backups_available": False,
        "current_cluster": "odcn-production",
        "cluster_base_domains": {},
        "csrf_token": "voorbeeld-token",
        "pending_rollout": {"count": 0, "since": None, "task_types": []},
        "service_config_sections": SERVICE_CONFIG_MODAL_FLOWS,
        "deployment_service_actions": {},
        "deployment_state_facts": {},
        "deployment_service_sections": {},
        "service_detail_sections": [],
    }


# Activiteit voor het dashboard. Verzonnen, maar consistent met de voorbeeldprojecten:
# een tweede werkelijkheid naast de fixtures zou het beeld juist onbetrouwbaar maken.
ACTIVITY = [
    {
        "icon": "plus",
        "actor": "Voorbeeldbeheerder",
        "action": "project aangemaakt",
        "resource": "voorbeeld-klein",
        "at": "vandaag 09:12",
    },
    {
        "icon": "arrow-up-arrow-down",
        "actor": "Voorbeeldontwikkelaar",
        "action": "deployment uitgerold",
        "resource": "voorbeeld-volledig / productie",
        "at": "gisteren 16:40",
    },
    {
        "icon": "lock-closed",
        "actor": "Voorbeeldbeheerder",
        "action": "sleutel vernieuwd",
        "resource": "voorbeeld-volledig / api-key",
        "at": "gisteren 11:05",
    },
]


def all_projects() -> list[dict[str, Any]]:
    """Elk voorbeeldproject in de vorm die de overzichtspagina's gebruiken."""
    projects: list[dict[str, Any]] = []
    for name in available_projects():
        data = load_project_data(name)
        if data is None:
            continue
        details = build_project_details(data)
        projects.append(
            {
                "name": details["name"],
                "display_name": details["display_name"],
                "description": details["description"],
                "clusters": details["clusters"],
                "users": details["users"],
                "components": details["components"],
                "deployments": details["deployments"],
                "deployment_count": len(details["deployments"]),
                "services": [service["value"] for service in details["services"]],
            }
        )
    return projects


def page_data(slug: str) -> dict[str, Any]:
    """De gegevens die een herontworpen pagina nodig heeft.

    Een eenvoudige tabel en geen slim mechanisme: elke pagina heeft andere gegevens
    nodig, en dat expliciet opschrijven leest prettiger dan een laag die het probeert te
    raden. Een onbekende naam levert een lege context, en dan valt op de pagina zelf te
    zien wat er ontbreekt.
    """
    projects = all_projects()

    if slug == "dashboard":
        return {
            "tiles": [
                {
                    "icon": "rectangle-stack",
                    "value": str(len(projects)),
                    "label": "Projecten",
                    "sub": "voorbeelddata",
                    "href": "/lotc/bg/projects",
                },
                {
                    "icon": "arrow-up-arrow-down",
                    "value": str(sum(project["deployment_count"] for project in projects)),
                    "label": "Deployments",
                    "sub": "over alle clusters",
                    "href": None,
                },
                {
                    "icon": "person-2",
                    "value": str(sum(len(project["users"]) for project in projects)),
                    "label": "Gebruikers",
                    "sub": "1 beheerder",
                    "href": "/lotc/bg/users",
                },
                {
                    "icon": "cylinder-split",
                    "value": str(len({service for project in projects for service in project["services"]})),
                    "label": "Diensten",
                    "sub": "in gebruik",
                    "href": "/lotc/bg/services",
                },
            ],
            "projects": projects,
            "activity": ACTIVITY,
        }

    if slug in ("projects", "services", "users"):
        return {"projects": projects}

    if slug == "wizard":
        # De velden komen uit dezelfde voorbeeldreeks als /lotc/formulier en worden door
        # dezelfde adapter gerenderd. Zo toont deze pagina de ECHTE formulierlaag; een
        # eigen setje velden zou een tweede werkelijkheid zijn die stil kan gaan afwijken.
        from opi.forms.widgets.lotc import LOTCWidgetAdapter
        from opi.web.lotc_form_preview import EXAMPLE_FIELDS

        adapter = LOTCWidgetAdapter()
        rendered = [adapter.render_field(field) for field in EXAMPLE_FIELDS]
        return {
            "flow_title": "Nieuw project",
            "flow_description": "Vul de gegevens in. U kunt tussentijds terug zonder iets kwijt te raken.",
            "current_step": 2,
            "steps": [
                {"label": "Project", "status": "complete"},
                {"label": "Diensten", "status": "current"},
                {"label": "Componenten", "status": None},
                {"label": "Controleren", "status": None},
            ],
            "field_groups": [
                {"legend": "Projectgegevens", "fields": rendered[:4]},
                {"legend": "Instellingen", "fields": rendered[4:]},
            ],
        }

    # De twee navigatieprototypes tonen hetzelfde project als de detailpagina: het gaat
    # om de navigatievorm, en dan moet de inhoud eronder juist gelijk zijn.
    if slug in ("project-details", "project-tabs", "project-context"):
        context = build_details_context(available_projects()[-1])
        return context or {}

    return {}
