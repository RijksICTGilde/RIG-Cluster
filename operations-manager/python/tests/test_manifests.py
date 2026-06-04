"""Tests for opi.generation.manifests module.

Focuses on: rendering real project templates, verifying generated file content,
file categorization logic, namespace prefix resolution, and kustomization
file assembly with correct resources/generators/namespace.
"""

import os
from unittest.mock import patch

import pytest
from opi.generation.manifests import ManifestGenerator, render_template
from ruamel.yaml import YAML

MANIFESTS_DIR = os.path.join(os.path.dirname(__file__), "..", "manifests")


@pytest.fixture
def generator():
    return ManifestGenerator()


@pytest.fixture
def yaml_loader():
    return YAML()


class TestRenderRealTemplates:
    """Render actual project templates to catch variable name mismatches and Jinja2 syntax errors."""

    def test_namespace_template(self):
        result = render_template("namespace.yaml.jinja", {"namespace": "rig-myproject"})
        yaml = YAML()
        doc = yaml.load(result)
        assert doc["kind"] == "Namespace"
        assert doc["metadata"]["name"] == "rig-myproject"
        assert doc["metadata"]["labels"]["created-by"] == "operations-manager"

    def test_service_template(self):
        result = render_template(
            "service.yaml.jinja",
            {
                "name": "web",
                "namespace": "rig-proj",
                "project": {"name": "myproj"},
                "application_port": 8080,
            },
        )
        yaml = YAML()
        doc = yaml.load(result)
        assert doc["kind"] == "Service"
        assert doc["metadata"]["name"] == "web"
        assert doc["spec"]["selector"]["app"] == "web"
        assert doc["spec"]["ports"][0]["targetPort"] == 8080
        # service_port defaults to 80 via Jinja2 default filter
        assert doc["spec"]["ports"][0]["port"] == 80

    def test_deployment_template_local_cluster(self):
        result = render_template(
            "deployment.yaml.jinja",
            {
                "name": "api",
                "namespace": "rig-proj",
                "project": {"name": "myproj"},
                "pod_replacement_mode": "RollingUpdate",
                "generated_at": "2024-01-01T00:00:00Z",
                "application_port": 3000,
                "imageURL": "registry.example.com/app:latest",
                "imagePullPolicy": "Always",
                "cluster": "local",
            },
        )
        yaml = YAML()
        doc = yaml.load(result)
        assert doc["kind"] == "Deployment"
        assert doc["metadata"]["name"] == "api"
        assert doc["spec"]["strategy"]["type"] == "RollingUpdate"
        # Local cluster should have explicit runAsUser
        pod_sec = doc["spec"]["template"]["spec"]["securityContext"]
        assert pod_sec["runAsUser"] == 1001

    def test_deployment_template_non_local_cluster(self):
        result = render_template(
            "deployment.yaml.jinja",
            {
                "name": "api",
                "namespace": "rig-proj",
                "project": {"name": "myproj"},
                "pod_replacement_mode": "Recreate",
                "generated_at": "2024-01-01T00:00:00Z",
                "application_port": 3000,
                "imageURL": "registry.example.com/app:latest",
                "imagePullPolicy": "IfNotPresent",
                "cluster": "production",
            },
        )
        yaml = YAML()
        doc = yaml.load(result)
        pod_sec = doc["spec"]["template"]["spec"]["securityContext"]
        # Non-local cluster should NOT have explicit runAsUser (OpenShift SCCs assign it)
        assert "runAsUser" not in pod_sec
        assert pod_sec["runAsNonRoot"] is True

    def test_deployment_template_sandbox_respects_security_override(self):
        """Per-component ``security`` block overrides 1001 defaults on sandbox/local."""
        result = render_template(
            "deployment.yaml.jinja",
            {
                "name": "api",
                "namespace": "rig-proj",
                "project": {"name": "myproj"},
                "pod_replacement_mode": "RollingUpdate",
                "generated_at": "2024-01-01T00:00:00Z",
                "application_port": 3000,
                "imageURL": "registry.example.com/app:latest",
                "imagePullPolicy": "Always",
                "cluster": "sandboxed-local",
                "security": {"runAsUser": 999, "runAsGroup": 999, "fsGroup": 999},
            },
        )
        yaml = YAML()
        doc = yaml.load(result)
        pod_sec = doc["spec"]["template"]["spec"]["securityContext"]
        assert pod_sec["runAsUser"] == 999
        assert pod_sec["runAsGroup"] == 999
        assert pod_sec["fsGroup"] == 999
        assert pod_sec["runAsNonRoot"] is True

    def test_deployment_template_sandbox_partial_security_override(self):
        """Only ``runAsUser`` overridden; other two fall back to 1001."""
        result = render_template(
            "deployment.yaml.jinja",
            {
                "name": "api",
                "namespace": "rig-proj",
                "project": {"name": "myproj"},
                "pod_replacement_mode": "RollingUpdate",
                "generated_at": "2024-01-01T00:00:00Z",
                "application_port": 3000,
                "imageURL": "registry.example.com/app:latest",
                "imagePullPolicy": "Always",
                "cluster": "sandboxed-local",
                "security": {"runAsUser": 999, "runAsGroup": None, "fsGroup": None},
            },
        )
        yaml = YAML()
        doc = yaml.load(result)
        pod_sec = doc["spec"]["template"]["spec"]["securityContext"]
        assert pod_sec["runAsUser"] == 999
        assert pod_sec["runAsGroup"] == 1001
        assert pod_sec["fsGroup"] == 1001

    def test_deployment_template_sandbox_without_security_keeps_1001_defaults(self):
        """Sandbox renders unchanged (1001/1001/1001) when no override is supplied."""
        result = render_template(
            "deployment.yaml.jinja",
            {
                "name": "api",
                "namespace": "rig-proj",
                "project": {"name": "myproj"},
                "pod_replacement_mode": "RollingUpdate",
                "generated_at": "2024-01-01T00:00:00Z",
                "application_port": 3000,
                "imageURL": "registry.example.com/app:latest",
                "imagePullPolicy": "Always",
                "cluster": "sandboxed-local",
            },
        )
        yaml = YAML()
        doc = yaml.load(result)
        pod_sec = doc["spec"]["template"]["spec"]["securityContext"]
        assert pod_sec["runAsUser"] == 1001
        assert pod_sec["runAsGroup"] == 1001
        assert pod_sec["fsGroup"] == 1001

    def test_deployment_template_production_ignores_security_override(self):
        """odcn-production must NOT emit runAsUser even when an override is passed.

        On OpenShift the SCC admission controller assigns the UID; emitting a
        fixed runAsUser would conflict and break production deployments.
        """
        result = render_template(
            "deployment.yaml.jinja",
            {
                "name": "api",
                "namespace": "rig-prd-proj",
                "project": {"name": "myproj"},
                "pod_replacement_mode": "RollingUpdate",
                "generated_at": "2024-01-01T00:00:00Z",
                "application_port": 3000,
                "imageURL": "registry.example.com/app:latest",
                "imagePullPolicy": "IfNotPresent",
                "cluster": "odcn-production",
                "security": {"runAsUser": 999, "runAsGroup": 999, "fsGroup": 999},
            },
        )
        yaml = YAML()
        doc = yaml.load(result)
        pod_sec = doc["spec"]["template"]["spec"]["securityContext"]
        assert "runAsUser" not in pod_sec
        assert "runAsGroup" not in pod_sec
        assert "fsGroup" not in pod_sec
        assert pod_sec["runAsNonRoot"] is True

    def test_deployment_template_with_env_vars(self):
        result = render_template(
            "deployment.yaml.jinja",
            {
                "name": "api",
                "namespace": "ns",
                "project": {"name": "p"},
                "pod_replacement_mode": "RollingUpdate",
                "generated_at": "now",
                "application_port": 8080,
                "imageURL": "img:v1",
                "imagePullPolicy": "Always",
                "cluster": "local",
                "env_vars": {"DATABASE_URL": "postgres://localhost/db", "DEBUG": "true"},
            },
        )
        assert "DATABASE_URL" in result
        assert "postgres://localhost/db" in result

    def test_ingress_template_with_tls(self):
        result = render_template(
            "ingress.yaml.jinja",
            {
                "name": "web-ingress",
                "hostname": "app.example.com",
                "enable_tls": True,
                "cluster_issuer": "letsencrypt-prod",
                "tls_secret_name": "app-tls",
            },
        )
        yaml = YAML()
        doc = yaml.load(result)
        assert doc["spec"]["rules"][0]["host"] == "app.example.com"
        assert doc["spec"]["tls"] is not None
        assert "hsts" in result.lower()

    def test_ingress_template_with_rewrite_root(self):
        """Rewrite to / should add HAProxy and NGINX rewrite annotations."""
        result = render_template(
            "ingress.yaml.jinja",
            {
                "name": "web-ingress",
                "hostname": "app.example.com",
                "path": "/kader",
                "rewrite": "/",
                "enable_tls": False,
            },
        )
        yaml = YAML()
        doc = yaml.load(result)
        annotations = doc["metadata"]["annotations"]
        # HAProxy rewrite
        assert annotations["haproxy.router.openshift.io/rewrite-target"] == "/"
        # NGINX rewrite rule in configuration-snippet
        snippet = annotations["nginx.ingress.kubernetes.io/configuration-snippet"]
        assert 'rewrite "^/kader' in snippet
        assert "break;" in snippet
        # Path should remain unchanged
        assert doc["spec"]["rules"][0]["http"]["paths"][0]["path"] == "/kader"
        assert doc["spec"]["rules"][0]["http"]["paths"][0]["pathType"] == "Prefix"

    def test_ingress_template_with_rewrite_nonroot(self):
        """Rewrite to a non-root path should use that as the rewrite base."""
        result = render_template(
            "ingress.yaml.jinja",
            {
                "name": "web-ingress",
                "hostname": "app.example.com",
                "path": "/old-prefix",
                "rewrite": "/new-prefix",
                "enable_tls": False,
            },
        )
        yaml = YAML()
        doc = yaml.load(result)
        annotations = doc["metadata"]["annotations"]
        assert annotations["haproxy.router.openshift.io/rewrite-target"] == "/new-prefix"
        snippet = annotations["nginx.ingress.kubernetes.io/configuration-snippet"]
        assert 'rewrite "^/old-prefix' in snippet
        assert "/new-prefix/$1" in snippet

    def test_ingress_template_without_rewrite(self):
        """When rewrite is not set, no rewrite annotations should appear."""
        result = render_template(
            "ingress.yaml.jinja",
            {
                "name": "web-ingress",
                "hostname": "app.example.com",
                "enable_tls": False,
            },
        )
        assert "rewrite-target" not in result
        assert "rewrite " not in result.split("configuration-snippet")[1].split("more_set_headers")[0]

    def test_ingress_template_without_tls(self):
        result = render_template(
            "ingress.yaml.jinja",
            {
                "name": "web-ingress",
                "hostname": "app.example.com",
                "enable_tls": False,
            },
        )
        assert "tls:" not in result
        # HSTS annotations should not appear without TLS
        assert "hsts_header" not in result

    def test_ingress_template_with_external_dns_target(self):
        """When external_dns_target is set, the matching annotation is rendered."""
        result = render_template(
            "ingress.yaml.jinja",
            {
                "name": "web-ingress",
                "hostname": "myproject.rijksapp.nl",
                "enable_tls": True,
                "cluster_issuer": "letsencrypt-prod",
                "tls_secret_name": "app-tls",
                "external_dns_target": "router.rijksapp.nl",
            },
        )
        yaml = YAML()
        doc = yaml.load(result)
        assert doc["metadata"]["annotations"]["external-dns.alpha.kubernetes.io/target"] == "router.rijksapp.nl"

    def test_ingress_template_without_external_dns_target(self):
        """When external_dns_target is absent, no annotation is rendered."""
        result = render_template(
            "ingress.yaml.jinja",
            {
                "name": "web-ingress",
                "hostname": "app.example.com",
                "enable_tls": False,
            },
        )
        assert "external-dns.alpha.kubernetes.io/target" not in result

    def test_network_policy_template(self):
        result = render_template(
            "network-policy.yaml.jinja",
            {
                "name": "allow-web",
                "namespace": "rig-proj",
                "ports": [8080, 443],
            },
        )
        yaml = YAML()
        doc = yaml.load(result)
        assert doc["kind"] == "NetworkPolicy"
        # podSelector should be empty dict when not provided
        assert doc["spec"]["podSelector"] == {}
        ingress_ports = doc["spec"]["ingress"][0]["ports"]
        assert len(ingress_ports) == 2
        assert ingress_ports[0]["port"] == 8080

    def test_generic_secret_template(self):
        result = render_template(
            "generic-secret.yaml.to-sops.jinja",
            {
                "name": "db-credentials",
                "namespace": "rig-proj",
                "secret_pairs": {"username": "admin", "password": "s3cret"},
            },
        )
        yaml = YAML()
        doc = yaml.load(result)
        assert doc["kind"] == "Secret"
        assert doc["stringData"]["username"] == "admin"
        assert doc["stringData"]["password"] == "s3cret"

    def test_generic_secret_empty_value(self):
        """Empty string values need special quoting to avoid YAML null."""
        result = render_template(
            "generic-secret.yaml.to-sops.jinja",
            {
                "name": "s",
                "namespace": "ns",
                "secret_pairs": {"key": ""},
            },
        )
        assert 'key: ""' in result

    def test_generic_secret_multiline_value(self):
        """Multiline values should use YAML literal block scalar."""
        cert = "-----BEGIN CERT-----\nABC123\n-----END CERT-----"
        result = render_template(
            "generic-secret.yaml.to-sops.jinja",
            {
                "name": "s",
                "namespace": "ns",
                "secret_pairs": {"cert": cert},
            },
        )
        assert "cert: |" in result

    def test_deployment_template_with_authorization_wall_sidecar(self):
        result = render_template(
            "deployment.yaml.jinja",
            {
                "name": "web",
                "namespace": "rig-proj",
                "project": {"name": "myproj"},
                "pod_replacement_mode": "RollingUpdate",
                "generated_at": "2024-01-01T00:00:00Z",
                "application_port": 8080,
                "imageURL": "registry.example.com/static-site:latest",
                "imagePullPolicy": "Always",
                "cluster": "local",
                "hostname": "app.example.com",
                "sidecars": ["authorization-wall"],
                "authorization_wall": {
                    "issuer_url": "https://keycloak.example.com/realms/test",
                    "client_id": "my-client",
                    "keycloak_secret_name": "web-keycloak",
                    "cookie_secret_name": "web-oauth2-cookie",
                },
            },
        )
        yaml = YAML()
        doc = yaml.load(result)
        containers = doc["spec"]["template"]["spec"]["containers"]
        assert len(containers) == 2
        app_container = containers[0]
        sidecar = containers[1]
        assert app_container["name"] == "app"
        assert sidecar["name"] == "authorization-wall"
        assert sidecar["image"] == "quay.io/oauth2-proxy/oauth2-proxy:v7.7.1"
        assert sidecar["ports"][0]["containerPort"] == 4180
        # Verify OIDC args
        args = sidecar["args"]
        assert "--provider=oidc" in args
        assert "--oidc-issuer-url=https://keycloak.example.com/realms/test" in args
        assert "--client-id=my-client" in args
        assert "--upstream=http://localhost:8080" in args
        # Custom sign-in page is always shown
        assert "--skip-provider-button=false" in args
        assert "--custom-templates-dir=/etc/oauth2-proxy/templates" in args
        assert not any(arg.startswith("--banner=") for arg in args)
        # Sidecar should have volumeMount for custom templates
        assert sidecar["volumeMounts"][0]["name"] == "oauth2-signin-templates"
        assert sidecar["volumeMounts"][0]["mountPath"] == "/etc/oauth2-proxy/templates"
        # Pod should have the ConfigMap volume
        volumes = doc["spec"]["template"]["spec"]["volumes"]
        oauth2_vol = [v for v in volumes if v["name"] == "oauth2-signin-templates"]
        assert len(oauth2_vol) == 1
        assert oauth2_vol[0]["configMap"]["name"] == "web-oauth2-signin"

    def test_deployment_template_with_authorization_wall_banner(self):
        result = render_template(
            "deployment.yaml.jinja",
            {
                "name": "web",
                "namespace": "rig-proj",
                "project": {"name": "myproj"},
                "pod_replacement_mode": "RollingUpdate",
                "generated_at": "2024-01-01T00:00:00Z",
                "application_port": 8080,
                "imageURL": "registry.example.com/static-site:latest",
                "imagePullPolicy": "Always",
                "cluster": "local",
                "hostname": "app.example.com",
                "sidecars": ["authorization-wall"],
                "authorization_wall": {
                    "issuer_url": "https://keycloak.example.com/realms/test",
                    "client_id": "my-client",
                    "keycloak_secret_name": "web-keycloak",
                    "cookie_secret_name": "web-oauth2-cookie",
                    "banner": "Welcome to our application. Please log in.",
                },
            },
        )
        yaml = YAML()
        doc = yaml.load(result)
        containers = doc["spec"]["template"]["spec"]["containers"]
        sidecar = containers[1]
        args = sidecar["args"]
        assert "--skip-provider-button=false" in args
        assert "--banner=Welcome to our application. Please log in." in args

    def test_authorization_wall_configmap_section(self):
        """The sidecar template's configmap section should produce a valid ConfigMap with sign_in.html."""
        result = render_template(
            "sidecar-authorization-wall.yaml.jinja",
            {
                "section": "configmap",
                "name": "web",
                "namespace": "rig-proj",
                "project": {"name": "myproj"},
            },
        )
        yaml = YAML()
        doc = yaml.load(result)
        assert doc["kind"] == "ConfigMap"
        assert doc["metadata"]["name"] == "web-oauth2-signin"
        assert doc["metadata"]["namespace"] == "rig-proj"
        assert "sign_in.html" in doc["data"]
        html = doc["data"]["sign_in.html"]
        assert "Deze website heeft beperkte toegang" in html
        assert "Inloggen" in html
        assert ".ProxyPrefix" in html

    def test_deployment_template_without_sidecars(self):
        result = render_template(
            "deployment.yaml.jinja",
            {
                "name": "api",
                "namespace": "rig-proj",
                "project": {"name": "myproj"},
                "pod_replacement_mode": "RollingUpdate",
                "generated_at": "2024-01-01T00:00:00Z",
                "application_port": 3000,
                "imageURL": "registry.example.com/app:latest",
                "imagePullPolicy": "Always",
                "cluster": "local",
            },
        )
        yaml = YAML()
        doc = yaml.load(result)
        containers = doc["spec"]["template"]["spec"]["containers"]
        assert len(containers) == 1
        assert containers[0]["name"] == "app"

    def test_service_template_with_authorization_wall_port(self):
        result = render_template(
            "service.yaml.jinja",
            {
                "name": "web",
                "namespace": "rig-proj",
                "project": {"name": "myproj"},
                "application_port": 8080,
                "service_port": 4180,
            },
        )
        yaml = YAML()
        doc = yaml.load(result)
        port = doc["spec"]["ports"][0]
        assert port["port"] == 4180
        assert port["targetPort"] == 4180


class TestCollectManifestFiles:
    """File categorization: .sops.yaml and .to-sops.yaml go to sops list, rest to regular."""

    def test_categorizes_sops_variants(self, generator, tmp_path):
        (tmp_path / "deploy.yaml").write_text("kind: Deployment")
        (tmp_path / "secret.sops.yaml").write_text("kind: Secret")
        (tmp_path / "config.to-sops.yaml").write_text("kind: Secret")
        (tmp_path / "notes.yml").write_text("kind: ConfigMap")

        sops, regular = generator.collect_manifest_files(str(tmp_path))
        sops_names = set(sops)
        regular_names = set(regular)

        assert "secret.sops.yaml" in sops_names
        assert "config.to-sops.yaml" in sops_names
        assert "deploy.yaml" in regular_names
        assert "notes.yml" in regular_names
        assert len(sops) == 2
        assert len(regular) == 2

    def test_subfolder_recursion(self, generator, tmp_path):
        sub = tmp_path / "subdir"
        sub.mkdir()
        (tmp_path / "root.yaml").write_text("a: 1")
        (sub / "nested.yaml").write_text("b: 2")

        _, shallow = generator.collect_manifest_files(str(tmp_path), include_subfolders=False)
        assert len(shallow) == 1

        _, deep = generator.collect_manifest_files(str(tmp_path), include_subfolders=True)
        assert len(deep) == 2

    def test_project_name_stripped_from_paths(self, generator, tmp_path):
        proj = tmp_path / "myproject"
        deploy = proj / "deploy-a"
        deploy.mkdir(parents=True)
        (deploy / "manifest.yaml").write_text("a: 1")

        _, regular = generator.collect_manifest_files(str(tmp_path), include_subfolders=True, project_name="myproject")
        # Path should be "deploy-a/manifest.yaml", not "myproject/deploy-a/manifest.yaml"
        assert len(regular) == 1
        assert not regular[0].startswith("myproject")
        assert regular[0] == "deploy-a/manifest.yaml"


class TestDetermineNamespaceWithPrefix:
    """Namespace prefix logic: add cluster prefix if not already present, handle unknown clusters."""

    def test_none_returns_none(self, generator):
        assert generator._determine_namespace_with_prefix(None) is None

    def test_no_deployment_returns_namespace_as_is(self, generator):
        assert generator._determine_namespace_with_prefix("my-ns") == "my-ns"

    @patch("opi.generation.manifests.get_namespace_prefix", return_value="rig-")
    def test_adds_prefix(self, mock_prefix, generator):
        result = generator._determine_namespace_with_prefix("proj", deployment={"cluster": "local"})
        assert result == "rig-proj"
        mock_prefix.assert_called_once_with("local")

    @patch("opi.generation.manifests.get_namespace_prefix", return_value="rig-")
    def test_does_not_double_prefix(self, mock_prefix, generator):
        result = generator._determine_namespace_with_prefix("rig-proj", deployment={"cluster": "local"})
        assert result == "rig-proj"
        mock_prefix.assert_called_once_with("local")

    @patch("opi.generation.manifests.get_namespace_prefix", side_effect=ValueError("unknown"))
    def test_unknown_cluster_falls_back_to_raw_namespace(self, mock_prefix, generator):
        result = generator._determine_namespace_with_prefix("proj", deployment={"cluster": "unknown"})
        assert result == "proj"
        mock_prefix.assert_called_once_with("unknown")

    def test_missing_cluster_key_does_not_crash(self, generator):
        """deployment dict without 'cluster' key should not raise KeyError."""
        result = generator._determine_namespace_with_prefix("proj", deployment={"env": "staging"})
        assert result == "proj"


class TestCreateKustomizationFiles:
    """Verify the assembled kustomization.yaml and decrypt-sops.yaml have correct content."""

    def test_kustomization_contains_resources_and_namespace(self, generator, yaml_loader, tmp_path):
        output_dir = str(tmp_path / "output")
        os.makedirs(output_dir)

        with patch("opi.generation.manifests.settings") as mock:
            mock.MANIFESTS_PATH = MANIFESTS_DIR
            generator.create_kustomization_files(
                output_dir=output_dir,
                namespace="my-ns",
                sops_files=[],
                regular_files=["deploy.yaml", "service.yaml"],
            )

        kust_path = os.path.join(output_dir, "kustomization.yaml")
        with open(kust_path) as f:
            doc = yaml_loader.load(f)

        assert doc["namespace"] == "my-ns"
        assert doc["resources"] == ["deploy.yaml", "service.yaml"]
        # No sops → no generators key
        assert "generators" not in doc or doc.get("generators") is None

    def test_sops_decrypt_files_preserve_order(self, generator, yaml_loader, tmp_path):
        """SOPS decrypt file list must preserve insertion order - nondeterministic order causes ArgoCD churn."""
        output_dir = str(tmp_path / "output")
        os.makedirs(output_dir)

        # Use enough files that set() would likely reorder them
        sops_files = [f"secret-{c}.to-sops.yaml" for c in "zyxwvutsrq"]

        with patch("opi.generation.manifests.settings") as mock:
            mock.MANIFESTS_PATH = MANIFESTS_DIR
            generator.create_kustomization_files(
                output_dir=output_dir,
                sops_files=sops_files,
                regular_files=[],
            )

        decrypt_path = os.path.join(output_dir, "decrypt-sops.yaml")
        with open(decrypt_path) as f:
            decrypt = yaml_loader.load(f)

        expected = [f"secret-{c}.sops.yaml" for c in "zyxwvutsrq"]
        assert decrypt["files"] == expected, "SOPS file order must be preserved (not randomized by set())"

    def test_sops_creates_decrypt_file_with_converted_filenames(self, generator, yaml_loader, tmp_path):
        output_dir = str(tmp_path / "output")
        os.makedirs(output_dir)

        with patch("opi.generation.manifests.settings") as mock:
            mock.MANIFESTS_PATH = MANIFESTS_DIR
            generator.create_kustomization_files(
                output_dir=output_dir,
                sops_files=["secret.to-sops.yaml", "creds.sops.yaml"],
                regular_files=["deploy.yaml"],
            )

        # kustomization should reference decrypt-sops.yaml as generator
        kust_path = os.path.join(output_dir, "kustomization.yaml")
        with open(kust_path) as f:
            kust = yaml_loader.load(f)
        assert kust["generators"] == ["decrypt-sops.yaml"]

        # decrypt-sops.yaml should convert .to-sops.yaml → .sops.yaml
        decrypt_path = os.path.join(output_dir, "decrypt-sops.yaml")
        with open(decrypt_path) as f:
            decrypt = yaml_loader.load(f)
        files = set(decrypt["files"])
        assert "secret.sops.yaml" in files  # converted from .to-sops.yaml
        assert "creds.sops.yaml" in files  # kept as-is

    def test_excludes_meta_files_from_resources(self, generator, yaml_loader, tmp_path):
        output_dir = str(tmp_path / "output")
        os.makedirs(output_dir)

        with patch("opi.generation.manifests.settings") as mock:
            mock.MANIFESTS_PATH = MANIFESTS_DIR
            generator.create_kustomization_files(
                output_dir=output_dir,
                sops_files=[],
                regular_files=["deploy.yaml", "kustomization.yaml", "decrypt-sops.yaml"],
            )

        kust_path = os.path.join(output_dir, "kustomization.yaml")
        with open(kust_path) as f:
            doc = yaml_loader.load(f)
        assert doc["resources"] == ["deploy.yaml"]

    def test_helm_charts_added_to_kustomization(self, generator, yaml_loader, tmp_path):
        output_dir = str(tmp_path / "output")
        os.makedirs(output_dir)

        charts = [{"name": "nginx", "version": "1.0.0", "repo": "https://charts.example.com"}]

        with patch("opi.generation.manifests.settings") as mock:
            mock.MANIFESTS_PATH = MANIFESTS_DIR
            generator.create_kustomization_files(
                output_dir=output_dir,
                sops_files=[],
                regular_files=[],
                helm_charts=charts,
            )

        kust_path = os.path.join(output_dir, "kustomization.yaml")
        with open(kust_path) as f:
            doc = yaml_loader.load(f)
        assert doc["helmCharts"] == charts

    @patch("opi.generation.manifests.get_namespace_prefix", return_value="rig-")
    def test_namespace_gets_cluster_prefix(self, mock_prefix, generator, yaml_loader, tmp_path):
        output_dir = str(tmp_path / "output")
        os.makedirs(output_dir)

        with patch("opi.generation.manifests.settings") as mock:
            mock.MANIFESTS_PATH = MANIFESTS_DIR
            generator.create_kustomization_files(
                output_dir=output_dir,
                namespace="proj",
                sops_files=[],
                regular_files=[],
                deployment={"cluster": "local"},
            )

        kust_path = os.path.join(output_dir, "kustomization.yaml")
        with open(kust_path) as f:
            doc = yaml_loader.load(f)
        assert doc["namespace"] == "rig-proj"


class TestCreateManifestFileEndToEnd:
    """End-to-end: template → file on disk with correct content."""

    def test_creates_file_with_rendered_content(self, generator, tmp_path):
        template = tmp_path / "template.yaml.jinja"
        template.write_text("apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: {{ name }}")
        output_dir = str(tmp_path / "output")

        path = generator.create_manifest_file(
            template_path=str(template),
            values={"name": "my-config"},
            output_dir=output_dir,
            output_filename="configmap",
        )

        yaml = YAML()
        with open(path) as f:
            doc = yaml.load(f)
        assert doc["metadata"]["name"] == "my-config"
        assert path.endswith("configmap.yaml")

    def test_sops_flag_changes_extension(self, generator, tmp_path):
        template = tmp_path / "t.yaml.jinja"
        template.write_text("data: x")

        path = generator.create_manifest_file(
            template_path=str(template),
            values={},
            output_dir=str(tmp_path / "out"),
            output_filename="secret",
            use_sops=True,
        )
        assert path.endswith("secret.to-sops.yaml")

    def test_missing_template_raises(self, generator, tmp_path):
        with pytest.raises(RuntimeError, match="Failed to create manifest"):
            generator.create_manifest_file(
                template_path="/nonexistent",
                values={},
                output_dir=str(tmp_path),
                output_filename="out",
            )


class TestCreateMultipleManifests:
    """Partial failure handling: one bad config should not block the others."""

    def test_continues_past_failures(self, generator, tmp_path):
        good = tmp_path / "good.yaml.jinja"
        good.write_text("ok: true")
        output_dir = str(tmp_path / "output")

        configs = [
            {"template_path": "/nonexistent", "values": {}, "output_filename": "bad"},
            {"template_path": str(good), "values": {}, "output_filename": "good"},
        ]

        results = generator.create_multiple_manifests(configs, output_dir)
        assert len(results) == 1
        assert results[0].endswith("good.yaml")
