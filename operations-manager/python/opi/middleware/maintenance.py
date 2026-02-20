"""
Maintenance middleware that shows a status page when the application
is not fully ready (e.g., database or Keycloak unavailable).

Excludes health/metrics endpoints so Kubernetes probes still work.
"""

import logging

from fastapi import Request
from fastapi.responses import HTMLResponse
from starlette.middleware.base import BaseHTTPMiddleware

from opi.core.readiness import get_readiness_state

logger = logging.getLogger(__name__)

# Paths that bypass the maintenance page
BYPASS_PATHS = {"/health", "/healthz", "/readyz", "/metrics"}


class MaintenanceMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Always let health/metrics endpoints through
        if request.url.path in BYPASS_PATHS:
            return await call_next(request)

        readiness = get_readiness_state()
        if readiness.is_ready:
            return await call_next(request)

        # Build status page
        return _build_status_page(readiness)


def _build_status_page(readiness) -> HTMLResponse:
    """Build an HTML status page showing which services are unavailable."""
    services_html = ""
    for service in readiness.get_all_services():
        if service.ready:
            status_icon = "&#10003;"
            status_class = "color: #2e7d32"
            status_text = "Beschikbaar"
            detail = ""
        else:
            status_icon = "&#10007;"
            status_class = "color: #c62828"
            status_text = "Niet beschikbaar"
            detail = f'<div style="color: #666; font-size: 0.85em; margin-top: 4px;">{service.error}</div>'

        services_html += f"""
        <div style="padding: 12px 16px; border-bottom: 1px solid #e0e0e0; display: flex; align-items: flex-start; gap: 12px;">
            <span style="{status_class}; font-size: 1.3em; font-weight: bold; line-height: 1;">{status_icon}</span>
            <div>
                <span style="font-weight: 500;">{service.display_name}</span>
                <span style="{status_class}; margin-left: 8px; font-size: 0.9em;">{status_text}</span>
                {detail}
            </div>
        </div>"""

    html = f"""<!DOCTYPE html>
<html lang="nl">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta http-equiv="refresh" content="30">
    <title>Operations Manager - Opstarten</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #f5f5f5;
            margin: 0;
            padding: 0;
            display: flex;
            justify-content: center;
            align-items: center;
            min-height: 100vh;
        }}
        .card {{
            background: white;
            border-radius: 8px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
            max-width: 520px;
            width: 90%;
            overflow: hidden;
        }}
        .header {{
            background: #154273;
            color: white;
            padding: 24px;
        }}
        .header h1 {{
            margin: 0 0 8px 0;
            font-size: 1.3em;
        }}
        .header p {{
            margin: 0;
            opacity: 0.85;
            font-size: 0.9em;
        }}
        .services {{
            padding: 0;
        }}
        .footer {{
            padding: 16px 24px;
            background: #fafafa;
            border-top: 1px solid #e0e0e0;
            font-size: 0.85em;
            color: #666;
        }}
    </style>
</head>
<body>
    <div class="card">
        <div class="header">
            <h1>Operations Manager is aan het opstarten</h1>
            <p>Een of meer services zijn nog niet beschikbaar. Er wordt elke 60 seconden opnieuw geprobeerd.</p>
        </div>
        <div class="services">
            {services_html}
        </div>
        <div class="footer">
            Deze pagina wordt automatisch elke 30 seconden ververst.
        </div>
    </div>
</body>
</html>"""

    return HTMLResponse(content=html, status_code=503)
