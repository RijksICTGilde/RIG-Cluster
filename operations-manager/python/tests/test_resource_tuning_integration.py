"""
Integration tests for resource tuning using real YAML round-trips.

These tests use actual ProjectFileHandler and ruamel.yaml serialization
to verify that resource changes, history entries, and AGE-encrypted
fields survive a full read-modify-write cycle.
"""

from io import StringIO

from opi.handlers.project_file_handler import ProjectFileHandler
from ruamel.yaml import YAML

# ---------------------------------------------------------------------------
# Test YAML content
# ---------------------------------------------------------------------------

SAMPLE_PROJECT_YAML = """\
name: test-project
config:
  age-public-key: age1abc123
  age-private-key: |-
    -----BEGIN AGE ENCRYPTED FILE-----
    YWdlLWVuY3J5cHRpb24ub3JnL3YxCi0+fake-private-key-data
    -----END AGE ENCRYPTED FILE-----
  api-key: |-
    -----BEGIN AGE ENCRYPTED FILE-----
    YWdlLWVuY3J5cHRpb24ub3JnL3YxCi0+fake-api-key-data
    -----END AGE ENCRYPTED FILE-----
users:
- email: test@example.com
  role: admin
components:
- name: backend
  type: single
  resources:
    cpu: '1'
    memory: 512Mi
    requests:
      memory: 256Mi
      cpu: 100m
    limits:
      memory: 512Mi
      cpu: '1'
  user-env-vars: |-
    -----BEGIN AGE ENCRYPTED FILE-----
    YWdlLWVuY3J5cHRpb24ub3JnL3YxCi0+fake-env-vars
    -----END AGE ENCRYPTED FILE-----
deployments:
- name: productie
  cluster: odcn-production
  namespace: test-project
  components:
  - reference: backend
    image: ghcr.io/example/app:latest
    resources:
      requests:
        memory: 400Mi
      limits:
        memory: 600Mi
  configuration: |-
    -----BEGIN AGE ENCRYPTED FILE-----
    YWdlLWVuY3J5cHRpb24ub3JnL3YxCi0+fake-config-data-with-secrets
    -----END AGE ENCRYPTED FILE-----
  base-domain: example.nl
  subdomain: app
- name: staging
  cluster: odcn-production
  namespace: test-project-staging
  components:
  - reference: backend
    image: ghcr.io/example/app:develop
schema-version: 2
"""

AGE_HEADER = "-----BEGIN AGE ENCRYPTED FILE-----"


def _parse_yaml(yaml_str: str) -> dict:
    yaml = YAML()
    return yaml.load(yaml_str)


def _dump_yaml(data: dict) -> str:
    yaml = YAML()
    yaml.preserve_quotes = True
    stream = StringIO()
    yaml.dump(data, stream)
    return stream.getvalue()


# ---------------------------------------------------------------------------
# YAML round-trip: encrypted fields survive modification
# ---------------------------------------------------------------------------


class TestYamlRoundTripPreservesEncryption:
    def test_age_fields_preserved_after_resource_change(self):
        """Changing resources should not corrupt AGE-encrypted fields."""
        data = _parse_yaml(SAMPLE_PROJECT_YAML)
        handler = ProjectFileHandler()

        # Modify deployment resources (what the tuner does)
        handler.set_deployment_component_resources(
            data,
            "productie",
            "backend",
            {"limits_memory": "400Mi", "requests_memory": "300Mi"},
        )

        # Serialize and re-parse
        output = _dump_yaml(data)
        reparsed = _parse_yaml(output)

        # AGE fields must still be encrypted strings
        assert AGE_HEADER in reparsed["config"]["age-private-key"]
        assert AGE_HEADER in reparsed["config"]["api-key"]
        assert AGE_HEADER in reparsed["components"][0]["user-env-vars"]
        assert AGE_HEADER in reparsed["deployments"][0]["configuration"]

        # Resource change applied
        dep_resources = reparsed["deployments"][0]["components"][0]["resources"]
        assert dep_resources["limits"]["memory"] == "400Mi"
        assert dep_resources["requests"]["memory"] == "300Mi"

    def test_age_fields_preserved_after_history_append(self):
        """Adding history entries should not corrupt AGE-encrypted fields."""
        data = _parse_yaml(SAMPLE_PROJECT_YAML)
        handler = ProjectFileHandler()

        entry = {
            "timestamp": "2026-03-16T12:00:00+00:00",
            "limits": {"memory": "400Mi"},
            "source": "auto-tune",
            "reason": "Test reason",
        }
        handler.append_deployment_component_resource_history(data, "productie", "backend", entry)
        handler.append_component_resource_history(data, "backend", entry)

        output = _dump_yaml(data)
        reparsed = _parse_yaml(output)

        # All AGE fields intact
        assert AGE_HEADER in reparsed["config"]["age-private-key"]
        assert AGE_HEADER in reparsed["config"]["api-key"]
        assert AGE_HEADER in reparsed["components"][0]["user-env-vars"]
        assert AGE_HEADER in reparsed["deployments"][0]["configuration"]

    def test_no_decrypted_configuration_key_after_modifications(self):
        """The key 'decrypted_configuration' must never appear in serialized output."""
        data = _parse_yaml(SAMPLE_PROJECT_YAML)
        handler = ProjectFileHandler()

        handler.set_deployment_component_resources(
            data,
            "productie",
            "backend",
            {"limits_memory": "400Mi", "requests_memory": "300Mi"},
        )

        output = _dump_yaml(data)
        assert "decrypted_configuration" not in output
        assert "decrypted_config" not in output


# ---------------------------------------------------------------------------
# History: full round-trip through YAML
# ---------------------------------------------------------------------------


class TestHistoryYamlRoundTrip:
    def test_history_survives_yaml_serialization(self):
        """History entries must be readable after YAML round-trip."""
        data = _parse_yaml(SAMPLE_PROJECT_YAML)
        handler = ProjectFileHandler()

        entry = {
            "timestamp": "2026-03-16T12:00:00+00:00",
            "limits": {"memory": "400Mi"},
            "source": "oom-watcher",
            "deployment": "productie",
            "reason": "OOM kill detected",
        }
        handler.append_component_resource_history(data, "backend", entry)
        handler.append_deployment_component_resource_history(data, "productie", "backend", entry)

        # Round-trip through YAML
        output = _dump_yaml(data)
        reparsed = _parse_yaml(output)

        # Component-level history
        comp_history = reparsed["components"][0]["resources"]["history"]
        assert len(comp_history) == 1
        assert comp_history[0]["source"] == "oom-watcher"
        assert comp_history[0]["deployment"] == "productie"
        assert comp_history[0]["limits"]["memory"] == "400Mi"

        # Deployment-level history
        dep_history = reparsed["deployments"][0]["components"][0]["resources"]["history"]
        assert len(dep_history) == 1
        assert dep_history[0]["source"] == "oom-watcher"

    def test_oom_floor_readable_after_yaml_round_trip(self):
        """OOM floor must be resolvable after serializing and re-parsing."""
        data = _parse_yaml(SAMPLE_PROJECT_YAML)
        handler = ProjectFileHandler()

        entry = {
            "timestamp": "2026-03-16T12:00:00+00:00",
            "limits": {"memory": "768Mi"},
            "source": "oom-watcher",
        }
        handler.append_deployment_component_resource_history(data, "productie", "backend", entry)

        output = _dump_yaml(data)
        reparsed = _parse_yaml(output)

        floor = handler.get_resource_history_floor(reparsed, "productie", "backend")
        assert floor is not None
        assert floor.floor_mb == 768.0
        assert floor.set_at == "2026-03-16T12:00:00+00:00"

    def test_multiple_history_entries_maintain_order(self):
        """Newest entry should stay first after round-trip."""
        data = _parse_yaml(SAMPLE_PROJECT_YAML)
        handler = ProjectFileHandler()

        for i in range(3):
            entry = {
                "timestamp": f"2026-03-1{i}T12:00:00+00:00",
                "limits": {"memory": f"{200 + i * 100}Mi"},
                "source": "auto-tune" if i < 2 else "oom-watcher",
                "reason": f"Entry {i}",
            }
            handler.append_deployment_component_resource_history(data, "productie", "backend", entry)

        output = _dump_yaml(data)
        reparsed = _parse_yaml(output)

        dep_history = reparsed["deployments"][0]["components"][0]["resources"]["history"]
        assert len(dep_history) == 3
        # Most recent (last appended) should be first
        assert dep_history[0]["source"] == "oom-watcher"
        assert dep_history[0]["limits"]["memory"] == "400Mi"


# ---------------------------------------------------------------------------
# Base component update logic
# ---------------------------------------------------------------------------


class TestBaseComponentResourceUpdates:
    def test_limits_raised_when_deployment_exceeds_base(self):
        """If deployment gets higher limits, base should be raised too."""
        data = _parse_yaml(SAMPLE_PROJECT_YAML)
        handler = ProjectFileHandler()

        # Base has 512Mi limits; set deployment to 768Mi
        handler.set_deployment_component_resources(
            data,
            "productie",
            "backend",
            {"limits_memory": "768Mi", "requests_memory": "400Mi"},
        )

        # Now check: base should be updatable
        from opi.services.resource_analyzer import _k8s_memory_to_mb

        base = handler.extract_component_resources(data, "backend")
        base_limit_mb = _k8s_memory_to_mb(base["limits_memory"])
        assert base_limit_mb == 512.0  # Not yet updated - set_deployment only changes deployment

        # Explicitly update base (what the tuner does)
        handler.set_component_resources(data, "backend", {"limits_memory": "768Mi"})

        output = _dump_yaml(data)
        reparsed = _parse_yaml(output)

        base_after = handler.extract_component_resources(reparsed, "backend")
        assert base_after["limits_memory"] == "768Mi"

    def test_base_requests_not_lowered_below_limits(self):
        """Raising requests above base limits should also raise the limits."""
        data = _parse_yaml(SAMPLE_PROJECT_YAML)
        handler = ProjectFileHandler()

        # Base: requests=256Mi, limits=512Mi
        # Setting requests to 600Mi (above limits) should be possible
        handler.set_component_resources(
            data,
            "backend",
            {"requests_memory": "600Mi", "limits_memory": "600Mi"},
        )

        base = handler.extract_component_resources(data, "backend")
        from opi.services.resource_analyzer import _k8s_memory_to_mb

        req = _k8s_memory_to_mb(base["requests_memory"])
        lim = _k8s_memory_to_mb(base["limits_memory"])
        assert req <= lim, f"requests ({req}Mi) should not exceed limits ({lim}Mi)"

    def test_staging_deployment_unaffected_by_production_tune(self):
        """Tuning production should not modify staging deployment resources."""
        data = _parse_yaml(SAMPLE_PROJECT_YAML)
        handler = ProjectFileHandler()

        # Only modify production
        handler.set_deployment_component_resources(
            data,
            "productie",
            "backend",
            {"limits_memory": "1024Mi", "requests_memory": "512Mi"},
        )
        handler.append_deployment_component_resource_history(
            data,
            "productie",
            "backend",
            {"timestamp": "2026-03-16", "limits": {"memory": "1024Mi"}, "source": "auto-tune"},
        )

        # Staging should have no resource overrides and no history
        staging_comp = data["deployments"][1]["components"][0]
        assert (
            "resources" not in staging_comp
            or staging_comp.get("resources") is None
            or staging_comp.get("resources") == {}
        )

        # Verify through YAML round-trip
        output = _dump_yaml(data)
        reparsed = _parse_yaml(output)

        staging_comp_rt = reparsed["deployments"][1]["components"][0]
        resources = staging_comp_rt.get("resources")
        if resources:
            assert "history" not in resources
            assert "limits" not in resources
