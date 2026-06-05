"""Web routes for platform usage and cost overview (admin-only)."""

from __future__ import annotations

import calendar
import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from opi.core.auth_decorators import get_current_user, requires_sso
from opi.core.cluster_config import get_cluster_config, get_namespace_prefix
from opi.core.config import settings
from opi.core.templates import get_templates
from opi.services.project_service import get_project_service
from opi.web.menu import get_menu_items

logger = logging.getLogger(__name__)

usage_router = APIRouter(prefix="/admin/usage", tags=["usage"])

DEFAULT_PRICE_PER_GIB = 27.0

# Cheap query over the hourly recording rule (PrometheusRule
# operations-manager-billing in de odcn-production bootstrap-overlay).
# sum_over_time over de ruwe samples gedeeld door het verwachte aantal
# samples (24 per dag) geeft het tijdgewogen maandgemiddelde; namespaces
# die maar een deel van de maand bestonden tellen naar rato mee.
RECORDED_USAGE_QUERY = """round(
  sum(
    sum_over_time(rig:namespace_memory_billed_bytes{{namespace=~"{namespace_filter}"}}[{days}d])
  ) / ({days} * 24) / 1024^3
, 0.01) or on() vector(0)"""

# Zware fallback die hetzelfde berekent uit de ruwe metrics. Alleen nodig
# voor maanden van voor de recording rule (uitgerold juni 2026); kan weg
# zodra er een vol jaar aan recorded data bestaat.
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
      )[{days}d:1h]
    ) * 3600
  ) / ({days} * 86400) / 1024^3)
, 0.01) or on() vector(0)"""


def _require_admin(request: Request) -> dict[str, Any]:
    """Return the current user dict or raise 403 if not an admin."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Niet ingelogd")
    email = user.get("email", "").lower()
    project_service = get_project_service()
    if not project_service.is_admin(email):
        raise HTTPException(status_code=403, detail="Alleen beheerders hebben toegang")
    return user


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
    project_service = get_project_service()
    projects = project_service.get_all_projects()
    namespaces = sorted({f"{prefix}{name}" for name in projects})
    opi_namespace = get_cluster_config(cluster_name).get("namespace")
    if opi_namespace and opi_namespace not in namespaces:
        namespaces.insert(0, opi_namespace)
    return namespaces


def _get_month_end(year: int, month: int, days: int) -> datetime:
    """Get the evaluation time for a month query (end of month, or now if current month)."""
    now = datetime.now(UTC)
    if year == now.year and month == now.month:
        return now
    return datetime(year, month, days, 23, 59, 59, tzinfo=UTC)


async def _query_month_usage(
    month_info: dict[str, Any],
    namespace_filter: str,
    datasource_uid: str,
) -> dict[str, Any]:
    """Query memory usage for a single month via Grafana billing datasource."""
    from opi.connectors.grafana_prometheus import GrafanaPrometheusConnector

    connector = GrafanaPrometheusConnector()

    eval_time = _get_month_end(month_info["year"], month_info["month"], month_info["days"])

    async def run(query_template: str) -> float:
        query = query_template.format(
            namespace_filter=namespace_filter,
            days=month_info["days"],
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
    }


@usage_router.get("", response_class=HTMLResponse)
@requires_sso
async def usage_overview(request: Request) -> HTMLResponse:
    """Show the usage and cost overview page."""
    user = _require_admin(request)

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
        month_data.extend({**month_info, "gib": 0.0, "cost": 0.0} for month_info in months)

    total_gib = round(sum(m["gib"] for m in month_data), 2)
    total_cost = round(sum(m["cost"] for m in month_data), 2)

    templates = get_templates()
    return templates.TemplateResponse(
        "admin/usage.html.j2",
        {
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
        },
    )
