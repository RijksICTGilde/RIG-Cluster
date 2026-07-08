"""Proof that a subdomain field error actually renders at the subdomain input.

Fix A keys the "niet beschikbaar" error to ``deployments[N]/subdomain``. This
verifies the render pipeline surfaces that message in the section HTML, instead
of silently anchoring it to the invisible deployment-group path.
"""

import pytest
from opi.forms.visualizers.wizard_sections import build_domain_section
from opi.web.router_detail_edit import _render_section_html


@pytest.mark.asyncio
async def test_subdomain_field_error_appears_in_rendered_html():
    section = build_domain_section(1, edit_mode=True)
    yaml_data = {
        "deployments": [
            {"name": "main"},
            {
                "name": "stable",
                "base-domain": "rijksapp.dev",
                "domain-format": "component.subdomain",
                "subdomain": "moza",
            },
        ],
    }
    message = "Het subdomein 'moza.rijksapp.dev' is niet beschikbaar, in gebruik door project 'mozad-dle'"

    html = _render_section_html(
        section,
        yaml_data,
        errors={"deployments[1]/subdomain": [message]},
        locked_services=None,
    )

    # The message is surfaced in the rendered field HTML (not swallowed).
    assert "niet beschikbaar" in html
    assert "mozad-dle" in html
