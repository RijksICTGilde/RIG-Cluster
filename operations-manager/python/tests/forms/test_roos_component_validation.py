"""Tests that rendered wizard HTML passes ROOS component validation.

These tests render each wizard section with sample data and pass the
resulting HTML through the ROOS component preprocessor.  If any
``c-*`` tag uses an invalid attribute, the preprocessor raises a
``RuntimeError`` — catching these errors at test time rather than at
runtime.
"""

from __future__ import annotations

import pytest
from opi.core.templates import _component_ext
from opi.forms.i18n import get_default_nl_translator
from opi.forms.renderer import FormRenderer
from opi.forms.visualizers.wizard_sections import (
    ALL_SECTIONS,
    AUTH_WALL_CONFIG_SECTION,
    COMPONENTS_SECTION,
    CONFIG_DISPLAY_SECTION,
    DEPLOYMENTS_SECTION,
    DOMAIN_SECTION,
    IDENTITY_SECTION,
    KEYCLOAK_CONFIG_SECTION,
    POSTGRESQL_CONFIG_SECTION,
    SERVICES_SECTION,
    TEAM_SECTION,
    WIZARD_DEPLOYMENT_SECTION,
)
from opi.forms.widgets.roos import ROOSWidgetAdapter


def _create_renderer() -> FormRenderer:
    return FormRenderer(
        widget_adapter=ROOSWidgetAdapter(),
        translator=get_default_nl_translator(),
    )


def _validate_roos_html(html: str, label: str = "test") -> None:
    """Pass HTML through the ROOS preprocessor; raises on invalid components."""
    _component_ext.preprocess(html, name=f"roos_validation_{label}", filename=None)


# ---------------------------------------------------------------------------
# Sample data per section — enough to exercise all widgets.
# ---------------------------------------------------------------------------

_IDENTITY_DATA = {
    "display-name": "Test Project",
    "description": "Een testproject",
    "clusters": ["local"],
}

_SERVICES_DATA = {
    "services": ["publish-on-web", "keycloak", "namespace-postgresql-database"],
}

_TEAM_DATA = {
    "users": [
        {"email": "admin@test.nl", "role": "admin"},
        {"email": "dev@test.nl", "role": "developer"},
    ],
}

_COMPONENTS_DATA = {
    "services": ["publish-on-web", "keycloak"],
    "components": [
        {
            "name": "frontend",
            "image": "nginx:latest",
            "ports": {"inbound": [8000], "outbound": [80, 443]},
            "resources": {
                "cpu": {"request": "50m", "limit": "1"},
                "memory": {"request": "256Mi", "limit": "1Gi"},
            },
            "uses-services": ["publish-on-web"],
            "aliases": {"APP_NAME": "frontend"},
        },
    ],
}

_DEPLOYMENTS_DATA = {
    **_COMPONENTS_DATA,
    "deployments": [
        {
            "name": "production",
            "cluster": "local",
            "repository": "main-repo",
            "subdomain": "app",
            "components": [
                {"reference": "frontend", "image": "nginx:latest", "imagePullPolicy": "Always"},
            ],
        },
    ],
}

_CONFIG_DATA = {
    "config": {
        "age-public-key": "age1ql3z7hjy54pw3hyww5ayyfg7zqgvc7w3j2elw8zmrj2kg5sfn9aqmcac8p",
        "age-private-key": "-----BEGIN AGE ENCRYPTED FILE-----\ndata\n-----END AGE ENCRYPTED FILE-----",
        "api-key": "-----BEGIN AGE ENCRYPTED FILE-----\nsecret\n-----END AGE ENCRYPTED FILE-----",
    },
}

_KEYCLOAK_DATA = {
    **_SERVICES_DATA,
    "services": [
        "publish-on-web",
        {
            "keycloak": {
                "config": {
                    "template": "sso-only",
                    "additional_redirect_uris": ["http://localhost:8080/*"],
                    "restrict-access": {
                        "enabled": True,
                        "realm-role": "allowed-user",
                        "error-message": "${accessDeniedNoPermission}",
                    },
                    "additional-clients": [
                        {"name": "api-client", "redirect-uris": ["http://localhost:9090/*"]},
                    ],
                },
            },
        },
    ],
}

_POSTGRESQL_DATA = {
    "services": [
        {
            "namespace-postgresql-database": {
                "config": {"instances": 1, "storage": "1Gi"},
            },
        },
    ],
}

_AUTH_WALL_DATA = {
    "services": [
        {
            "authorization-wall": {
                "config": {"banner": "Welkom"},
            },
        },
    ],
}

_DOMAIN_DATA = {
    "deployments": [
        {
            "name": "main",
            "domain-mode": "component-specific",
        },
    ],
}


# ---------------------------------------------------------------------------
# Map each section to appropriate sample data.
# ---------------------------------------------------------------------------

SECTION_DATA = {
    "identity": (IDENTITY_SECTION, _IDENTITY_DATA),
    "services": (SERVICES_SECTION, _SERVICES_DATA),
    "team": (TEAM_SECTION, _TEAM_DATA),
    "components": (COMPONENTS_SECTION, _COMPONENTS_DATA),
    "deployments": (DEPLOYMENTS_SECTION, _DEPLOYMENTS_DATA),
    "config": (CONFIG_DISPLAY_SECTION, _CONFIG_DATA),
    "keycloak-config": (KEYCLOAK_CONFIG_SECTION, _KEYCLOAK_DATA),
    "postgresql-config": (POSTGRESQL_CONFIG_SECTION, _POSTGRESQL_DATA),
    "auth-wall-config": (AUTH_WALL_CONFIG_SECTION, _AUTH_WALL_DATA),
    "domains": (DOMAIN_SECTION, _DOMAIN_DATA),
    "deployment": (WIZARD_DEPLOYMENT_SECTION, _DOMAIN_DATA),
}


class TestROOSComponentValidation:
    """Each wizard section's rendered HTML must pass ROOS component validation."""

    @pytest.mark.parametrize(
        "section_id",
        [s.section_id for s in ALL_SECTIONS],
        ids=[s.section_id for s in ALL_SECTIONS],
    )
    def test_section_html_valid(self, section_id: str) -> None:
        section, data = SECTION_DATA[section_id]
        renderer = _create_renderer()

        assert section.layout is not None, f"Section {section_id} has no layout"
        html = renderer.render_fields_from_editables(
            editables=section.editables,
            yaml_data=data,
            layout=section.layout,
        )

        assert html, f"Section {section_id} rendered empty HTML"
        _validate_roos_html(html, label=section_id)

    @pytest.mark.parametrize(
        "section_id",
        [s.section_id for s in ALL_SECTIONS],
        ids=[f"{s.section_id}-edit" for s in ALL_SECTIONS],
    )
    def test_section_html_valid_edit_mode(self, section_id: str) -> None:
        """Edit mode may render different widgets (readonly_on_edit)."""
        section, data = SECTION_DATA[section_id]
        renderer = _create_renderer()

        assert section.layout is not None
        html = renderer.render_fields_from_editables(
            editables=section.editables,
            yaml_data=data,
            layout=section.layout,
            edit_mode=True,
        )

        assert html
        _validate_roos_html(html, label=f"{section_id}-edit")

    def test_full_form_renders_valid_html(self) -> None:
        """The full project form with all data should produce valid ROOS HTML."""
        from opi.forms.visualizers.project_registry import get_all_project_editables, get_project_form_layout

        all_data = {
            **_IDENTITY_DATA,
            **_SERVICES_DATA,
            **_TEAM_DATA,
            **_COMPONENTS_DATA,
            **_DEPLOYMENTS_DATA,
            **_CONFIG_DATA,
        }

        renderer = _create_renderer()
        editables = get_all_project_editables()
        layout = get_project_form_layout()

        html = renderer.render_from_editables(
            editables=editables,
            yaml_data=all_data,
            layout=layout,
            edit_mode=True,
            action="/projects/edit/test-project",
        )

        assert html
        _validate_roos_html(html, label="full-form")


class TestPresetCardValidation:
    """Preset card HTML must pass ROOS component validation."""

    def test_preset_cards_unapplied(self) -> None:
        """Clickable (unapplied) preset cards produce valid ROOS HTML."""
        from opi.forms.presets.loader import Preset
        from opi.forms.widgets.roos import render_preset_cards

        presets = [
            Preset(
                id="test-preset",
                label="Test Preset",
                description="A test scenario",
                icon="laptop",
                color="hemelblauw",
                values={"some/path": "value"},
            ),
        ]
        html = render_preset_cards(presets, flow_id="test-flow", section_id="test-section")
        assert html
        _validate_roos_html(html, label="preset-cards-unapplied")

    def test_preset_cards_applied(self) -> None:
        """Applied preset cards (disabled state) produce valid ROOS HTML."""
        from opi.forms.presets.loader import Preset
        from opi.forms.widgets.roos import render_preset_cards

        presets = [
            Preset(
                id="test-applied",
                label="Applied Preset",
                description="Already applied scenario",
                icon="vinkje",
                color="groen",
                values={"config/enabled": True},
            ),
        ]
        # Provide yaml_data that matches the preset values so it's detected as applied
        yaml_data = {"config": {"enabled": True}}
        html = render_preset_cards(
            presets,
            flow_id="test-flow",
            section_id="test-section",
            yaml_data=yaml_data,
        )
        assert html
        assert "service-card--selected" in html
        assert "checked" in html
        _validate_roos_html(html, label="preset-cards-applied")

    def test_preset_cards_mixed(self) -> None:
        """A mix of applied and unapplied presets produces valid ROOS HTML."""
        from opi.forms.presets.loader import Preset
        from opi.forms.widgets.roos import render_preset_cards

        presets = [
            Preset(
                id="applied-one",
                label="Applied",
                description="This is applied",
                icon="vinkje",
                color="groen",
                values={"config/enabled": True},
            ),
            Preset(
                id="not-applied",
                label="Not Applied",
                description="This is clickable",
                icon="plus",
                color="hemelblauw",
                values={"config/other": "something"},
            ),
        ]
        yaml_data = {"config": {"enabled": True}}
        html = render_preset_cards(
            presets,
            flow_id="test-flow",
            section_id="test-section",
            yaml_data=yaml_data,
        )
        assert html
        assert "service-card--selected" in html
        assert "hx-post" in html  # second preset is still clickable
        _validate_roos_html(html, label="preset-cards-mixed")

    def test_real_keycloak_presets(self) -> None:
        """The actual keycloak-config presets file produces valid ROOS HTML."""
        from opi.forms.presets.loader import load_presets
        from opi.forms.widgets.roos import render_preset_cards

        presets = load_presets("keycloak-config")
        if not presets:
            pytest.skip("No keycloak-config presets found")

        # Test without any data applied
        html = render_preset_cards(presets, flow_id="new-project", section_id="keycloak-config")
        assert html
        _validate_roos_html(html, label="keycloak-presets-fresh")

        # Test with restrict-access already enabled (simulating authorization-wall)
        yaml_data = {
            "services": [
                {
                    "keycloak": {
                        "config": {
                            "restrict-access": {
                                "enabled": True,
                                "realm-role": "allowed-user",
                                "error-message": "${accessDeniedNoPermission}",
                            },
                        },
                    },
                },
            ],
        }
        html = render_preset_cards(
            presets,
            flow_id="new-project",
            section_id="keycloak-config",
            yaml_data=yaml_data,
        )
        assert html
        _validate_roos_html(html, label="keycloak-presets-with-restrict")
