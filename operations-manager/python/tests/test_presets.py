"""Tests for preset loading and application."""

from __future__ import annotations

from opi.forms.editables.service_path import smart_get_value, smart_set_value
from opi.forms.presets.loader import get_preset_by_id, load_presets


class TestLoadPresets:
    def test_loads_keycloak_presets(self):
        presets = load_presets("keycloak-config")
        assert len(presets) >= 2

    def test_preset_has_required_fields(self):
        presets = load_presets("keycloak-config")
        for preset in presets:
            assert preset.id
            assert preset.label
            assert preset.description
            assert isinstance(preset.values, dict)

    def test_lokale_ontwikkeling_preset_values(self):
        presets = load_presets("keycloak-config")
        lokaal = next((p for p in presets if p.id == "lokale-ontwikkeling"), None)
        assert lokaal is not None
        assert "services/keycloak/config/additional-clients" in lokaal.values
        clients = lokaal.values["services/keycloak/config/additional-clients"]
        assert isinstance(clients, list)
        assert len(clients) == 1
        assert clients[0]["name"] == "local-development"
        assert len(clients[0]["redirect-uris"]) == 2

    def test_toegangsbeperking_preset_values(self):
        presets = load_presets("keycloak-config")
        toegang = next((p for p in presets if p.id == "toegangsbeperking"), None)
        assert toegang is not None
        assert toegang.values["services/keycloak/config/restrict-access/enabled"] is True
        assert toegang.values["services/keycloak/config/restrict-access/realm-role"] == "allowed-user"

    def test_nonexistent_section_returns_empty(self):
        presets = load_presets("nonexistent-section")
        assert presets == []


class TestGetPresetById:
    def test_finds_existing_preset(self):
        preset = get_preset_by_id("keycloak-config", "lokale-ontwikkeling")
        assert preset is not None
        assert preset.id == "lokale-ontwikkeling"

    def test_returns_none_for_unknown(self):
        preset = get_preset_by_id("keycloak-config", "unknown-preset")
        assert preset is None


class TestPresetApplication:
    def test_preset_values_applied_correctly(self):
        data: dict = {"services": ["keycloak"]}
        presets = load_presets("keycloak-config")
        lokaal = next(p for p in presets if p.id == "lokale-ontwikkeling")

        for path, value in lokaal.values.items():
            smart_set_value(data, path, value)

        clients = smart_get_value(data, "services/keycloak/config/additional-clients")
        assert isinstance(clients, list)
        assert len(clients) == 1
        assert clients[0]["name"] == "local-development"

    def test_preset_is_additive(self):
        """Applying a preset preserves existing unrelated values."""
        data: dict = {
            "services": [
                {
                    "keycloak": {
                        "config": {
                            "template": "sso-only",
                        },
                    },
                },
            ],
        }

        presets = load_presets("keycloak-config")
        lokaal = next(p for p in presets if p.id == "lokale-ontwikkeling")
        for path, value in lokaal.values.items():
            smart_set_value(data, path, value)

        # Original template value preserved
        assert smart_get_value(data, "services/keycloak/config/template") == "sso-only"
        # New values applied
        assert smart_get_value(data, "services/keycloak/config/additional-clients") is not None

    def test_preset_preserves_other_services(self):
        """Applying a keycloak preset doesn't affect other services."""
        data: dict = {
            "services": [
                "publish-on-web",
                {"keycloak": {"config": {"template": "sso-only"}}},
            ],
        }

        presets = load_presets("keycloak-config")
        toegang = next(p for p in presets if p.id == "toegangsbeperking")
        for path, value in toegang.values.items():
            smart_set_value(data, path, value)

        # Other services still present
        assert data["services"][0] == "publish-on-web"
        # Keycloak access restriction applied
        assert smart_get_value(data, "services/keycloak/config/restrict-access/enabled") is True
