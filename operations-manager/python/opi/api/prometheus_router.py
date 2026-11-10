"""The /metrics endpoint this instance is scraped on, in Prometheus text format."""

import logging

from fastapi import APIRouter
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.requests import Request

from opi.http.auth.endpoint_util import validate_metrics_scrape_token

logger = logging.getLogger(__name__)

prometheus_router: APIRouter = APIRouter(
    tags=["prometheus"],
    default_response_class=Response,
)


@prometheus_router.get("/metrics", include_in_schema=False)
@validate_metrics_scrape_token
async def prometheus_metrics(request: Request) -> Response:
    return Response(
        content=generate_latest(),
        media_type=CONTENT_TYPE_LATEST,
    )
