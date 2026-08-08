"""
Metrics Explorer web route for browsing Prometheus metrics by service.

Provides a UI to select a monitored service, discover its available metrics,
and view them in the Prometheus graph UI via an iframe.
"""

import asyncio
import logging

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from opi.core.auth_decorators import get_current_user, requires_sso
from opi.core.config import settings
from opi.web.lotc_switch import render
from opi.web.menu import get_menu_items
from opi.web.navigation_lotc import get_navigation

logger = logging.getLogger(__name__)

metrics_explorer_router = APIRouter()

# Known services and how to find their metrics in Prometheus.
# Each entry maps a display name to a PromQL match selector that identifies
# the metrics belonging to that service.
MONITORED_SERVICES: list[dict[str, str]] = [
    {
        "id": "cloudnative-pg",
        "name": "PostgreSQL (rig-db)",
        "description": "CloudNativePG database metrics (scrape: 2h)",
        "match": '{job="cloudnative-pg"}',
        "range": "7d",
    },
    {
        "id": "minio",
        "name": "MinIO",
        "description": "Object storage metrics (scrape: 2h)",
        "match": '{job="minio"}',
        "range": "7d",
    },
    {
        "id": "keycloak",
        "name": "Keycloak",
        "description": "Identity provider metrics (scrape: 2h)",
        "match": '{job="keycloak-rig-metrics"}',
        "range": "7d",
    },
    {
        "id": "operations-manager",
        "name": "Operations Manager",
        "description": "OPI internal metrics (via kubernetes-pods scrape)",
        "match": '{job="kubernetes-pods",app="operations-manager"}',
        "range": "1h",
    },
    {
        "id": "kubernetes-pods",
        "name": "Kubernetes Pods (all)",
        "description": "All pod metrics scraped via annotations",
        "match": '{job="kubernetes-pods"}',
        "range": "1h",
    },
    {
        "id": "prometheus",
        "name": "Prometheus",
        "description": "Prometheus server internal metrics",
        "match": '{job="prometheus"}',
        "range": "1h",
    },
]


def _get_prometheus_external_url() -> str:
    """Return the browser-accessible Prometheus URL."""
    return settings.PROMETHEUS_EXTERNAL_URL or settings.PROMETHEUS_URL


@metrics_explorer_router.get("/metrics-explorer", response_class=HTMLResponse)
@requires_sso
async def metrics_explorer_page(request: Request):
    """Serve the metrics explorer page."""
    user = get_current_user(request)

    return render(
        request,
        roos="metrics-explorer.html.j2",
        lotc="bg/metrics-explorer.html.j2",
        context={
            "request": request,
            "title": "Metrics Explorer",
            "menu_items": get_menu_items(user),
            # De navigatie van de nieuwe schil. Kost niets als de bestaande weergave
            # gekozen is: dat sjabloon leest hem niet.
            "navigation": get_navigation(user, current_path="/metrics-explorer"),
            "user": user,
            "services": MONITORED_SERVICES,
            "prometheus_url": _get_prometheus_external_url(),
        },
    )


@metrics_explorer_router.get("/ui/metrics-explorer/metrics/{service_id}", response_class=JSONResponse)
@requires_sso
async def get_service_metrics(request: Request, service_id: str):
    """
    Return the list of metric names available for a given service.

    Queries Prometheus /api/v1/series with the service's match selector,
    then extracts unique __name__ values.
    """
    # Find the service definition
    service = next((s for s in MONITORED_SERVICES if s["id"] == service_id), None)
    if not service:
        return JSONResponse(status_code=404, content={"error": f"Unknown service: {service_id}"})

    try:
        from opi.connectors.prometheus import PrometheusConnector

        prom = PrometheusConnector()
        # discover_metric_names does a blocking requests.get; run it off the event loop
        # so this async route does not stall other requests for up to the 10s HTTP timeout.
        metric_names = await asyncio.to_thread(prom.discover_metric_names, service["match"])
        return JSONResponse(content={"metrics": metric_names, "count": len(metric_names)})

    except Exception as e:
        logger.error(f"Failed to fetch metrics for service {service_id}: {e}")
        return JSONResponse(status_code=500, content={"error": "Failed to fetch metrics"})
