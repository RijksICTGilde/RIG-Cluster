"""Regression: the delete-attachment route must serialise as JSON.

The endpoint returns a dict on success. The app's default response class is
HTMLResponse, which would call dict.encode() and raise
"'dict' object has no attribute 'encode'" (an unhandled 500 even though the
delete succeeded). The route must therefore pin response_class=JSONResponse.
"""

from fastapi.responses import JSONResponse
from opi.web.router_attachments import attachments_router


def test_delete_attachment_route_uses_json_response():
    routes = [r for r in attachments_router.routes if getattr(r, "path", "").endswith("/attachments/{attachment_id}")]
    assert routes, "delete-attachment route not found"

    response_class = getattr(routes[0].response_class, "value", routes[0].response_class)
    assert response_class is JSONResponse
