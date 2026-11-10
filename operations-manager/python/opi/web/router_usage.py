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

from opi.core.authz.actions import Action, action
from opi.core.cluster_config import get_cluster_config, get_namespace_prefix
from opi.core.config import settings
from opi.http.auth.auth_decorators import require_platform_admin, requires_sso
from opi.http.render.menu import get_menu_items
from opi.services.project_store import get_project_store

logger = logging.getLogger(__name__)

usage_router = APIRouter(prefix="/admin/usage", tags=["usage"])

DEFAULT_PRICE_PER_GIB = 27.0

# Cheap query over the hourly recording rule, the PrometheusRule operations-manager-billing
# in the odcn-production bootstrap overlay.
#
# ``sum_over_time`` adds up the hourly samples of a month, and dividing by the HOURS IN THAT
# MONTH gives the GiB held on average. That is the quantity billed, because the price is euro
# per GiB per MONTH: a namespace that held 10 GiB for half the month counts for 5, and a
# namespace that existed for part of the month counts pro rata on its own.
#
# THE WINDOW IS NOT THE DENOMINATOR. The window runs from the FIRST of the month
# (``{venster}``) while the denominator stays the whole month (``{noemer_uren}``), so the
# current month shows what has been built up so far and not a prediction of the whole month.
# The template marks that row as running, or half a month reads as a drop.
RECORDED_USAGE_QUERY = """round(
  sum(
    sum_over_time(rig:namespace_memory_billed_bytes{{namespace=~"{namespace_filter}"}}[{venster}])
  ) / {noemer_uren} / 1024^3
, 0.01) or on() vector(0)"""

# Heavy fallback that computes the same from the raw metrics, needed only for months from
# before the recording rule existed.
#
# Same window and same denominator as above, because the two routes have to yield the same
# number. The subquery with step 1h gives the same hourly samples as the recording rule, so
# dividing by the number of hours is enough.
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
    """One entry per month up to the current month."""
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
    """The project namespaces on this cluster, with the OPI namespace first."""
    prefix = get_namespace_prefix(cluster_name)
    projects = get_project_store().get_all()
    namespaces = sorted({f"{prefix}{project.name}" for project in projects})
    opi_namespace = get_cluster_config(cluster_name).get("namespace")
    if opi_namespace and opi_namespace not in namespaces:
        namespaces.insert(0, opi_namespace)
    return namespaces


def _is_current_month(year: int, month: int) -> bool:
    """Is this month still running, or is it closed?"""
    now = datetime.now(UTC)
    return year == now.year and month == now.month


def _get_month_end(year: int, month: int, days: int) -> datetime:
    """The evaluation time for a month query: end of month, or now for the current month."""
    if _is_current_month(year, month):
        return datetime.now(UTC)
    return datetime(year, month, days, 23, 59, 59, tzinfo=UTC)


def _window_and_divisor(year: int, month: int, days: int) -> tuple[str, int]:
    """The look-back window as a PromQL duration, and the hours to divide by.

    The two differ ONLY in the current month. The window is counted back from "now", so for a
    running month it has to reach the first of that month and not a whole month back, or it
    slides into the previous one. The denominator stays the whole month, so a running and a
    closed month yield the same quantity: GiB held over that month.
    """
    hours_in_month = days * 24
    if not _is_current_month(year, month):
        return f"{hours_in_month}h", hours_in_month

    now = datetime.now(UTC)
    start = datetime(year, month, 1, tzinfo=UTC)
    # Rounded up, so the window covers the first hour of the month, and at least one hour:
    # just after midnight on the first, a window of 0h is not a valid PromQL duration.
    elapsed_hours = max(1, ceil((now - start).total_seconds() / 3600))
    return f"{elapsed_hours}h", hours_in_month


async def _query_month_usage(
    month_info: dict[str, Any],
    namespace_filter: str,
    datasource_uid: str,
) -> dict[str, Any]:
    """Query memory usage for a single month via Grafana billing datasource."""
    from opi.connectors.grafana_prometheus import GrafanaPrometheusConnector

    connector = GrafanaPrometheusConnector()

    eval_time = _get_month_end(month_info["year"], month_info["month"], month_info["days"])
    venster, noemer_uren = _window_and_divisor(month_info["year"], month_info["month"], month_info["days"])

    async def run(query_template: str) -> float:
        query = query_template.format(
            namespace_filter=namespace_filter,
            venster=venster,
            noemer_uren=noemer_uren,
        )
        results = await connector.query(query, at=eval_time, datasource_uid=datasource_uid)
        return float(results[0].get("value", [None, "0"])[1]) if results else 0.0

    try:
        value = await run(RECORDED_USAGE_QUERY)
        if value == 0.0:
            # Months from before the recording rule have no recorded data.
            value = await run(MEMORY_USAGE_QUERY)
    except Exception:
        logger.exception("Failed to query billing data for %s %d", month_info["name"], month_info["year"])
        value = 0.0

    return {
        **month_info,
        "gib": value,
        # Without this flag a row halfway through the month reads as a drop.
        "loopt_nog": _is_current_month(month_info["year"], month_info["month"]),
    }


@usage_router.get("", response_class=HTMLResponse)
@action(Action.PLATFORM_USAGE)
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
                "loopt_nog": _is_current_month(month_info["year"], month_info["month"]),
            }
            for month_info in months
        )

    total_gib = round(sum(m["gib"] for m in month_data), 2)
    total_cost = round(sum(m["cost"] for m in month_data), 2)

    from opi.http.render.lotc_switch import build_lotc_admin, render

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
