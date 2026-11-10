"""Maintenance middleware: a status page while the application is not fully ready.

Excludes health/metrics endpoints so Kubernetes probes still work.
"""

from typing import TYPE_CHECKING

from fastapi.responses import HTMLResponse
from starlette.middleware.base import BaseHTTPMiddleware

from opi.core.readiness import get_readiness_state

if TYPE_CHECKING:
    from fastapi import Request

# Paths that bypass the maintenance page
BYPASS_PATHS = {"/health", "/healthz", "/readyz", "/metrics", "/version"}


class MaintenanceMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Always let health/metrics endpoints through
        if request.url.path in BYPASS_PATHS:
            return await call_next(request)

        if get_readiness_state().is_ready:
            return await call_next(request)

        return _status_page()


#: The page an anonymous visitor gets while ZAD is starting. Deliberately without a service
#: list and without an error text: those name internal hosts, accounts and roles, and the
#: visitor here has not signed in. ``ServiceStatus.mark_failed`` logs them instead.
_STATUS_HTML = """<!DOCTYPE html>
<html lang="nl">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta http-equiv="refresh" content="30">
    <title>ZAD is tijdelijk niet beschikbaar</title>
    <style>
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #f5f5f5;
            margin: 0;
            padding: 0;
            display: flex;
            justify-content: center;
            align-items: center;
            min-height: 100vh;
        }
        .card {
            background: white;
            border-radius: 8px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
            max-width: 520px;
            width: 90%;
            overflow: hidden;
        }
        .header {
            background: #154273;
            color: white;
            padding: 24px;
        }
        .header h1 {
            margin: 0 0 8px 0;
            font-size: 1.3em;
        }
        .header p {
            margin: 0;
            opacity: 0.85;
            font-size: 0.9em;
        }
        .footer {
            padding: 16px 24px;
            background: #fafafa;
            border-top: 1px solid #e0e0e0;
            font-size: 0.85em;
            color: #666;
        }
    </style>
</head>
<body>
    <div class="card">
        <div class="header">
            <h1>ZAD is tijdelijk niet beschikbaar</h1>
            <p>Er wordt automatisch opnieuw geprobeerd.</p>
        </div>
        <div class="footer">
            Deze pagina wordt elke 30 seconden ververst.
        </div>
    </div>
</body>
</html>"""


def _status_page() -> HTMLResponse:
    return HTMLResponse(content=_STATUS_HTML, status_code=503)
