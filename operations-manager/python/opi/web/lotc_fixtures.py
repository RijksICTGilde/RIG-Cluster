"""Example projects, so the screens can be looked at without deploying.

The richest pages of the application show a project and show nothing without one, which is
what this module is for. Two rules keep it honest:

1. **It runs through the real code.** The YAML files in ``lotc_fixtures/`` go through the same
   helpers as a real project file, so the page shows what the application would show and not a
   hand-built context that happens to resemble it.

2. **It is visibly made up.** Names like "Voorbeeldproject" are there so a screenshot of this
   page can never be mistaken for a real project. No encrypted values: the page shows the
   decrypted form anyway, and this way the preview needs no keys.

What does NOT come from here comes from the infrastructure: metrics, ArgoCD status, backups.
Those stand at "not available", which is a state the templates know and show.
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from opi.core.dns_config import DEFAULT_ROUTER_HOSTNAME, router_addresses_for
from opi.http.render.lotc_switch import PROJECT_TABS
from opi.services.catalog.approval import SERVICE_USE_SUBJECT
from opi.services.project_file_handler import extract_attachment_catalog, extract_attachment_usage
from opi.services.registry import collect_detail_page_sections
from opi.services.services import ServiceAdapter
from opi.services.shared_services import Threshold, UnmeasuredService
from opi.services.user_otp_service import OtpStatus
from opi.utils.qr import qr_svg
from opi.utils.yaml_util import load_yaml_safe
from opi.web.router_approvals import APPROVAL_STATUSSEN, group_by_service
from opi.web.security_policy import security_policy_html

FIXTURES_DIR = Path(__file__).parent / "lotc_fixtures"


def available_projects() -> list[str]:
    """The names of the example projects, from the file names."""
    return sorted(path.stem for path in FIXTURES_DIR.glob("*.yaml"))


def load_project_data(name: str) -> dict[str, Any] | None:
    """Read an example project; None when it does not exist.

    The name comes from the URL, so it is held against the file listing instead of pasted onto
    a path: a ``../`` would otherwise open any YAML file on disk.
    """
    if name not in available_projects():
        return None
    data: dict[str, Any] = load_yaml_safe((FIXTURES_DIR / f"{name}.yaml").read_text(encoding="utf-8"))
    return data


def build_project_details(project_data: dict[str, Any]) -> dict[str, Any]:
    """Build the ``project`` context the way the real detail page builds it.

    Keep the same shape and the same helpers as ``opi/web/router.py``: a difference would make
    the preview show something other than the application.
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
        "source_code": project_data.get("source-code", []),
        "users": project_data.get("users", []),
        "user_roles": ["admin", "ops"],
        # The gallery has no project file and so no role gate to read; ``can()`` says yes here,
        # so the buttons are on the example page.
        "can": lambda *_a, **_k: True,
        # The example admin is a platform admin, so the Configuratie & Secrets block is in the
        # gallery as well.
        "is_platform_admin": True,
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
    """The full template context for the detail page of an example project.

    The keys that depend on running infrastructure stand at their "nothing to report" value,
    which is what a real instance passes when Prometheus or ArgoCD is unreachable.
    """
    project_data = load_project_data(name)
    if project_data is None:
        return None

    from opi.forms.visualizers.flows import SERVICE_CONFIG_MODAL_FLOWS

    return {
        "title": f"Project Details - {project_data.get('display-name', name)}",
        "project": build_project_details(project_data),
        "user": {"email": "beheerder@voorbeeld.nl", "name": "Voorbeeldbeheerder"},
        "user_roles": ["admin", "ops"],
        # The gallery has no project file and so no role gate to read; ``can()`` says yes here,
        # so the buttons are on the example page.
        "can": lambda *_a, **_k: True,
        "ServiceAdapter": ServiceAdapter,
        "service_selection_labels": {},
        "service_config_hint": {},
        "prometheus_available": False,
        "argocd_available": False,
        # A dict, because that is how the real route supplies it: per deployment name the
        # approvals not yet granted. The example data has to have the SHAPE of the real data,
        # or the preview proves nothing about the real page.
        "approval_notices": {},
        "address_approval_notices": {},
        "backups_available": False,
        "current_cluster": "odcn-production",
        "cluster_base_domains": {},
        "csrf_token": "voorbeeld-token",
        "pending_rollout": {"count": 0, "since": None, "task_types": [], "rollout_in_progress": False},
        "service_config_sections": SERVICE_CONFIG_MODAL_FLOWS,
        "deployment_service_actions": {},
        "deployment_service_sections": {},
        # Empty, as for a project that uses no service with anything to back up; the preview
        # has no cluster.
        "backups_sections": [],
        # The blocks the SERVICES themselves supply, from the real registry and not from a list
        # here, so a section that is missing shows up on the page.
        "service_detail_sections": collect_detail_page_sections(project_data, lambda _action: True),
    }


def all_projects() -> list[dict[str, Any]]:
    """Every example project in the shape the overview pages use."""
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


#: A visibly made-up seed for the registration page. Not a real secret: the page shows what
#: the generator makes of it, and this preview has no second factor.
VOORBEELD_BASE32_SEED = "VOORBEELDWAARDEGEENECHTGEHEIM234"
VOORBEELD_OTPAUTH_URI = f"otpauth://totp/ZAD:voorbeeld@rijksapp.dev?secret={VOORBEELD_BASE32_SEED}&issuer=ZAD"


def _voorbeeld_otp_status() -> OtpStatus:
    """A confirmed, unblocked second factor for the account pages."""
    return OtpStatus(registered=True, confirmed_at=datetime(2026, 9, 4, 10, 12, tzinfo=UTC))


def page_data(slug: str) -> dict[str, Any]:
    """The data a redesigned page needs.

    A plain table and no clever mechanism: every page needs other data. An unknown name yields
    an empty context, and then the page itself shows what is missing.
    """
    projects = all_projects()

    if slug == "account":
        # The account page shows what is in the session, and this preview has no login
        # service, so an example user stands here.
        return {
            "account": {
                "name": "Voorbeeld Gebruiker",
                "email": "voorbeeld@rijksapp.dev",
                "organisatie": "Rijksorganisatie voor Ontwikkeling, Digitalisering en Innovatie",
            },
            "otp_status": _voorbeeld_otp_status(),
        }

    if slug == "account-otp":
        # A confirmed factor: the richer of the two branches, with the date and the form that
        # replaces the factor.
        return {"status": _voorbeeld_otp_status()}

    if slug == "account-otp-registreren":
        # A running registration: the QR comes from the real generator, with a visibly
        # made-up otpauth URI.
        return {
            "qr_svg": qr_svg(VOORBEELD_OTPAUTH_URI),
            "base32_secret": VOORBEELD_BASE32_SEED,
            "pending": "VOORBEELDWAARDE-geen-echt-geheim",
            "pending_field": "pending",
        }

    if slug == "security":
        # The REAL policy, the way the introduction page shows the real catalog: a made-up
        # text would make the preview look right while the page is wrong.
        return {"security_policy_html": security_policy_html()}

    if slug == "invite-landing":
        # Both sign-in options on, so the page shows the choice it explains.
        return {
            "display_name": "Docs en Grist",
            "invite_key": "voorbeeldsleutel-geen-echte-uitnodiging",
            "message": "Welkom bij Docs en Grist.",
            "allow_sso": True,
            "allow_local": True,
            "identity_providers": [{"alias": "sso-rijk", "displayName": "SSO Rijk"}],
            "contact_email": "beheerder@voorbeeld.nl",
            "language": "nl",
        }

    if slug == "introductie":
        # NO example services: the introduction page shows the REAL catalog, which comes from
        # the code and needs no cluster. Without the ``navigation`` that comes out of it too:
        # ``_context()`` in lotc_router sets that itself, and ``**extra`` would write over it.
        from opi.http.render.lotc_switch import build_lotc_introduction

        catalog = build_lotc_introduction(None)
        return {key: value for key, value in catalog.items() if key != "navigation"}

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
            # The same shape the real route supplies; another shape would let the template
            # work on something that never occurs in production.
            "health": [{"label": "Healthy", "count": len(projects)}],
        }

    if slug in ("projects", "users"):
        return {"projects": projects}

    if slug == "services":
        # The filter answers "do I choose this myself or is it always there", which is the
        # question someone opens this page with.
        all_services = services_overview(projects)
        return {
            "projects": projects,
            "services": all_services,
            "service_filters": [
                ("", "Alle", len(all_services)),
                ("user", "Zelf te kiezen", sum(1 for s in all_services if not s["kind_label"])),
                ("system", "Altijd aan", sum(1 for s in all_services if s["kind_label"])),
            ],
        }

    if slug == "wizard":
        # The fields come from the same example series as /lotc/formulier, through the same
        # adapter, so this page shows the REAL form layer.
        from opi.forms.widgets.lotc import LOTCWidgetAdapter
        from opi.web.lotc_form_preview import EXAMPLE_FIELDS

        adapter = LOTCWidgetAdapter()
        rendered = [adapter.render_field(field) for field in EXAMPLE_FIELDS]
        return {
            "flow_title": "Nieuw project",
            "flow_description": "Vul de gegevens in. Je kunt tussentijds terug zonder iets kwijt te raken.",
            "steps": [
                # "past" and not "complete": the enum of step-bar-item is past/current/future,
                # and "complete" renders without a complaint but yields no finished step.
                {"label": "Project", "status": "past"},
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
        # The start page shows only an explanation and a button.
        return {}

    if slug == "wizard-page":
        # The REAL wizard, with the real steps and the real fields of the first step, so the
        # form layer can be looked at as a whole.
        from starlette.requests import Request

        from opi.forms.visualizers.flows import get_flow
        from opi.forms.wizard.resolver import get_section_metadata, resolve_active_sections
        from opi.forms.wizard.state import WizardState
        from opi.utils.nldd_iconen import to_nldd_icon
        from opi.web.router_wizard import _render_step_html

        flow = get_flow("create-project")
        sections = resolve_active_sections(flow, {})
        state = WizardState(
            flow_id="create-project",
            current_step=sections[0].section_id,
            active_sections=[section.section_id for section in sections],
        )
        # A bare request: ``_render_step_html`` only reads the design choice from it, and
        # without a query string or cookie that is the new design. The csrf token is empty
        # because the preview submits nothing.
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
        # The platform users are the people in the example projects, so this list comes from
        # the same source as the rest.
        members = {
            member["email"]: project["display_name"]
            for project in projects
            for member in project["users"]
            if member.get("email")
        }
        return {
            "users": [
                {
                    "id": str(number),
                    "email": email,
                    "full_name": email.split("@")[0].replace(".", " ").title(),
                    "created_at": datetime(2026, 8, 1, tzinfo=UTC),
                }
                for number, email in enumerate(sorted(members), start=1)
            ],
            "csrf_token": "",
            "success_message": "",
        }

    if slug == "admin-user-form":
        # The fields come from the same editables as the real route, through the same adapter.
        from opi.web.router_user_admin import _render_form_html

        return {
            "page_heading": "Gebruiker bewerken",
            "form_action": "/admin/users/1/edit",
            "form_html": _render_form_html(
                data={"email": "voorbeeldbeheerder@rijksoverheid.nl", "full_name": "Voorbeeldbeheerder"},
                edit_mode=True,
            ),
            "csrf_token": "",
        }

    if slug == "admin-usage":
        # Example figures, visibly round. The preview has no Grafana, so the notice about that
        # belongs on the page as well: the same state a real instance without a billing
        # datasource passes.
        months = [
            {"month": number, "name": name, "year": 2026, "gib": gib, "cost": round(gib * 27.0, 2)}
            for number, (name, gib) in enumerate([("January", 12.5), ("February", 13.0), ("March", 14.25)], start=1)
        ]
        return {
            "year": 2026,
            "month_data": months,
            "total_gib": round(sum(month["gib"] for month in months), 2),
            "total_cost": round(sum(month["cost"] for month in months), 2),
            "selected_namespace": "all",
            "namespace_options": [{"value": "all", "label": "Alle namespaces"}]
            + [{"value": f"rig-{project['name']}", "label": f"rig-{project['name']}"} for project in projects],
            "price_per_gib": 27.0,
            "has_billing_datasource": False,
        }

    if slug == "router":
        # THE REAL NAME AND THE REAL ADDRESSES, from dns_config. The page tells a visitor
        # which DNS record to put in their own zone, and those values are public and stand in
        # the code, so the preview shows what the real page shows.
        router_host = DEFAULT_ROUTER_HOSTNAME
        addresses = router_addresses_for(router_host)
        return {
            "router_host": router_host,
            "router_ipv4": addresses[0] if addresses else "",
            "router_ipv6": addresses[1] if addresses else "",
        }

    if slug == "admin-gedeelde-diensten":
        # VISIBLY MADE-UP thresholds and unmeasured services. The real "THRESHOLDS" and
        # "UNMEASURED_SERVICES" do NOT belong here: /lotc/bg/<name> asks for no session and no
        # role, while the admin page sits behind require_platform_admin, and publishing your
        # own thresholds and the list of what goes unmeasured tells an outsider exactly where
        # nobody is looking.
        #
        # The SHAPE is the real one, the same dataclasses the route uses, so a field added
        # there shows up here at once. The three blocks above stay at "wordt opgehaald...":
        # they are fetched lazily behind require_platform_admin and need a metrics source.
        return {
            "drempels": [
                Threshold(
                    naam="voorbeeld_vulling",
                    waarschuwing=60.0,
                    kritiek=80.0,
                    eenheid="%",
                    uitleg="Voorbeeldwaarde. De echte drempels staan op /admin/diensten zelf.",
                ),
                Threshold(
                    naam="voorbeeld_wachtenden",
                    waarschuwing=2.0,
                    kritiek=4.0,
                    eenheid="verbindingen",
                    uitleg="Voorbeeldwaarde, met een eenheid die in een eigen kolom hoort.",
                ),
                Threshold(
                    naam="voorbeeld_leeftijd",
                    waarschuwing=100_000_000.0,
                    kritiek=200_000_000.0,
                    eenheid="transacties",
                    uitleg="Voorbeeldwaarde, groot genoeg om de duizendtalgroepering te tonen.",
                ),
            ],
            "ongemeten": [
                UnmeasuredService(
                    naam="Voorbeelddienst",
                    reden="Voorbeeldtekst. Welke diensten hier echt staan is te zien op /admin/diensten.",
                    nodig="Voorbeeldtekst over wat er nodig zou zijn om hem wel te meten.",
                ),
                UnmeasuredService(
                    naam="Tweede voorbeelddienst",
                    reden="Er staan er twee, zodat de opbouw met meerdere meldingen te beoordelen is.",
                    nodig="Voorbeeldtekst.",
                ),
            ],
        }

    if slug == "admin-approvals":
        # One request per state, so the three labels stand next to each other, and TWO
        # services, because the page groups per service. Every item carries the same keys
        # ``collect_approval_items()`` delivers.
        requests = [
            {
                "service": "publish-on-web",
                "type": "subdomain",
                "label": "Subdomein",
                "subject": "voorbeeld.rijksapps.nl",
                "domain": "rijksapps.nl",
                "name": "voorbeeld",
                "current_status": "requested",
                "history": [{"date": datetime(2026, 8, 1, tzinfo=UTC), "by": "voorbeeldgebruiker"}],
            },
            {
                "service": "publish-on-web",
                "type": "domain",
                "label": "Domein",
                "subject": "voorbeeld.nl",
                "domain": "voorbeeld.nl",
                "name": "voorbeeld.nl",
                "current_status": "approved",
                "history": [{"date": datetime(2026, 7, 14, tzinfo=UTC), "by": "voorbeeldbeheerder"}],
            },
            {
                "service": "send-email",
                "type": "send-email",
                "label": "E-mail versturen",
                "subject": SERVICE_USE_SUBJECT,
                "domain": "",
                "name": projects[0]["name"],
                "current_status": "denied",
                "history": [{"date": datetime(2026, 8, 12, tzinfo=UTC), "by": "voorbeeldbeheerder"}],
            },
        ]
        # The groups come from the SAME function as on the real page. The template reads only
        # ``approval_groups``; ``approval_items`` stays next to it because the counter counts
        # REQUESTS and not groups.
        return {
            "projects_data": [
                {
                    "project_name": projects[0]["name"],
                    "approval_items": requests,
                    "approval_groups": group_by_service(requests),
                }
            ],
            "approvals_totaal": len(requests),
            "approvals_getoond": len(requests),
            "approval_status": "",
            "approval_statussen": APPROVAL_STATUSSEN,
            "success_message": "",
        }

    if slug in ("project-details", "project-tabs", "project-context"):
        context = build_details_context(available_projects()[-1]) or {}
        # The tabs come from PROJECT_TABS, the same list the real page uses: a copy of its own
        # here fell behind, which is what a second list does.
        context["tabs"] = {key: dict(tab) for key, tab in PROJECT_TABS.items()}
        # The preview has no cluster, so no measurements: the same state a real instance passes
        # when Prometheus is unreachable.
        context["usage"] = None
        context["usage_error"] = "Prometheus is niet verbonden in de proefopstelling."
        # The audit tab reads its rows from the database, which the preview has not.
        context["audit_rijen"] = _audit_examples()
        return context

    return {}


def _audit_examples() -> list[dict[str, Any]]:
    """Two audit rows in the shape ``build_lotc_audit`` yields: an action and a refusal."""
    return [
        {
            "time": datetime(2026, 10, 28, 9, 14, tzinfo=UTC),
            "actor": "beheerder@sandbox.rijksapp.dev",
            "actor_kind": "user",
            "action": "deployment.write",
            "subject": "demo/productie",
            "allowed": True,
            "cause": "",
            "commits": [{"sha": "9f1c2ab7d4", "short": "9f1c2ab7", "url": ""}],
        },
        {
            "time": datetime(2026, 10, 28, 8, 2, tzinfo=UTC),
            "actor": "demo/pijplijn",
            "actor_kind": "service-account",
            "action": "deployment.image-replace",
            "subject": "demo/productie",
            "allowed": False,
            "cause": "insufficient-role",
            "commits": [],
        },
    ]


def services_overview(projects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every visible service from the REAL registry, with who uses it.

    The registry and not a list of its own: a second list next to it drifts away from what the
    application actually offers.
    """
    from opi.services.services import ServiceAdapter
    from opi.utils.nldd_iconen import to_nldd_icon

    used_by: dict[str, list[str]] = {}
    for project in projects:
        for service in project["services"]:
            used_by.setdefault(service, []).append(project["display_name"])

    overview: list[dict[str, Any]] = []
    for service_type in ServiceAdapter.get_all_services():
        definition = ServiceAdapter.SERVICE_DEFINITIONS[service_type]
        if getattr(definition, "hidden", False):
            continue

        is_platform = definition.kind.value == "system"

        overview.append(
            {
                "name": service_type.value,
                "label": definition.name,
                "summary": definition.description,
                # Through the same translation table as the navigation: our icons carry Dutch
                # ROOS names, and an unknown name leaves the icon empty SILENTLY.
                "icon": to_nldd_icon(definition.icon),
                "color": definition.color,
                # A label only where it says something; a service you pick yourself is the
                # normal case and needs none.
                "kind_label": "altijd aan" if is_platform else "",
                "kind_type": "info",
                "help": definition.description,
                "used_by": used_by.get(service_type.value, []),
            }
        )
    return overview
