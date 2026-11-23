"""``bg/_component-resources.html.j2``: the value per field, and a release button only on a
pinned field (RC-518).

The button shares its right with the route behind it (``can('component.update')``), the same
way ``_deployment-actions.html.j2`` does: checked here by replacing ``can`` in the render
context, as ``test_the_button_opens_the_confirmation`` does.
"""

from __future__ import annotations

from opi.http.render.templates_lotc import templates_lotc as templates
from starlette.requests import Request

PROJECT = "demo"
COMPONENT = "api"


def _request() -> Request:
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": [], "query_string": b""})
    request.state.csrf_token = "csrf-token"
    return request


def _rows(*, pinned: bool, set_at: str | None = None) -> list[dict]:
    return [
        {"field": "requests_memory", "label": "Memory request", "value": "64Mi", "pinned": False, "set_at": None},
        {
            "field": "limits_memory",
            "label": "Memory limit",
            "value": "900Mi",
            "pinned": pinned,
            "set_at": set_at,
        },
    ]


def _render(*, pinned: bool, set_at: str | None, allowed: bool) -> str:
    template = templates.env.get_template("bg/_component-resources.html.j2")
    return template.render(
        request=_request(),
        project={"name": PROJECT},
        component={"name": COMPONENT, "resource_rows": _rows(pinned=pinned, set_at=set_at)},
        can=lambda *_args, **_kwargs: allowed,
    )


def test_a_pinned_field_gets_the_release_button_and_the_date() -> None:
    html = _render(pinned=True, set_at="2026-08-20T10:00:00+00:00", allowed=True)

    assert "Laat de tuner dit weer regelen" in html
    assert "Vastgezet" in html
    assert f"/projects/{PROJECT}/componenten/{COMPONENT}/resources/limits_memory/loslaten" in html


def test_an_unpinned_field_gets_no_button() -> None:
    html = _render(pinned=False, set_at=None, allowed=True)

    assert "Laat de tuner dit weer regelen" not in html
    assert "Vastgezet" not in html


def test_without_the_right_the_button_stays_hidden_even_when_pinned() -> None:
    """The button shares its right with the route: without it there is nothing to click."""
    html = _render(pinned=True, set_at="2026-08-20T10:00:00+00:00", allowed=False)

    assert "Laat de tuner dit weer regelen" not in html
    # The pinned label may still show; only the ACTION is gated.
    assert "Vastgezet" in html


def test_every_field_shows_its_label_and_value() -> None:
    html = _render(pinned=False, set_at=None, allowed=True)

    assert "Memory request: 64Mi" in html
    assert "Memory limit: 900Mi" in html
