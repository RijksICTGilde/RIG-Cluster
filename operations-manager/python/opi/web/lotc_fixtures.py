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

from datetime import UTC, datetime
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
        # Een dict, want zo levert de echte route hem aan: per deploymentnaam de
        # goedkeuringen die nog niet verleend zijn. Als lijst gaf dit een 500 op de
        # proefopstelling zodra het sjabloon .get(deployment.name) deed - de voorbeelddata
        # moet de VORM van de echte gegevens hebben, anders bewijst de proefopstelling
        # niets over de echte pagina.
        "approval_notices": {},
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
            # Dezelfde vorm als de echte route levert (opi/web/lotc_switch.py). Een
            # proefopstelling die een ANDERE vorm voedt dan de applicatie zou het
            # sjabloon laten werken op iets dat in productie nooit voorkomt.
            "health": [{"label": "Healthy", "count": len(projects)}],
        }

    if slug in ("projects", "users"):
        return {"projects": projects}

    if slug == "services":
        # Filteren op "kies ik dit zelf of is het er altijd" - dat is de vraag waarmee
        # iemand deze pagina opent. De binding (per component, per deployment) staat als
        # chip op de kaart; dat is verdieping, geen keuze vooraf.
        alle = services_overview(projects)
        return {
            "projects": projects,
            "services": alle,
            "service_filters": [
                ("", "Alle", len(alle)),
                ("user", "Zelf te kiezen", sum(1 for s in alle if not s["kind_label"])),
                ("system", "Altijd aan", sum(1 for s in alle if s["kind_label"])),
            ],
        }

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
            "flow_description": "Vul de gegevens in. Je kunt tussentijds terug zonder iets kwijt te raken.",
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

    if slug == "wizard-start":
        # De startpagina toont alleen uitleg en een knop; die heeft geen gegevens nodig.
        return {}

    if slug == "wizard-page":
        # De ECHTE wizard, met de echte stappen en de echte velden van de eerste stap.
        # Niet nagebouwd: dan zou de proefopstelling een vorm tonen die in de applicatie
        # niet voorkomt, en juist deze pagina is er om de formulierlaag in samenhang te
        # kunnen bekijken.
        from starlette.requests import Request

        from opi.forms.visualizers.flows import get_flow
        from opi.forms.wizard.resolver import get_section_metadata, resolve_active_sections
        from opi.forms.wizard.state import WizardState
        from opi.web.navigation_lotc import to_nldd_icon
        from opi.web.router_wizard import _render_step_html

        flow = get_flow("create-project")
        sections = resolve_active_sections(flow, {})
        state = WizardState(
            flow_id="create-project",
            current_step=sections[0].section_id,
            active_sections=[section.section_id for section in sections],
        )
        # Een kaal verzoek: _render_step_html leest er alleen de weergavekeuze uit, en
        # zonder querystring of koekje is dat de nieuwe vormgeving - precies wat deze
        # pagina toont. Het csrf-token is hier leeg omdat de proefopstelling niets
        # verstuurt; in de applicatie zet de CSRF-middleware het.
        request = Request(
            {"type": "http", "method": "GET", "path": "/lotc/bg/wizard-page", "headers": [], "query_string": b""}
        )
        request.state.csrf_token = ""
        return {
            "flow_title": flow.title,
            "flow_id": "create-project",
            "project_name": None,
            "steps": state.get_steps(get_section_metadata(sections)),
            "section": sections[0],
            "step_html": _render_step_html(request, sections[0], yaml_data={}),
            "preset_html": "",
            "errors": {},
            "global_errors": [],
            "show_review": flow.show_review,
            "all_steps_completed": False,
            "nldd_icon": to_nldd_icon,
        }

    if slug == "admin-users":
        # De platformgebruikers zijn de mensen die in de voorbeeldprojecten staan; zo komt
        # ook deze lijst uit dezelfde bron als de rest en niet uit een tweede verzinsel.
        leden = {
            member["email"]: project["display_name"]
            for project in projects
            for member in project["users"]
            if member.get("email")
        }
        return {
            "users": [
                {
                    "id": str(nummer),
                    "email": email,
                    "full_name": email.split("@")[0].replace(".", " ").title(),
                    "created_at": datetime(2026, 8, 1, tzinfo=UTC),
                }
                for nummer, email in enumerate(sorted(leden), start=1)
            ],
            "csrf_token": "",
            "success_message": "",
        }

    if slug == "admin-user-form":
        # De velden komen uit dezelfde editables als de echte route, gerenderd door
        # dezelfde adapter. Een eigen setje velden zou een tweede werkelijkheid zijn.
        from opi.web.router_user_admin import _render_form_html

        return {
            "page_heading": "Gebruiker bewerken",
            "form_action": "/admin/users/1/edit",
            "form_html": _render_form_html(
                data={"email": "voorbeeldbeheerder@rijksoverheid.nl", "full_name": "Voorbeeldbeheerder"},
                edit_mode=True,
                lotc=True,
            ),
            "csrf_token": "",
        }

    if slug == "admin-usage":
        # Voorbeeldcijfers, en zichtbaar rond. De proefopstelling heeft geen Grafana, dus
        # de melding daarover hoort er ook te staan - dat is dezelfde stand die een echte
        # instantie zonder billing-datasource doorgeeft.
        maanden = [
            {"month": nummer, "name": name, "year": 2026, "gib": gib, "cost": round(gib * 27.0, 2)}
            for nummer, (name, gib) in enumerate([("January", 12.5), ("February", 13.0), ("March", 14.25)], start=1)
        ]
        return {
            "year": 2026,
            "month_data": maanden,
            "total_gib": round(sum(maand["gib"] for maand in maanden), 2),
            "total_cost": round(sum(maand["cost"] for maand in maanden), 2),
            "selected_namespace": "all",
            "namespace_options": [{"value": "all", "label": "Alle namespaces"}]
            + [{"value": f"rig-{project['name']}", "label": f"rig-{project['name']}"} for project in projects],
            "price_per_gib": 27.0,
            "has_billing_datasource": False,
        }

    if slug == "admin-approvals":
        # Een aanvraag per stand, zodat de drie labels op het scherm naast elkaar staan.
        return {
            "projects_data": [
                {
                    "project_name": projects[0]["name"],
                    "approval_items": [
                        {
                            "type": "subdomain",
                            "domain": "rijksapps.nl",
                            "name": "voorbeeld",
                            "current_status": "requested",
                            "history": [{"date": datetime(2026, 8, 1, tzinfo=UTC), "by": "voorbeeldgebruiker"}],
                        },
                        {
                            "type": "domain",
                            "domain": "voorbeeld.nl",
                            "name": "voorbeeld.nl",
                            "current_status": "approved",
                            "history": [{"date": datetime(2026, 7, 14, tzinfo=UTC), "by": "voorbeeldbeheerder"}],
                        },
                    ],
                }
            ],
            "success_message": "",
        }

    if slug in ("project-details", "project-tabs", "project-context"):
        context = build_details_context(available_projects()[-1]) or {}
        # De tabbladen. Zes, en dat is de bovengrens van deze vorm: daarna wordt de balk
        # te vol. Repositories staat er bewust NIET bij - die stond op de oude pagina
        # omdat hij in het projectbestand staat, niet omdat iemand hem nodig heeft.
        # Dezelfde tabs als de bestaande projectpagina (Project, Deployments, Taken),
        # met Metrics erbij. Niet zelf bedacht: die verdeling scheidt "wat is dit
        # project", "waar draait het" en "wat is er aan de hand", en die scheiding werkt.
        context["tabs"] = {
            "project": {"label": "Project"},
            "deployments": {"label": "Deployments"},
            "metrics": {"label": "Metrics"},
            "taken": {"label": "Taken"},
        }
        # De proefopstelling heeft geen cluster, dus geen metingen. Dat is dezelfde stand
        # die een echte instantie doorgeeft als Prometheus niet bereikbaar is.
        context["usage"] = None
        context["usage_error"] = "Prometheus is niet verbonden in de proefopstelling."
        return context

    return {}


# Hoe een dienst gebonden is, in gewone taal. De registry noemt dit "binding" en dat zegt
# een gebruiker niets; dit zegt wat het voor hem betekent.
BINDING_LABELS = {
    "component": "per component",
    "deployment": "per deployment",
    "project": "per project",
}


def services_overview(projects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Alle zichtbare diensten uit de ECHTE registry, met wie ze afneemt.

    Bewust de registry en geen eigen lijst: naam, omschrijving, icoon, kleur, binding en
    hulptekst staan daar al, en een tweede lijst ernaast gaat vroeg of laat afwijken van
    wat de applicatie werkelijk aanbiedt.
    """
    from opi.services.services import ServiceAdapter
    from opi.web.navigation_lotc import to_nldd_icon

    used_by: dict[str, list[str]] = {}
    for project in projects:
        for service in project["services"]:
            used_by.setdefault(service, []).append(project["display_name"])

    overview: list[dict[str, Any]] = []
    for service_type in ServiceAdapter.get_all_services():
        definition = ServiceAdapter.SERVICE_DEFINITIONS[service_type]
        if getattr(definition, "hidden", False):
            continue

        binding = getattr(definition.binding, "value", str(definition.binding))
        is_platform = definition.kind.value == "system"

        chips = [BINDING_LABELS.get(binding, binding)]
        if definition.variables:
            chips.append(f"{len(definition.variables)} variabelen")
        if definition.requires:
            chips.append(f"vereist {len(definition.requires)}")

        overview.append(
            {
                "name": service_type.value,
                "label": definition.name,
                "summary": definition.description,
                # Door dezelfde vertaaltabel als de navigatie: onze iconen dragen
                # Nederlandse ROOS-namen en NLDD kent alleen zijn eigen woordenschat.
                # Zonder deze stap blijft het icoon leeg, en dat gebeurt STIL.
                "icon": to_nldd_icon(definition.icon),
                "color": definition.color,
                "chips": chips,
                # Alleen een label waar het iets zegt. Een dienst die je zelf kiest heeft
                # geen label nodig; dat is de normale situatie.
                "kind_label": "altijd aan" if is_platform else "",
                "kind_type": "info",
                "help": definition.description,
                "used_by": used_by.get(service_type.value, []),
            }
        )
    return overview
