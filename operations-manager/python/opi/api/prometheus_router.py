"""Prometheus metrics endpoint for scraping.

Exposes a /metrics endpoint in standard Prometheus text format.
This is separate from the existing metrics_router which queries
an external Prometheus instance for cluster metrics.
"""

import logging

from fastapi import APIRouter
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

logger = logging.getLogger(__name__)

prometheus_router: APIRouter = APIRouter(
    tags=["prometheus"],
    default_response_class=Response,
)


@prometheus_router.get("/metrics", include_in_schema=False)
async def prometheus_metrics() -> Response:
    """Prometheus scrape endpoint.

    Returns all registered metrics in Prometheus text exposition format.
    Includes built-in process/GC metrics and custom OPI collectors.
    """
    return Response(
        content=generate_latest(),
        media_type=CONTENT_TYPE_LATEST,
    )
