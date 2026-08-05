"""The delete-attachment route answers with the shared progress fragment.

It used to return a dict, which the app's HTMLResponse default would call .encode() on
("'dict' object has no attribute 'encode'": an unhandled 500 even though the delete
succeeded), so it pinned JSONResponse. Since RC-29 it starts a task and answers with
rendered HTML instead, so what must hold is the opposite: the route is HTML, and it is
the POST the shared confirmation dialog addresses.
"""

from fastapi.responses import HTMLResponse, JSONResponse
from opi.web.router_attachments import attachments_router


def test_delete_attachment_route_answers_html():
    routes = [
        r for r in attachments_router.routes if getattr(r, "path", "").endswith("/attachments/{attachment_id}/delete")
    ]
    assert routes, "delete-attachment route not found"

    assert "POST" in routes[0].methods
    response_class = getattr(routes[0].response_class, "value", routes[0].response_class)
    assert response_class is HTMLResponse


def test_auth_user_route_uses_json_response():
    # GET /auth/user returns a dict from the session; auth_router inherits the app's
    # HTMLResponse default, so the route must pin JSONResponse to avoid a 500.
    from opi.api.auth_routes import auth_router

    routes = [r for r in auth_router.routes if getattr(r, "path", "").endswith("/user")]
    assert routes, "auth /user route not found"

    response_class = getattr(routes[0].response_class, "value", routes[0].response_class)
    assert response_class is JSONResponse
