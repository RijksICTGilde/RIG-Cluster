"""
Metrics API endpoints for Prometheus metrics retrieval.

This module provides REST API endpoints for querying cluster and application metrics
from Prometheus.
"""

import logging

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse
from opi.connectors.prometheus import (
    PrometheusConnectionError,
    PrometheusConnector,
    PrometheusQueryError,
)

logger = logging.getLogger(__name__)

metrics_router: APIRouter = APIRouter(
    prefix="/api/metrics",
    tags=["metrics"],
    responses={
        404: {"description": "Not found"},
        503: {"description": "Prometheus not available"},
    },
)


@metrics_router.get("/health")
async def metrics_health() -> JSONResponse:
    """
    Check if Prometheus is connected and available.

    Returns:
        JSON response with connection status.
    """
    try:
        connector = PrometheusConnector()
        return JSONResponse(
            content={
                "status": "healthy" if connector.is_connected else "unhealthy",
                "prometheus_connected": connector.is_connected,
            },
            status_code=200 if connector.is_connected else 503,
        )
    except Exception as e:
        logger.error(f"Error checking metrics health: {e}")
        return JSONResponse(
            content={"status": "error", "message": str(e)},
            status_code=500,
        )


@metrics_router.get("/overview")
async def get_cluster_overview() -> JSONResponse:
    """
    Get a high-level overview of cluster metrics.

    Returns:
        JSON response with cluster overview including CPU, memory, pod count, etc.

    Example:
    ```bash
    curl "http://localhost:9595/api/metrics/overview"
    ```
    """
    try:
        connector = PrometheusConnector()
        overview = connector.get_cluster_overview()

        return JSONResponse(
            content={
                "status": "success",
                "data": overview,
            },
            status_code=200,
        )
    except PrometheusConnectionError as e:
        logger.warning(f"Prometheus not connected: {e}")
        raise HTTPException(status_code=503, detail="Prometheus is not available")
    except PrometheusQueryError as e:
        logger.error(f"Prometheus query failed: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to query metrics: {e}")
    except Exception as e:
        logger.error(f"Error getting cluster overview: {e}")
        raise HTTPException(status_code=500, detail=f"Error getting metrics: {e}")


@metrics_router.get("/cpu")
async def get_cpu_usage(namespace: str | None = Query(None, description="Filter by namespace")) -> JSONResponse:
    """
    Get CPU usage metrics grouped by pod.

    Args:
        namespace: Optional namespace to filter by.

    Returns:
        JSON response with CPU usage data per pod.

    Example:
    ```bash
    curl "http://localhost:9595/api/metrics/cpu"
    curl "http://localhost:9595/api/metrics/cpu?namespace=rig-system"
    ```
    """
    try:
        connector = PrometheusConnector()
        cpu_data = connector.get_cpu_usage_by_namespace(namespace)

        return JSONResponse(
            content={
                "status": "success",
                "namespace": namespace,
                "data": cpu_data,
            },
            status_code=200,
        )
    except PrometheusConnectionError as e:
        logger.warning(f"Prometheus not connected: {e}")
        raise HTTPException(status_code=503, detail="Prometheus is not available")
    except PrometheusQueryError as e:
        logger.error(f"Prometheus query failed: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to query CPU metrics: {e}")
    except Exception as e:
        logger.error(f"Error getting CPU usage: {e}")
        raise HTTPException(status_code=500, detail=f"Error getting CPU metrics: {e}")


@metrics_router.get("/memory")
async def get_memory_usage(namespace: str | None = Query(None, description="Filter by namespace")) -> JSONResponse:
    """
    Get memory usage metrics grouped by pod.

    Args:
        namespace: Optional namespace to filter by.

    Returns:
        JSON response with memory usage data per pod in bytes.

    Example:
    ```bash
    curl "http://localhost:9595/api/metrics/memory"
    curl "http://localhost:9595/api/metrics/memory?namespace=rig-system"
    ```
    """
    try:
        connector = PrometheusConnector()
        memory_data = connector.get_memory_usage_by_namespace(namespace)

        return JSONResponse(
            content={
                "status": "success",
                "namespace": namespace,
                "data": memory_data,
            },
            status_code=200,
        )
    except PrometheusConnectionError as e:
        logger.warning(f"Prometheus not connected: {e}")
        raise HTTPException(status_code=503, detail="Prometheus is not available")
    except PrometheusQueryError as e:
        logger.error(f"Prometheus query failed: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to query memory metrics: {e}")
    except Exception as e:
        logger.error(f"Error getting memory usage: {e}")
        raise HTTPException(status_code=500, detail=f"Error getting memory metrics: {e}")


@metrics_router.get("/pods/count")
async def get_pod_count(namespace: str | None = Query(None, description="Filter by namespace")) -> JSONResponse:
    """
    Get the count of running pods.

    Args:
        namespace: Optional namespace to filter by.

    Returns:
        JSON response with pod count.

    Example:
    ```bash
    curl "http://localhost:9595/api/metrics/pods/count"
    curl "http://localhost:9595/api/metrics/pods/count?namespace=rig-system"
    ```
    """
    try:
        connector = PrometheusConnector()
        pod_count = connector.get_pod_count(namespace)

        return JSONResponse(
            content={
                "status": "success",
                "namespace": namespace,
                "pod_count": pod_count,
            },
            status_code=200,
        )
    except PrometheusConnectionError as e:
        logger.warning(f"Prometheus not connected: {e}")
        raise HTTPException(status_code=503, detail="Prometheus is not available")
    except PrometheusQueryError as e:
        logger.error(f"Prometheus query failed: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to query pod count: {e}")
    except Exception as e:
        logger.error(f"Error getting pod count: {e}")
        raise HTTPException(status_code=500, detail=f"Error getting pod count: {e}")


@metrics_router.get("/pods/restarts")
async def get_pod_restarts(namespace: str | None = Query(None, description="Filter by namespace")) -> JSONResponse:
    """
    Get pod restart counts.

    Args:
        namespace: Optional namespace to filter by.

    Returns:
        JSON response with restart counts per pod.

    Example:
    ```bash
    curl "http://localhost:9595/api/metrics/pods/restarts"
    curl "http://localhost:9595/api/metrics/pods/restarts?namespace=rig-system"
    ```
    """
    try:
        connector = PrometheusConnector()
        restart_data = connector.get_pod_restarts(namespace)

        return JSONResponse(
            content={
                "status": "success",
                "namespace": namespace,
                "data": restart_data,
            },
            status_code=200,
        )
    except PrometheusConnectionError as e:
        logger.warning(f"Prometheus not connected: {e}")
        raise HTTPException(status_code=503, detail="Prometheus is not available")
    except PrometheusQueryError as e:
        logger.error(f"Prometheus query failed: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to query pod restarts: {e}")
    except Exception as e:
        logger.error(f"Error getting pod restarts: {e}")
        raise HTTPException(status_code=500, detail=f"Error getting pod restarts: {e}")


@metrics_router.get("/network")
async def get_network_metrics(namespace: str | None = Query(None, description="Filter by namespace")) -> JSONResponse:
    """
    Get network traffic metrics (receive and transmit rates).

    Args:
        namespace: Optional namespace to filter by.

    Returns:
        JSON response with network receive and transmit rates per pod.

    Example:
    ```bash
    curl "http://localhost:9595/api/metrics/network"
    curl "http://localhost:9595/api/metrics/network?namespace=rig-system"
    ```
    """
    try:
        connector = PrometheusConnector()
        receive_data = connector.get_network_receive_bytes(namespace)
        transmit_data = connector.get_network_transmit_bytes(namespace)

        return JSONResponse(
            content={
                "status": "success",
                "namespace": namespace,
                "data": {
                    "receive": receive_data,
                    "transmit": transmit_data,
                },
            },
            status_code=200,
        )
    except PrometheusConnectionError as e:
        logger.warning(f"Prometheus not connected: {e}")
        raise HTTPException(status_code=503, detail="Prometheus is not available")
    except PrometheusQueryError as e:
        logger.error(f"Prometheus query failed: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to query network metrics: {e}")
    except Exception as e:
        logger.error(f"Error getting network metrics: {e}")
        raise HTTPException(status_code=500, detail=f"Error getting network metrics: {e}")


@metrics_router.get("/query")
async def custom_query(query: str = Query(..., description="PromQL query to execute")) -> JSONResponse:
    """
    Execute a custom PromQL query.

    Args:
        query: The PromQL query to execute.

    Returns:
        JSON response with query results.

    Example:
    ```bash
    curl "http://localhost:9595/api/metrics/query?query=up"
    curl "http://localhost:9595/api/metrics/query?query=sum(rate(container_cpu_usage_seconds_total[5m]))"
    ```
    """
    try:
        connector = PrometheusConnector()
        result = connector.custom_query(query)

        return JSONResponse(
            content={
                "status": "success",
                "query": query,
                "data": result,
            },
            status_code=200,
        )
    except PrometheusConnectionError as e:
        logger.warning(f"Prometheus not connected: {e}")
        raise HTTPException(status_code=503, detail="Prometheus is not available")
    except PrometheusQueryError as e:
        logger.error(f"Prometheus query failed: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to execute query: {e}")
    except Exception as e:
        logger.error(f"Error executing custom query: {e}")
        raise HTTPException(status_code=500, detail=f"Error executing query: {e}")
