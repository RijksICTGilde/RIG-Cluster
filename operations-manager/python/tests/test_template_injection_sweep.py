"""Regression tests for systemic manifest template injection (CRITICAL/HIGH).

Same vulnerability class as the auth-wall sidecar fix, but spread across the
deployment and ingress templates: ``opi/generation/manifests.py`` renders with
``autoescape=False`` and tenant-controlled scalars (env name/value, storage
mount-path, ingress path/rewrite) used to be interpolated unquoted. A newline
plus ``"`` in a tenant value could inject sibling keys into the rendered
podSpec / nginx configuration-snippet, which ArgoCD then applies.

These tests render the real templates via ``render_template`` (no FastAPI
import chain) and assert that malicious values cannot break out, while normal
values still produce a valid manifest.
"""

import pytest
from opi.generation.manifests import render_template
from opi.handlers.project_file_handler import (
    _normalize_path_config,
    _sanitize_path_value,
)
from ruamel.yaml import YAML

# A payload that, before the fix, broke out of the quoted YAML scalar and
# injected a privileged sibling key into the container spec.
MALICIOUS = '"\n          securityContext:\n            privileged: true\n          x: "pwned'


def _yaml():
    return YAML()


class TestDeploymentTemplateInjection:
    def test_env_value_injection_is_neutralized(self):
        result = render_template(
            "deployment.yaml.jinja",
            {
                "name": "api",
                "namespace": "rig-proj",
                "project": {"name": "myproj"},
                "pod_replacement_mode": "RollingUpdate",
                "application_port": 3000,
                "imageURL": "registry.example.com/app:latest",
                "imagePullPolicy": "Always",
                "cluster": "local",
                "env_vars": {"INJECT": MALICIOUS},
            },
        )
        doc = _yaml().load(result)
        container = doc["spec"]["template"]["spec"]["containers"][0]

        # No injected sibling key anywhere in the container spec.
        assert "privileged" not in str(container.get("securityContext", {}))
        assert container["securityContext"]["allowPrivilegeEscalation"] is False

        # The malicious value survives intact as a single string value.
        env = {e["name"]: e["value"] for e in container["env"]}
        assert env["INJECT"] == MALICIOUS

    def test_storage_mount_path_injection_is_neutralized(self):
        result = render_template(
            "deployment.yaml.jinja",
            {
                "name": "api",
                "namespace": "rig-proj",
                "project": {"name": "myproj"},
                "pod_replacement_mode": "RollingUpdate",
                "application_port": 3000,
                "imageURL": "registry.example.com/app:latest",
                "imagePullPolicy": "Always",
                "cluster": "local",
                "storage_configs": [
                    {
                        "name": "data",
                        "mount-path": MALICIOUS,
                        "type": "persistent",
                        "pvc_name": "data-pvc",
                    }
                ],
            },
        )
        doc = _yaml().load(result)
        container = doc["spec"]["template"]["spec"]["containers"][0]

        # The payload is contained as a single scalar value, not parsed into
        # structure: no injected privileged/hostPath sibling keys appear.
        assert "privileged" not in container.get("securityContext", {})
        assert "privileged" not in doc["spec"]["template"]["spec"]
        mount = container["volumeMounts"][0]
        assert mount["mountPath"] == MALICIOUS
        assert mount["name"] == "data"

    def test_normal_values_still_render_valid_manifest(self):
        result = render_template(
            "deployment.yaml.jinja",
            {
                "name": "api",
                "namespace": "rig-proj",
                "project": {"name": "myproj"},
                "pod_replacement_mode": "RollingUpdate",
                "application_port": 3000,
                "imageURL": "registry.example.com/app:latest",
                "imagePullPolicy": "Always",
                "cluster": "local",
                "env_vars": {"LOG_LEVEL": "info"},
                "storage_configs": [
                    {
                        "name": "data",
                        "mount-path": "/data",
                        "type": "persistent",
                        "pvc_name": "data-pvc",
                    }
                ],
            },
        )
        doc = _yaml().load(result)
        container = doc["spec"]["template"]["spec"]["containers"][0]
        env = {e["name"]: e["value"] for e in container["env"]}
        assert env["LOG_LEVEL"] == "info"
        assert container["volumeMounts"][0]["mountPath"] == "/data"
        assert doc["kind"] == "Deployment"


class TestServiceTemplateInjection:
    """service.yaml.jinja is rendered in the same per-component loop as
    deployment.yaml.jinja with the same tenant-controlled ``name`` (=
    deployment-component, both unconstrained ``{"type": "string"}`` in the
    project schema), ``namespace`` and ``project.name``. It must be quoted
    structurally just like the deployment template."""

    def test_service_name_injection_is_neutralized(self):
        result = render_template(
            "service.yaml.jinja",
            {
                "name": MALICIOUS,
                "namespace": "rig-proj",
                "project": {"name": "myproj"},
                "service_port": 80,
                "application_port": 3000,
            },
        )
        doc = _yaml().load(result)
        # No injected sibling key anywhere; document root stays minimal and
        # the payload never escapes the scalar into the metadata mapping.
        assert set(doc.keys()) == {"apiVersion", "kind", "metadata", "spec"}
        assert set(doc["metadata"].keys()) == {"name", "namespace", "labels"}
        assert "securityContext" not in doc["spec"]
        # The payload survives intact as a single scalar value.
        assert doc["metadata"]["name"] == MALICIOUS
        assert doc["metadata"]["labels"]["app"] == MALICIOUS
        assert doc["spec"]["selector"]["app"] == MALICIOUS

    def test_service_project_name_injection_is_neutralized(self):
        result = render_template(
            "service.yaml.jinja",
            {
                "name": "api",
                "namespace": "rig-proj",
                "project": {"name": MALICIOUS},
                "service_port": 80,
                "application_port": 3000,
            },
        )
        doc = _yaml().load(result)
        assert set(doc.keys()) == {"apiVersion", "kind", "metadata", "spec"}
        assert doc["metadata"]["labels"]["project"] == MALICIOUS

    def test_normal_values_still_render_valid_service(self):
        result = render_template(
            "service.yaml.jinja",
            {
                "name": "frontend-webapp",
                "namespace": "rig-proj",
                "project": {"name": "myproj"},
                "service_port": 8080,
                "application_port": 3000,
            },
        )
        doc = _yaml().load(result)
        assert doc["kind"] == "Service"
        assert doc["metadata"]["name"] == "frontend-webapp"
        assert doc["spec"]["selector"]["app"] == "frontend-webapp"
        assert doc["spec"]["ports"][0]["port"] == 8080


class TestIngressTemplateInjection:
    def test_ingress_renders_valid_with_safe_rewrite(self):
        result = render_template(
            "ingress.yaml.jinja",
            {
                "name": "web-api",
                "hostname": "app.example.com",
                "path": "/api",
                "rewrite": "/",
                "service_name": "web",
                "service_port": 8080,
            },
        )
        doc = _yaml().load(result)
        assert doc["kind"] == "Ingress"
        assert doc["spec"]["rules"][0]["host"] == "app.example.com"
        assert doc["spec"]["rules"][0]["http"]["paths"][0]["path"] == "/api"
        snippet = doc["metadata"]["annotations"]["nginx.ingress.kubernetes.io/configuration-snippet"]
        assert 'rewrite "^/api/?(.*)$" "/$1" break;' in snippet

    def test_ingress_hostname_injection_is_neutralized(self):
        result = render_template(
            "ingress.yaml.jinja",
            {"name": "w", "hostname": MALICIOUS},
        )
        doc = _yaml().load(result)
        # No injected sibling keys at the document root.
        assert set(doc.keys()) == {"apiVersion", "kind", "metadata", "spec"}
        assert "privileged" not in doc
        assert doc["spec"]["rules"][0]["host"] == MALICIOUS


class TestPathSanitizationAtSource:
    """The real defense for the nginx snippet: reject dangerous match/rewrite."""

    @pytest.mark.parametrize(
        "bad",
        [
            '/api"\n      proxy_pass http://evil;',
            "/api\nreturn 301 http://evil;",
            '/api" break; proxy_pass http://evil; #',
            "/api;rm -rf",
            "/api with space",
            "../../etc",
            "",
            123,
        ],
    )
    def test_dangerous_values_rejected(self, bad):
        with pytest.raises(ValueError, match="Component path"):
            _sanitize_path_value(bad, "match")

    @pytest.mark.parametrize(
        "good",
        ["/", "/api", "/v1/users", "/kader", "/a-b_c.d~e", "/Some/Path"],
    )
    def test_legitimate_paths_accepted(self, good):
        assert _sanitize_path_value(good, "match") == good

    def test_normalize_path_config_dict_and_string(self):
        assert _normalize_path_config("/api") == {"match": "/api", "rewrite": None}
        assert _normalize_path_config({"match": "/kader", "rewrite": "/"}) == {
            "match": "/kader",
            "rewrite": "/",
        }

    def test_normalize_path_config_rejects_injection(self):
        with pytest.raises(ValueError, match="Component path 'rewrite'"):
            _normalize_path_config({"match": "/api", "rewrite": '/"\nproxy_pass http://evil;'})
