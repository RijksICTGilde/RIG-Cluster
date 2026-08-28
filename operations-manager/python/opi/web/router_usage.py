"""Web routes for platform usage and cost overview (admin-only)."""

from __future__ import annotations

import calendar
import logging
from datetime import UTC, datetime
from math import ceil
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from starlette.responses import Response

from opi.core.auth_decorators import require_platform_admin, requires_sso
from opi.core.cluster_config import get_cluster_config, get_namespace_prefix
from opi.core.config import settings
from opi.services.project_store import get_project_store
from opi.web.menu import get_menu_items

logger = logging.getLogger(__name__)

usage_router = APIRouter(prefix="/admin/usage", tags=["usage"])

DEFAULT_PRICE_PER_GIB = 27.0

# Cheap query over the hourly recording rule (PrometheusRule
# operations-manager-billing in de odcn-production bootstrap-overlay).
#
# WAT ER UIT KOMT. sum_over_time telt de uurlijkse samples van een maand op; delen door
# het aantal UREN IN DIE MAAND geeft het aantal GiB dat er gemiddeld gehouden is. Dat is
# geen tussenstap maar de grootheid waarop gefactureerd wordt: de prijs is euro per GiB
# per MAAND, dus een namespace die de halve maand 10 GiB hield telt voor 5. Namespaces
# die maar een deel van de maand bestonden tellen zo vanzelf naar rato mee, en de
# maandbedragen zijn optelbaar tot een jaarbedrag.
#
# HET VENSTER IS NIET HETZELFDE ALS DE NOEMER, en dat was de bug. Hier stond een vast
# venster van {days}d dat op het moment van bevragen eindigde. Voor een afgesloten maand
# klopt dat, want daar wordt op de laatste dag van de maand geevalueerd. Voor de LOPENDE
# maand werd op "nu" geevalueerd, en dan reikte een venster van 31 dagen terug tot in de
# vorige maand: op 28 augustus 2026 stonden er vier dagen juli in de augustusrij en
# ontbraken de laatste drie dagen van augustus. Het venster loopt daarom vanaf de EERSTE
# van de maand ({venster}), terwijl de noemer de hele maand blijft ({noemer_uren}).
#
# De lopende maand toont daarmee wat er tot nu toe is opgebouwd en niet een voorspelling
# van de hele maand. Dat is dezelfde rekensom als bij een afgesloten maand en hetzelfde
# als op een rekening: het getal groeit door tot het einde van de maand. Het sjabloon
# merkt die rij daarom aan als lopend, anders leest een halve maand als een daling.
RECORDED_USAGE_QUERY = """round(
  sum(
    sum_over_time(rig:namespace_memory_billed_bytes{{namespace=~"{namespace_filter}"}}[{venster}])
  ) / {noemer_uren} / 1024^3
, 0.01) or on() vector(0)"""

# Zware fallback die hetzelfde berekent uit de ruwe metrics. Alleen nodig
# voor maanden van voor de recording rule (uitgerold juni 2026); kan weg
# zodra er een vol jaar aan recorded data bestaat.
#
# Zelfde venster en zelfde noemer als hierboven, want de twee wegen horen hetzelfde
# getal op te leveren. De subquery levert met stap 1h dezelfde uurlijkse samples als de
# recording rule, dus delen door het aantal uren is genoeg; hier stond eerst een
# omweg langs byte-seconden (* 3600 gedeeld door {days} * 86400) die op precies
# hetzelfde neerkwam.
MEMORY_USAGE_QUERY = """round(
  sum((
    sum_over_time(
      (
        sum by(namespace,pod) (kube_pod_resource_request{{
          job="scheduler",
          namespace=~"{namespace_filter}",
          resource="memory"
        }})
        +
        clamp_min(
          sum by(namespace,pod) (container_memory_working_set_bytes{{
            job="kubelet",
            metrics_path="/metrics/cadvisor",
            namespace=~"{namespace_filter}",
            container!="",
            image!="",
            prometheus!="openshift-monitoring/k8s"
          }})
          -
          sum by(namespace,pod) (kube_pod_resource_request{{
            job="scheduler",
            namespace=~"{namespace_filter}",
            resource="memory"
          }}),
          0
        )
      )[{venster}:1h]
    )
  ) / {noemer_uren} / 1024^3)
, 0.01) or on() vector(0)"""


def _get_months_for_year(year: int) -> list[dict[str, Any]]:
    """Get month info for each month up to the current month."""
    now = datetime.now(UTC)
    months = []
    for month in range(1, 13):
        if year == now.year and month > now.month:
            break
        days_in_month = calendar.monthrange(year, month)[1]
        months.append(
            {
                "month": month,
                "name": calendar.month_name[month],
                "days": days_in_month,
                "year": year,
            }
        )
    return months


def _get_namespace_filter(namespace: str | None, cluster_name: str) -> str:
    """Build the namespace regex filter for PromQL."""
    prefix = get_namespace_prefix(cluster_name)
    if namespace and namespace != "all":
        return namespace
    return f"{prefix}.*"


def _get_available_namespaces(cluster_name: str) -> list[str]:
    """Get available project namespaces from the project service."""
    prefix = get_namespace_prefix(cluster_name)
    projects = get_project_store().get_all()
    namespaces = sorted({f"{prefix}{project.name}" for project in projects})
    opi_namespace = get_cluster_config(cluster_name).get("namespace")
    if opi_namespace and opi_namespace not in namespaces:
        namespaces.insert(0, opi_namespace)
    return namespaces


def _is_lopende_maand(year: int, month: int) -> bool:
    """Loopt deze maand nog, of is hij afgesloten?"""
    now = datetime.now(UTC)
    return year == now.year and month == now.month


def _get_month_end(year: int, month: int, days: int) -> datetime:
    """Get the evaluation time for a month query (end of month, or now if current month)."""
    if _is_lopende_maand(year, month):
        return datetime.now(UTC)
    return datetime(year, month, days, 23, 59, 59, tzinfo=UTC)


def _venster_en_noemer(year: int, month: int, days: int) -> tuple[str, int]:
    """Het terugkijkvenster en het aantal uren waardoor gedeeld wordt.

    De twee lopen ALLEEN in de lopende maand uiteen, en dat is precies waar het eerder
    misging. Het venster wordt vanaf "nu" teruggerekend, dus voor een lopende maand moet
    het tot de eerste van die maand reiken en niet een volle maand terug - anders schuift
    het de vorige maand in. De noemer blijft de hele maand, zodat een lopende en een
    afgesloten maand dezelfde grootheid opleveren: GiB gehouden over die maand.

    Returns:
        Het venster als PromQL-duur (bijvoorbeeld ``"672h"``) en de noemer in uren.
    """
    uren_in_maand = days * 24
    if not _is_lopende_maand(year, month):
        return f"{uren_in_maand}h", uren_in_maand

    now = datetime.now(UTC)
    begin = datetime(year, month, 1, tzinfo=UTC)
    # Naar boven afgerond, zodat het venster het eerste uur van de maand omvat; en
    # minstens een uur, want vlak na middernacht op de eerste is een venster van 0h geen
    # geldige PromQL-duur.
    verstreken_uren = max(1, ceil((now - begin).total_seconds() / 3600))
    return f"{verstreken_uren}h", uren_in_maand


async def _query_month_usage(
    month_info: dict[str, Any],
    namespace_filter: str,
    datasource_uid: str,
) -> dict[str, Any]:
    """Query memory usage for a single month via Grafana billing datasource."""
    from opi.connectors.grafana_prometheus import GrafanaPrometheusConnector

    connector = GrafanaPrometheusConnector()

    eval_time = _get_month_end(month_info["year"], month_info["month"], month_info["days"])
    venster, noemer_uren = _venster_en_noemer(month_info["year"], month_info["month"], month_info["days"])

    async def run(query_template: str) -> float:
        query = query_template.format(
            namespace_filter=namespace_filter,
            venster=venster,
            noemer_uren=noemer_uren,
        )
        results = await connector.custom_query(query, datasource_uid=datasource_uid, eval_time=eval_time)
        return float(results[0].get("value", [None, "0"])[1]) if results else 0.0

    try:
        value = await run(RECORDED_USAGE_QUERY)
        if value == 0.0:
            # Maanden van voor de recording rule hebben geen recorded data;
            # val terug op de zware raw query
            value = await run(MEMORY_USAGE_QUERY)
    except Exception:
        logger.exception("Failed to query billing data for %s %d", month_info["name"], month_info["year"])
        value = 0.0

    return {
        **month_info,
        "gib": value,
        # Een lopende maand is nog niet vol. Zonder deze vlag leest een rij die halverwege
        # de maand op de helft staat als een daling, terwijl er alleen nog minder maand is.
        "loopt_nog": _is_lopende_maand(month_info["year"], month_info["month"]),
    }


@usage_router.get("", response_class=HTMLResponse)
@requires_sso
async def usage_overview(request: Request) -> Response:
    """Show the usage and cost overview page."""
    user = require_platform_admin(request)

    year = int(request.query_params.get("year", datetime.now(UTC).year))
    namespace = request.query_params.get("namespace", "all")
    price_per_gib = float(request.query_params.get("price", DEFAULT_PRICE_PER_GIB))

    cluster_name = settings.CLUSTER_MANAGER
    datasource_uid = settings.GRAFANA_BILLING_DATASOURCE_UID

    months = _get_months_for_year(year)
    available_namespaces = _get_available_namespaces(cluster_name)
    namespace_filter = _get_namespace_filter(namespace, cluster_name)

    # Query each month
    month_data: list[dict[str, Any]] = []
    if datasource_uid:
        for month_info in months:
            result = await _query_month_usage(month_info, namespace_filter, datasource_uid)
            result["cost"] = round(result["gib"] * price_per_gib, 2)
            month_data.append(result)
    else:
        month_data.extend(
            {
                **month_info,
                "gib": 0.0,
                "cost": 0.0,
                "loopt_nog": _is_lopende_maand(month_info["year"], month_info["month"]),
            }
            for month_info in months
        )

    total_gib = round(sum(m["gib"] for m in month_data), 2)
    total_cost = round(sum(m["cost"] for m in month_data), 2)

    # Dezelfde gegevens, twee weergaven; zie opi/web/lotc_switch.py.
    from opi.web.lotc_switch import build_lotc_admin, render

    return render(
        request,
        template="bg/admin-usage.html.j2",
        context={
            "request": request,
            "menu_items": get_menu_items(user),
            "year": year,
            "month_data": month_data,
            "total_gib": total_gib,
            "total_cost": total_cost,
            "selected_namespace": namespace,
            "namespace_options": [{"value": "all", "label": "Alle namespaces"}]
            + [{"value": ns, "label": ns} for ns in available_namespaces],
            "price_per_gib": price_per_gib,
            "has_billing_datasource": datasource_uid is not None,
            **build_lotc_admin(user=user, current_path="/admin/usage"),
        },
    )
