"""
Tests verifying that auto-tune does not silently strip YAML fields.

The core bugs fixed:
1. _apply_flat_resources unconditionally popped legacy `cpu` and `memory` flat keys
   from the resources dict without migrating their values into the nested structure.
2. tune_deployment_resources read from the in-memory cache (which could be stale)
   and wrote the entire project file back, potentially overwriting fields that
   changed since the cache was populated.
"""

from io import StringIO

from opi.handlers.project_file_handler import ProjectFileHandler, _apply_flat_resources
from ruamel.yaml import YAML

# ---------------------------------------------------------------------------
# _apply_flat_resources: legacy key migration
# ---------------------------------------------------------------------------


class TestApplyFlatResourcesPreservesLegacyValues:
    def test_flat_cpu_migrated_to_limits(self):
        """A flat `cpu: '1'` key should be migrated to limits.cpu, not dropped."""
        target = {"resources": {"cpu": "1"}}
        _apply_flat_resources(target, {"limits_memory": "512Mi", "requests_memory": "256Mi"})

        assert target["resources"]["limits"]["cpu"] == "1"
        assert target["resources"]["limits"]["memory"] == "512Mi"
        assert "cpu" not in target["resources"]  # flat key removed after migration

    def test_flat_memory_migrated_to_both(self):
        """A flat `memory: '256Mi'` key should be migrated to both requests and limits."""
        target = {"resources": {"memory": "256Mi"}}
        _apply_flat_resources(target, {"limits_cpu": "500m"})

        assert target["resources"]["limits"]["memory"] == "256Mi"
        assert target["resources"]["requests"]["memory"] == "256Mi"
        assert target["resources"]["limits"]["cpu"] == "500m"
        assert "memory" not in target["resources"]

    def test_flat_cpu_dict_migrated(self):
        """A flat `cpu: {request: '50m', limit: '1'}` dict should be migrated."""
        target = {"resources": {"cpu": {"request": "50m", "limit": "1"}}}
        _apply_flat_resources(target, {"limits_memory": "512Mi"})

        assert target["resources"]["requests"]["cpu"] == "50m"
        assert target["resources"]["limits"]["cpu"] == "1"
        assert target["resources"]["limits"]["memory"] == "512Mi"
        assert "cpu" not in target["resources"]

    def test_existing_nested_not_overwritten_by_flat(self):
        """If nested values already exist, flat migration should not overwrite them."""
        target = {
            "resources": {
                "cpu": "2",
                "requests": {"cpu": "100m"},
                "limits": {"cpu": "500m"},
            }
        }
        _apply_flat_resources(target, {"limits_memory": "512Mi"})

        # Existing nested values preserved, flat value not used
        assert target["resources"]["requests"]["cpu"] == "100m"
        assert target["resources"]["limits"]["cpu"] == "500m"
        assert "cpu" not in target["resources"]

    def test_no_flat_keys_no_change(self):
        """When there are no legacy flat keys, nothing extra is modified."""
        target = {
            "resources": {
                "requests": {"memory": "256Mi", "cpu": "50m"},
                "limits": {"memory": "512Mi", "cpu": "1"},
            }
        }
        _apply_flat_resources(target, {"limits_memory": "400Mi"})

        assert target["resources"]["limits"]["memory"] == "400Mi"
        assert target["resources"]["requests"]["memory"] == "256Mi"
        assert target["resources"]["limits"]["cpu"] == "1"
        assert target["resources"]["requests"]["cpu"] == "50m"


# ---------------------------------------------------------------------------
# Full YAML round-trip: unrelated fields survive auto-tune modifications
# ---------------------------------------------------------------------------

YAML_WITH_EXTRA_FIELDS = """\
name: test-project
description: A project with many fields
config:
  age-public-key: age1abc
  age-private-key: |-
    -----BEGIN AGE ENCRYPTED FILE-----
    fake-private-key
    -----END AGE ENCRYPTED FILE-----
users:
- email: dev@example.com
  role: admin
components:
- name: backend
  type: single
  image: ghcr.io/example/app:latest
  resources:
    cpu: '1'
    memory: 512Mi
    requests:
      memory: 256Mi
      cpu: 100m
    limits:
      memory: 512Mi
      cpu: '1'
  services:
  - database
  - persistent-storage
  publish-on-web: false
  user-env-vars: |-
    -----BEGIN AGE ENCRYPTED FILE-----
    fake-env-vars
    -----END AGE ENCRYPTED FILE-----
  aliases:
    admin: admin.example.com
deployments:
- name: productie
  cluster: odcn-production
  namespace: test-project
  base-domain: example.nl
  subdomain: app
  root-component: backend
  configuration: |-
    -----BEGIN AGE ENCRYPTED FILE-----
    fake-config
    -----END AGE ENCRYPTED FILE-----
  components:
  - reference: backend
    image: ghcr.io/example/app:v2.1
    resources:
      requests:
        memory: 400Mi
      limits:
        memory: 600Mi
    env-vars:
      DEBUG: 'true'
schema-version: 2
"""


def _parse(yaml_str: str) -> dict:
    yaml = YAML()
    return yaml.load(yaml_str)


def _dump(data: dict) -> str:
    yaml = YAML()
    yaml.preserve_quotes = True
    stream = StringIO()
    yaml.dump(data, stream)
    return stream.getvalue()


class TestAutoTunePreservesAllFields:
    def test_unrelated_component_fields_preserved(self):
        """Fields like services, aliases, publish-on-web must survive resource changes."""
        data = _parse(YAML_WITH_EXTRA_FIELDS)
        handler = ProjectFileHandler()

        handler.set_deployment_component_resources(
            data,
            "productie",
            "backend",
            {"limits_memory": "800Mi", "requests_memory": "500Mi"},
        )
        handler.set_component_resources(
            data,
            "backend",
            {"limits_memory": "800Mi", "requests_memory": "500Mi"},
        )

        output = _dump(data)
        reparsed = _parse(output)

        comp = reparsed["components"][0]
        assert comp["services"] == ["database", "persistent-storage"]
        assert comp["aliases"] == {"admin": "admin.example.com"}
        assert comp["publish-on-web"] is False
        assert comp["image"] == "ghcr.io/example/app:latest"
        assert comp["type"] == "single"
        assert "-----BEGIN AGE ENCRYPTED FILE-----" in comp["user-env-vars"]

    def test_unrelated_deployment_fields_preserved(self):
        """Deployment-level fields like base-domain, root-component must survive."""
        data = _parse(YAML_WITH_EXTRA_FIELDS)
        handler = ProjectFileHandler()

        handler.set_deployment_component_resources(
            data,
            "productie",
            "backend",
            {"limits_memory": "800Mi", "requests_memory": "500Mi"},
        )

        output = _dump(data)
        reparsed = _parse(output)

        dep = reparsed["deployments"][0]
        assert dep["base-domain"] == "example.nl"
        assert dep["subdomain"] == "app"
        assert dep["root-component"] == "backend"
        assert "-----BEGIN AGE ENCRYPTED FILE-----" in dep["configuration"]

    def test_deployment_component_env_vars_preserved(self):
        """Deployment component env-vars must not be stripped by resource changes."""
        data = _parse(YAML_WITH_EXTRA_FIELDS)
        handler = ProjectFileHandler()

        handler.set_deployment_component_resources(
            data,
            "productie",
            "backend",
            {"limits_memory": "800Mi", "requests_memory": "500Mi"},
        )

        output = _dump(data)
        reparsed = _parse(output)

        dep_comp = reparsed["deployments"][0]["components"][0]
        assert dep_comp["env-vars"] == {"DEBUG": "true"}
        assert dep_comp["image"] == "ghcr.io/example/app:v2.1"

    def test_legacy_flat_cpu_migrated_not_lost(self):
        """Legacy flat `cpu: '1'` on components must be migrated, not silently dropped."""
        data = _parse(YAML_WITH_EXTRA_FIELDS)
        handler = ProjectFileHandler()

        # The component has cpu: '1' as a flat key
        comp_res = data["components"][0]["resources"]
        assert "cpu" in comp_res  # sanity check: flat key exists

        handler.set_component_resources(
            data,
            "backend",
            {"limits_memory": "800Mi"},
        )

        # Flat key should be gone but its value migrated
        comp_res = data["components"][0]["resources"]
        assert "cpu" not in comp_res  # flat key removed
        assert comp_res["limits"]["cpu"] == "1"  # value preserved in nested form

    def test_project_level_fields_preserved(self):
        """Top-level fields like name, description, users, config must survive."""
        data = _parse(YAML_WITH_EXTRA_FIELDS)
        handler = ProjectFileHandler()

        handler.set_deployment_component_resources(
            data,
            "productie",
            "backend",
            {"limits_memory": "800Mi", "requests_memory": "500Mi"},
        )
        handler.append_deployment_component_resource_history(
            data,
            "productie",
            "backend",
            {"timestamp": "2026-01-01", "limits": {"memory": "800Mi"}, "source": "auto-tune", "reason": "test"},
        )

        output = _dump(data)
        reparsed = _parse(output)

        assert reparsed["name"] == "test-project"
        assert reparsed["description"] == "A project with many fields"
        assert reparsed["schema-version"] == 2
        assert len(reparsed["users"]) == 1
        assert reparsed["users"][0]["email"] == "dev@example.com"
        assert reparsed["config"]["age-public-key"] == "age1abc"
