"""publish-on-web TLS passthrough: tls-mode read + ingress render + cert suppression."""

from __future__ import annotations

from jinja2 import Environment, FileSystemLoader
from opi.handlers.project_file_handler import ProjectFileHandler

_ENV = Environment(loader=FileSystemLoader("manifests"))


def _tls(project: dict, component: str) -> str:
    h = ProjectFileHandler.__new__(ProjectFileHandler)
    return h.extract_component_publish_on_web_tls(project, component)


def test_tls_mode_read() -> None:
    project = {
        "components": [
            {"name": "api", "services": [{"publish-on-web": {"config": {"tls": "passthrough"}}}]},
            {"name": "web", "services": ["publish-on-web"]},
            {"name": "std", "services": [{"publish-on-web": {"config": {"tls": "standard"}}}]},
            {"name": "none", "services": ["keycloak"]},
        ]
    }
    assert _tls(project, "api") == "passthrough"
    assert _tls(project, "web") == "standard"  # bare string -> default
    assert _tls(project, "std") == "standard"
    assert _tls(project, "none") == "standard"
    assert _tls(project, "ghost") == "standard"  # unknown component


def _render(**extra) -> str:
    base = {
        "name": "api-ing", "service_name": "api", "hostname": "app.rijksapps.nl", "path": "/",
        "enable_tls": True, "tls_secret_name": "api-tls", "external_dns_target": "router.rijksapps.nl",
        "issuer_name": None, "cluster_issuer": None,
    }
    return _ENV.get_template("ingress.yaml.jinja").render({**base, **extra})


def test_passthrough_render_suppresses_cert() -> None:
    h = _render(passthrough=True)
    assert "route.openshift.io/termination: passthrough" in h
    assert "nginx.ingress.kubernetes.io/ssl-passthrough" in h
    assert "cert-manager.io/" not in h  # no cert requested
    assert "secretName" not in h


def test_standard_render_keeps_cert() -> None:
    h = _render(passthrough=False, cluster_issuer="kind-ca-issuer")
    assert "route.openshift.io/termination: passthrough" not in h
    assert "cert-manager.io/cluster-issuer: kind-ca-issuer" in h
    assert "secretName" in h
