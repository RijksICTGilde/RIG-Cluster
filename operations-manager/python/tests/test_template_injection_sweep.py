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

from pathlib import Path
from typing import Any

import pytest
from opi.core.config import settings
from opi.generation.manifests import render_template, yaml_scalar
from opi.handlers.project_file_handler import _normalize_path_config
from opi.utils.url_paths import sanitize_path_value
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
            sanitize_path_value(bad, "match")

    @pytest.mark.parametrize(
        "good",
        ["/", "/api", "/v1/users", "/kader", "/a-b_c.d~e", "/Some/Path"],
    )
    def test_legitimate_paths_accepted(self, good):
        assert sanitize_path_value(good, "match") == good

    def test_normalize_path_config_dict_and_string(self):
        assert _normalize_path_config("/api") == {"match": "/api", "rewrite": None}
        assert _normalize_path_config({"match": "/kader", "rewrite": "/"}) == {
            "match": "/kader",
            "rewrite": "/",
        }

    def test_normalize_path_config_rejects_injection(self):
        with pytest.raises(ValueError, match="Component path 'rewrite'"):
            _normalize_path_config({"match": "/api", "rewrite": '/"\nproxy_pass http://evil;'})


# ---------------------------------------------------------------------------
# De backup- en terugzetsjablonen (RC-339, issue #86)
# ---------------------------------------------------------------------------

#: De tien sjablonen die een backup- of restore-pod of -PVC beschrijven. Issue #86 noemde
#: ze als nog kaal; sinds RC-339 gaat elke niet-constante waarde erin door ``yaml_scalar``.
BACKUP_EN_RESTORE_SJABLONEN = [
    "backup-pod.yaml.jinja",
    "backup-database-pod.yaml.jinja",
    "backup-bucket-pod.yaml.jinja",
    "backup-bucket-mirror-pod.yaml.jinja",
    "backup-snapshot.yaml.jinja",
    "backup-clone-pvc.yaml.jinja",
    "restore-pod.yaml.jinja",
    "restore-database-pod.yaml.jinja",
    "restore-bucket-pod.yaml.jinja",
    "restore-target-pvc.yaml.jinja",
]

#: De velden die een aanroeper aan een terugzet-endpunt meegeeft, en die de sjablonen dus
#: van buiten kunnen bereiken. Elk sjabloon kent er een deel van; een sjabloon dat een veld
#: niet gebruikt, rendert er simpelweg hetzelfde van.
AANROEPERVELDEN = [
    "namespace",
    "pvc_name",
    "target_pvc_name",
    "reference_name",
    "snapshot_id",
    "size",
    "storage_class_name",
    "target_db_name",
    "target_db_host",
    "target_bucket_name",
    "target_minio_endpoint",
]

#: Alles wat de tien sjablonen samen nodig hebben. De waarden zijn plausibel, zodat de
#: gerenderde manifesten ook zonder injectiewaarde geldig zijn.
BACKUP_CTX: dict = {
    "pod_name": "wies-production-backup",
    "namespace": "rig-wies",
    "service_account_name": "wies-sa",
    "timestamp": "20260520-073000",
    "backup_run_id": "20260520073000",
    "s3_endpoint": "minio.example:9000",
    "s3_bucket": "backups",
    "s3_disable_tls": True,
    "backup_prefix": "local/rig-wies",
    "credentials_secret": "wies-production-backup-credentials",
    "repository_unusable_exit_code": 21,
    "target_unusable_exit_code": 22,
    "kopia_hostname": "wies-production-pvc-data",
    "kopia_source": "opi-backup@wies-production-pvc-data",
    "timeout_seconds": 3600,
    "retention_keep_latest": 30,
    "retention_keep_daily": 30,
    "retention_keep_weekly": 4,
    "retention_keep_monthly": 12,
    "retention_keep_annual": 0,
    "cluster": "local",
    "project_name": "wies",
    "deployment_name": "production",
    "component_name": "frontend",
    "storage_name": "data",
    "pvc_name": "production-frontend-data-pvc",
    "target_pvc_name": "production-frontend-data-pvc-v2",
    "clone_pvc_name": "production-frontend-data-pvc-clone",
    "source_pvc_name": "production-frontend-data-pvc",
    "snapshot_name": "production-frontend-data-snap",
    "snapshot_class": "csi-hostpath-snapclass",
    "snapshot_id": "k1234567890",
    "reference_name": "production-postgresql",
    "size": "10Gi",
    "storage_class_name": "standard",
    "access_modes": ["ReadWriteOnce"],
    "backup_enabled": True,
    "generation": 2,
    "pvc_generation": 2,
    "source_type": "minio",
    "db_host": "postgresql.rig-wies.svc.cluster.local",
    "db_port": 5432,
    "db_name": "wies_production",
    "db_user": "wies_production",
    "target_db_host": "postgresql.rig-other.svc.cluster.local",
    "target_db_port": 5432,
    "target_db_name": "wies_production",
    "target_db_user": "wies_production",
    "source_schema": "wies_production",
    "source_minio_endpoint": "http://minio.rig-system.svc.cluster.local:9000",
    "source_bucket_name": "wies-production",
    "backup_minio_endpoint": "http://minio.rig-system.svc.cluster.local:9000",
    "backup_bucket": "backups",
    "backup_path": "backups/wies/production/gen-2",
    "target_minio_endpoint": "http://minio.rig-other.svc.cluster.local:9000",
    "target_bucket_name": "wies-production-restored",
    "clear_target": False,
    "staging_size_limit": "5Gi",
    "trigger": "scheduled",
}


def _keys(node) -> set[str]:
    """Elke mapping-sleutel in een geparst document, op elke diepte."""
    if isinstance(node, dict):
        return set(node) | {k for v in node.values() for k in _keys(v)}
    if isinstance(node, list):
        return {k for v in node for k in _keys(v)}
    return set()


def _pod_script(doc) -> str:
    """Het shellscript van de pod, of leeg voor een sjabloon dat geen pod beschrijft."""
    containers = (doc.get("spec") or {}).get("containers") or []
    if not containers:
        return ""
    return "".join(containers[0].get("command") or []) + "".join(containers[0].get("args") or [])


class TestBackupEnRestoreSjablonen:
    """De payload komt er als scalar in en nergens in het script terecht."""

    @pytest.mark.parametrize("template", BACKUP_EN_RESTORE_SJABLONEN)
    def test_normale_waarden_renderen_geldig(self, template):
        doc = _yaml().load(render_template(template, BACKUP_CTX))

        assert doc["kind"] in {"Pod", "PersistentVolumeClaim", "VolumeSnapshot"}
        assert doc["metadata"]["namespace"] == "rig-wies"

    @pytest.mark.parametrize("template", BACKUP_EN_RESTORE_SJABLONEN)
    @pytest.mark.parametrize("field", AANROEPERVELDEN)
    def test_injectie_breekt_niet_uit_de_scalar(self, template, field):
        doc = _yaml().load(render_template(template, {**BACKUP_CTX, field: MALICIOUS}))

        assert not {"privileged", "x"} & _keys(doc)
        assert doc["kind"] in {"Pod", "PersistentVolumeClaim", "VolumeSnapshot"}

    @pytest.mark.parametrize("template", BACKUP_EN_RESTORE_SJABLONEN)
    @pytest.mark.parametrize("field", AANROEPERVELDEN)
    def test_aanroeperwaarde_staat_niet_in_het_shellscript(self, template, field):
        """Een naam die de pod print of tagt, komt via ``env`` binnen.

        YAML-quoting is geen shell-quoting: een waarde die veilig in een scalar staat kan
        in een script nog altijd een commando afsluiten.
        """
        doc = _yaml().load(render_template(template, {**BACKUP_CTX, field: MALICIOUS}))

        assert "pwned" not in _pod_script(doc)


# --------------------------------------------------------------------------- #
# De sweep over alle manifestsjablonen                                        #
# --------------------------------------------------------------------------- #
# De tests hierboven zijn per sjabloon geschreven. Daardoor kan een nieuw sjabloon
# dezelfde vorm opnieuw introduceren zonder dat iets rood wordt. De sweep sluit dat
# gat: elk bestand in manifests/ staat in SJABLOON_FIXTURES of in BUITEN_DE_SWEEP,
# en voor alles in de eerste lijst wordt gerenderd met een injectiewaarde op elke
# plek die uit een projectbestand of een service-config komt.
#
# Platformwaarden houden hun echte vorm: een clusternaam, een poort of een door OPI
# berekende enum komt niet van een afnemer, en een injectiewaarde op die plek zou een
# tak kiezen die in productie niet bestaat.

#: Probeert elke vorm van YAML-breakout tegelijk: een aanhalingsteken dat een naief
#: gequote scalar sluit, een regeleinde, een sleutelscheiding, commentaar en een
#: documentscheiding op kolom 0.
INJECTIE = 'a" #c\n---\nkind: Evil\ngeinjecteerd: "pwned"\nnaam: x'

#: Dezelfde fixtures met een gewone waarde, als vergelijkingspunt voor de vorm.
ONSCHULDIG = "onschuldig"


def _sjabloon_fixtures(waarde: str) -> dict[str, dict[str, Any]]:
    """Per sjabloon een minimale variabelenset met ``waarde`` op elke gebruikersplek."""
    return {
        "deployment.yaml.jinja": {
            "name": waarde,
            "namespace": waarde,
            "deployment_name": waarde,
            "project": {"name": waarde},
            "extra_selector_labels": {waarde: waarde},
            "service_account_name": waarde,
            "imageURL": waarde,
            "imagePullPolicy": waarde,
            "imagePullSecretsMap": {waarde: waarde},
            "env_vars": {waarde: waarde},
            "env_from_secrets": [waarde],
            "env_from_configmaps": [waarde],
            "command": [waarde],
            "host_aliases": [{"ip": waarde, "hostnames": [waarde]}],
            "storage_configs": [
                {"name": waarde, "type": "persistent", "mount-path": waarde, "pvc_name": waarde},
                {"name": waarde, "type": "ephemeral", "mount-path": waarde, "size": waarde},
            ],
            "attachment_secret_mounts": [
                {"name": waarde, "mount_path": waarde, "sub_path": waarde, "secret_name": waarde}
            ],
            "metrics_config": {"port": 9090, "path": waarde},
            "resources_requests_memory": waarde,
            "resources_requests_cpu": waarde,
            "resources_limits_memory": waarde,
            "resources_limits_cpu": waarde,
            # Platform: de probe-tak, het clustertype, de poorten en de replicacount.
            "probe_scheme": "http",
            "probe_port": 8081,
            "probe_liveness_path": waarde,
            "probe_readiness_path": waarde,
            "application_port": 8080,
            "inbound_ports": [8080],
            "replicas": 1,
            "pod_replacement_mode": "RollingUpdate",
            "cluster": "odcn-production",
            "ca_config": {"enabled": False},
            "security": None,
        },
        "service.yaml.jinja": {
            "name": waarde,
            "namespace": waarde,
            "project": {"name": waarde},
            "extra_labels": {waarde: waarde},
            "application_port": 8080,
            "service_port": 8080,
            "inbound_ports": [8080, 9090],
        },
        "ingress.yaml.jinja": {
            "name": waarde,
            "hostname": waarde,
            "service_name": waarde,
            "extra_labels": {waarde: waarde},
            "issuer_name": waarde,
            "tls_secret_name": waarde,
            "provided_tls_secret": waarde,
            "external_dns_target": waarde,
            "enable_tls": True,
            "service_port": 8080,
            # path en rewrite zijn aan de bron charset-gevalideerd (SAFE_PATH_PATTERN in
            # opi/utils/url_paths.py) omdat ze in de nginx-snippet terechtkomen, niet in
            # een YAML-scalar. TestPathSanitizationAtSource hierboven toetst die kant.
            "path": "/api",
            "rewrite": "/",
        },
        "postgresql-cluster.yaml.jinja": {
            "project_name": waarde,
            "infrastructure_namespace": waarde,
            "imagePullSecretsMap": {waarde: waarde},
            "database_config": {
                "instances": 1,
                "image": waarde,
                "storage": waarde,
                "postInitSQL": [waarde],
                "resources": {
                    "requests": {"memory": waarde, "cpu": waarde},
                    "limits": {"memory": waarde, "cpu": waarde},
                },
            },
            # Platform: de storageClass komt uit de clusterconfig.
            "storage_class": "standard",
        },
        "generic-secret.yaml.to-sops.jinja": {
            "name": waarde,
            "namespace": waarde,
            "secret_type": waarde,
            "secret_labels": {waarde: waarde},
            "secret_annotations": {waarde: waarde},
            "secret_pairs": {waarde: waarde},
        },
        "binary-secret.yaml.to-sops.jinja": {
            "name": waarde,
            "namespace": waarde,
            "secret_type": waarde,
            "data_pairs": {waarde: waarde},
        },
        "pvc.yaml.jinja": {
            "name": waarde,
            "namespace": waarde,
            "size": waarde,
            "source_pvc_name": waarde,
            "backup_enabled": True,
            # Platform: accessModes en storageClass komen uit de clusterconfig.
            "access_modes": ["ReadWriteOnce"],
            "storage_class_name": "standard",
        },
        "vpa.yaml.jinja": {
            "name": waarde,
            "namespace": waarde,
            "deployment_name": waarde,
            "project": {"name": waarde},
        },
        "namespace.yaml.jinja": {"namespace": waarde},
        "cmp-sops-key-reader.yaml.jinja": {
            "role_name": waarde,
            "namespace": waarde,
            "secret_name": waarde,
            "service_account": waarde,
            "argo_namespace": waarde,
        },
        "network-policy.yaml.jinja": {
            "name": waarde,
            "namespace": waarde,
            # Platform: deze policy wordt alleen voor de ACME-challenge gerenderd, met
            # een vaste poortenlijst, de solverpod als doel en de router als bron.
            "pod_selector": 'acme.cert-manager.io/http01-solver: "true"',
            "from_selector": {"namespace": "openshift-ingress", "pod_labels": {"app": "router"}},
            "ports": [8089],
        },
        "service-network-policy.yaml.jinja": {
            "name": waarde,
            "namespace": waarde,
            "pod_selector": {waarde: waarde},
            "ingress": [
                {"peer": {"namespace": waarde, "pod_labels": {waarde: waarde}}, "ports": [8080]},
                {"peer": None, "ports": [9090]},
            ],
            "egress": [{"peer": {"namespace": waarde, "pod_labels": {waarde: waarde}}, "ports": [5432]}],
        },
        "tenant-baseline-network-policy.yaml.jinja": {
            "name": waarde,
            "namespace": waarde,
            "deployment_selector": waarde,
            "allowed_ingress_namespaces": [waarde],
            "project_infra_namespace": waarde,
            # Platform: de namespaces van de router, de operations- en de backupkant en
            # van de database-operator komen uit de clusterconfig.
            "ingress_controller_selector": {
                "namespace": "openshift-ingress",
                "pod_labels": {"ingresscontroller": "rig"},
            },
            "ops_namespace": "rig-prd-operations",
            "backup_namespace": "rig-prd-backup",
            "database_operator_namespace": "cnpg-system",
        },
    }


#: Sjablonen die de sweep niet rendert, met per regel waarom. Een nieuw sjabloon hoort
#: in de fixtures of hier, anders wordt test_elk_sjabloon_is_belegd rood.
#:
#: Alleen _TAAK_7 wijst naar een plan dat bestaat. De andere redenen stonden in het plan
#: van RC-296 als aparte ronde zonder dat er een plan voor geschreven is, dus hier staat
#: de reden en niet een bestandsnaam die niet te vinden is.
_ARGO_PLAN = "eigen ronde voor de argo-sjablonen; die injectietest gaat hierin op"
_POD_PLAN = "gaat via KubectlConnector.template_manifest en wordt direct toegepast, eigen ronde"
_TAAK_7 = "taak 7 van plans/invoer-eerst-schoon-dan-getoetst.md"
_BUITEN_RC296 = "buiten RC-296 (plan noemt postgresql-cluster, deployment en generic-secret; de feedback de netwerk-, pvc-, vpa-, namespace- en secretsjablonen), nog kaal of naief gequoteerd"

BUITEN_DE_SWEEP: dict[str, str] = {
    "argo-repository.yaml.jinja": _ARGO_PLAN,
    "argo-repository-https.yaml.jinja": _ARGO_PLAN,
    "argocd-application.yaml.jinja": _ARGO_PLAN,
    "argocd-appproject.yaml.jinja": _ARGO_PLAN,
    "backup-bucket-mirror-pod.yaml.jinja": _POD_PLAN,
    "backup-bucket-pod.yaml.jinja": _POD_PLAN,
    "backup-clone-pvc.yaml.jinja": _POD_PLAN,
    "backup-database-pod.yaml.jinja": _POD_PLAN,
    "backup-pod.yaml.jinja": _POD_PLAN,
    "backup-snapshot.yaml.jinja": _POD_PLAN,
    "list-snapshots-pod.yaml.jinja": _POD_PLAN,
    "restore-bucket-pod.yaml.jinja": _POD_PLAN,
    "restore-database-pod.yaml.jinja": _POD_PLAN,
    "restore-pod.yaml.jinja": _POD_PLAN,
    "restore-target-pvc.yaml.jinja": _POD_PLAN,
    "db-console-emails-configmap.yaml.jinja": _TAAK_7,
    "issuer-letsencrypt.yaml.jinja": _TAAK_7,
    "configmap.yaml.jinja": f"{_BUITEN_RC296}: de sleutels van data",
    "db-console-pod.yaml.jinja": f"{_BUITEN_RC296}: name, namespace, project.name, serviceAccountName",
    "db-console-secret.yaml.jinja": f"{_BUITEN_RC296}: de sleutels van extra_labels en secret_data",
    "job-pod.yaml.jinja": f"{_BUITEN_RC296}: name, namespace, project.name, serviceAccountName",
    "project-serviceaccount.yaml.jinja": f"{_BUITEN_RC296}: name en namespace",
    "quay-proxy-organization.yaml.jinja": f"{_BUITEN_RC296}: alles behalve upstreamRegistry",
    "decrypt-sops.yaml.jinja": "statische YAML, wordt met ruamel geladen in plaats van gerenderd",
    "kustomization.yaml.jinja": "statische YAML, wordt met ruamel geladen in plaats van gerenderd",
    "pod-security-context.yaml.jinja": "include zonder gebruikerswaarden; alleen het clustertype kiest een tak",
    "sidecar-authorization-wall.yaml.jinja": "include uit deployment.yaml.jinja, volledig via yaml_scalar met test_authwall_banner_yaml_injection.py als eigen test",
}


def _documenten(gerenderd: str) -> list[Any]:
    return list(YAML(typ="safe").load_all(gerenderd))


def _vorm(knoop: Any) -> Any:
    """De structuur van een geparst document, zonder de waarden zelf.

    Een injectie verandert altijd de structuur: een extra document, een sleutel die er
    niet hoort of een scalar die een mapping wordt. De waarde zelf mag wel verschillen,
    want die is per render anders. Een sleutel die de ingevulde waarde IS wordt
    genormaliseerd, zodat de twee renders vergelijkbaar blijven.
    """
    if isinstance(knoop, dict):
        return {_sleutel(k): _vorm(v) for k, v in knoop.items()}
    if isinstance(knoop, list):
        return [_vorm(v) for v in knoop]
    return type(knoop).__name__


def _sleutel(sleutel: Any) -> Any:
    return "<waarde>" if sleutel in (INJECTIE, ONSCHULDIG) else sleutel


def _scalars(knoop: Any) -> list[Any]:
    if isinstance(knoop, dict):
        return [s for v in knoop.values() for s in _scalars(v)] + list(knoop.keys())
    if isinstance(knoop, list):
        return [s for v in knoop for s in _scalars(v)]
    return [knoop]


@pytest.mark.parametrize("sjabloon", sorted(_sjabloon_fixtures(ONSCHULDIG)))
def test_gebruikerswaarde_blijft_een_scalar(sjabloon: str) -> None:
    """Een gebruikerswaarde met regeleinden en quotes verandert de structuur niet."""
    onschuldig = _documenten(render_template(sjabloon, _sjabloon_fixtures(ONSCHULDIG)[sjabloon]))
    gerenderd = render_template(sjabloon, _sjabloon_fixtures(INJECTIE)[sjabloon])
    geinjecteerd = _documenten(gerenderd)

    assert len(geinjecteerd) == len(onschuldig), (
        f"{sjabloon} rendert {len(geinjecteerd)} documenten met een injectiewaarde en "
        f"{len(onschuldig)} met een gewone waarde:\n{gerenderd}"
    )
    assert [_vorm(d) for d in geinjecteerd] == [_vorm(d) for d in onschuldig], (
        f"{sjabloon} krijgt een andere structuur van een injectiewaarde:\n{gerenderd}"
    )
    assert INJECTIE in _scalars(geinjecteerd), (
        f"{sjabloon} geeft de waarde niet ongewijzigd terug; een filter heeft hem "
        f"verminkt in plaats van gequoteerd:\n{gerenderd}"
    )


def test_elk_sjabloon_is_belegd() -> None:
    """Elk sjabloon staat in de fixtures of op de lijst erbuiten, met reden."""
    op_schijf = {p.name for p in Path(settings.MANIFESTS_PATH).glob("*.jinja")}
    belegd = set(_sjabloon_fixtures(ONSCHULDIG)) | set(BUITEN_DE_SWEEP)

    assert not op_schijf - belegd, (
        f"Nieuw sjabloon zonder fixture: {sorted(op_schijf - belegd)}. Zet er een "
        "variabelenset voor in _sjabloon_fixtures, of een regel met reden in BUITEN_DE_SWEEP."
    )
    assert not belegd - op_schijf, f"Verdwenen sjabloon staat nog in de lijst: {sorted(belegd - op_schijf)}"
    assert not set(_sjabloon_fixtures(ONSCHULDIG)) & set(BUITEN_DE_SWEEP), "Sjabloon staat in beide lijsten"


@pytest.mark.parametrize("waarde", [None, 5, True, ["een"]])
def test_yaml_scalar_weigert_een_niet_string(waarde: Any) -> None:
    """Een niet-string is een sjabloonfout en moet dat ook zeggen.

    De emitter zou er anders ``!!null "null"`` of ``!!int "5"`` van maken en dat belandt
    ongemerkt in een manifest. Elke optionele waarde staat in een if-blok, dus een None
    of een ontbrekende variabele hier betekent dat die guard mist.
    """
    with pytest.raises(TypeError, match="yaml_scalar verwacht een str"):
        yaml_scalar(waarde)


@pytest.mark.parametrize(
    ("sjabloon", "waarden"),
    [
        # Een ontbrekende variabele geeft een Undefined ...
        ("vpa.yaml.jinja", {"name": "a", "namespace": "b", "project": {"name": "c"}}),
        # ... behalve als de naam een Jinja-global is: {{ namespace }} wordt dan de klasse
        # jinja2.utils.Namespace. Beide moeten dezelfde fout geven.
        ("namespace.yaml.jinja", {}),
    ],
)
def test_een_ontbrekende_sjabloonvariabele_noemt_het_filter(sjabloon: str, waarden: dict[str, Any]) -> None:
    with pytest.raises(TypeError, match="yaml_scalar verwacht een str"):
        render_template(sjabloon, waarden)
