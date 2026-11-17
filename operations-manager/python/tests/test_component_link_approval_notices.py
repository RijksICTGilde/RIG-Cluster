"""Under a component's public links only the approvals that change the link's address show."""

from __future__ import annotations

from types import SimpleNamespace

from opi.http.render.templates_lotc import templates_lotc as templates
from opi.services.catalog.publish_on_web.urls import address_approval_notices
from opi.web.lotc_fixtures import page_data

DOMAIN_TEXT = "Het domein mijn-app.nl is aangevraagd en wacht op goedkeuring."
SENDER_TEXT = "Het afzenderadres noreply@rijksoverheid.nl is aangevraagd en wacht op goedkeuring."


def _render_components_tab(notices: list[dict[str, str]]) -> str:
    request = SimpleNamespace(
        scope={"type": "http"},
        headers={},
        cookies={},
        state=SimpleNamespace(),
        url=SimpleNamespace(path="/"),
        session={},
        query_params={},
    )
    data = page_data("project-tabs")
    data["project"] = {
        **data["project"],
        "components": [
            {
                "name": "frontend",
                "type": "single",
                "services": [],
                "ingress_links": [
                    {"deployment_name": "production", "cluster": "odcn-production", "url": "https://a", "path": "/"},
                    {"deployment_name": "production", "cluster": "odcn-production", "url": "https://b", "path": "/b"},
                ],
            }
        ],
    }
    data["approval_notices"] = {"production": notices}
    # Through the service's own filter, because that is what the page does: the general
    # template may not know which service owns the address.
    data["address_approval_notices"] = {"production": address_approval_notices(notices)}
    data["active_tab"] = "componenten"
    data["tabs"] = {key: {**tab, "url": "#"} for key, tab in data["tabs"].items()}
    return templates.env.get_template("bg/project-tabs.html.j2").render(request=request, navigation=[], **data)


def test_a_domain_notice_shows_once_under_the_links() -> None:
    html = _render_components_tab([{"service": "publish-on-web", "status": "requested", "text": DOMAIN_TEXT}])
    assert html.count(DOMAIN_TEXT) == 1


def test_a_sender_address_notice_does_not_show_under_the_links() -> None:
    html = _render_components_tab([{"service": "send-email", "status": "requested", "text": SENDER_TEXT}])
    assert SENDER_TEXT not in html
