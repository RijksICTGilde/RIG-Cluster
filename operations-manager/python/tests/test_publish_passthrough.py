"""publish-on-web TLS passthrough: tls-mode read + ingress render + cert suppression."""

from __future__ import annotations

from jinja2 import Environment, FileSystemLoader
from opi.handlers.project_file_handler import ProjectFileHandler

_ENV = Environment(loader=FileSystemLoader("manifests"))


def _tls(project: dict, component: str, deployment: str | None = None) -> str:
    h = ProjectFileHandler.__new__(ProjectFileHandler)
    return h.extract_component_publish_on_web_tls(project, component, deployment)


def test_tls_resolution_cascade() -> None:
    """deployment-component override > component > root > standard."""
    project = {
        "services": [{"publish-on-web": {"config": {"tls": "passthrough"}}}],  # root default
        "components": [
            {"name": "api", "services": [{"publish-on-web": {"config": {"tls": "standard"}}}]},
            {"name": "web", "services": ["publish-on-web"]},  # no component config -> root
        ],
        "deployments": [
            {
                "name": "prod",
                "components": [
                    {
                        "reference": "api",
                        "services": {"publish-on-web": {"config": {"tls": "provided", "attachment": "wc"}}},
                    },
                    {"reference": "web"},  # no override -> inherits (root passthrough)
                ],
            }
        ],
    }
    assert _tls(project, "api", "prod") == "provided"  # deployment override wins
    assert _tls(project, "api") == "standard"  # no deployment -> component
    assert _tls(project, "web", "prod") == "passthrough"  # no override/component -> root
    assert _tls(project, "web") == "passthrough"  # -> root


def test_domain_flow_has_cert_step() -> None:
    from opi.forms.visualizers.flows import build_domain_edit_flow

    flow = build_domain_edit_flow(0)
    assert [s.section_id for s in flow.sections] == ["domain-edit-0", "domain-cert-0"]


def test_domain_cert_save_preserves_unmanaged_deployment_fields() -> None:
    """A focused sequence edit must merge, not clobber: editing only publish-on-web in
    the domain-cert step must keep image, the attachment coupling, and the system
    service-revision map on the deployment component."""
    import asyncio

    from opi.forms.editables.processor import EditableFormProcessor
    from opi.forms.visualizers.wizard_sections import build_domain_cert_section

    project = {
        "components": [{"name": "backend", "services": ["publish-on-web"]}],
        "deployments": [
            {
                "name": "staging",
                "components": [
                    {
                        "reference": "backend",
                        "image": "local/hello-zad-backend",
                        "services": {
                            "attachments": {"config": [{"reference": "sso", "provide-as": "file", "path": "/x"}]},
                            "persistent-storage": [{"reference": "data", "config": {"generation": 0}}],
                        },
                    }
                ],
            }
        ],
    }
    submitted = {
        "deployments": [{"components": [{"services": {"publish-on-web": {"config": {"tls": "passthrough"}}}}]}]
    }
    section = build_domain_cert_section(0)
    result, _ = asyncio.run(
        EditableFormProcessor().process_json_submission(submitted, section.editables, project, edit_mode=True)
    )
    comp = result["deployments"][0]["components"][0]
    assert comp["image"] == "local/hello-zad-backend"  # unmanaged top-level field kept
    assert comp["services"]["attachments"]["config"][0]["reference"] == "sso"  # sibling service kept
    assert comp["services"]["persistent-storage"][0]["config"]["generation"] == 0  # revision map kept
    assert comp["services"]["publish-on-web"]["config"]["tls"] == "passthrough"  # managed field set


def test_domain_cert_erven_prunes_empty_publish_on_web() -> None:
    """Setting the override back to 'erven' (empty) must remove the whole publish-on-web
    key, not leave noisy empty ancestors (publish-on-web: {config: {}}), while siblings
    under services and the image are preserved."""
    import asyncio

    from opi.forms.editables.processor import EditableFormProcessor
    from opi.forms.visualizers.wizard_sections import build_domain_cert_section

    project = {
        "components": [{"name": "backend", "services": ["publish-on-web"]}],
        "deployments": [
            {
                "name": "staging",
                "components": [
                    {
                        "reference": "backend",
                        "image": "x",
                        "services": {
                            "publish-on-web": {"config": {"tls": "passthrough"}},
                            "attachments": {"config": [{"reference": "sso", "provide-as": "file", "path": "/x"}]},
                        },
                    }
                ],
            }
        ],
    }
    submitted = {"deployments": [{"components": [{"services": {"publish-on-web": {"config": {"tls": ""}}}}]}]}
    section = build_domain_cert_section(0)
    result, _ = asyncio.run(
        EditableFormProcessor().process_json_submission(submitted, section.editables, project, edit_mode=True)
    )
    services = result["deployments"][0]["components"][0]["services"]
    assert "publish-on-web" not in services  # empty override pruned, no config: {} noise
    assert result["deployments"][0]["components"][0]["image"] == "x"
    assert services["attachments"]["config"][0]["reference"] == "sso"


def test_domain_cert_section_renders_per_component_without_add_remove() -> None:
    from opi.forms.visualizers.wizard_sections import build_domain_cert_section
    from opi.web.router_wizard import _create_renderer

    project = {
        "services": [{"attachments": {"data": [{"id": "wc", "filename": "wild.pem", "content": "x"}]}}],
        "components": [{"name": "api", "services": ["publish-on-web"]}],
        "deployments": [
            {
                "name": "prod",
                "components": [
                    {
                        "reference": "api",
                        "services": {"publish-on-web": {"config": {"tls": "provided", "attachment": "wc"}}},
                    }
                ],
            }
        ],
    }
    section = build_domain_cert_section(0)
    html = _create_renderer().render_fields_from_editables(
        editables=section.editables, yaml_data=project, layout=section.layout or [], edit_mode=True
    )
    assert "TLS-modus" in html
    assert "Certificaat (bijlage)" in html  # provided -> attachment picker shown
    assert "wc" in html  # catalog option
    assert "Erven" in html
    assert "sequenceAdd" not in html  # read-only component list
    assert "sequenceRemove" not in html


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
        "name": "api-ing",
        "service_name": "api",
        "hostname": "app.rijksapps.nl",
        "path": "/",
        "enable_tls": True,
        "tls_secret_name": "api-tls",
        "external_dns_target": "router.rijksapps.nl",
        "issuer_name": None,
        "cluster_issuer": None,
    }
    return _ENV.get_template("ingress.yaml.jinja").render({**base, **extra})


def test_passthrough_render_suppresses_cert() -> None:
    h = _render(passthrough=True)
    assert "route.openshift.io/termination: passthrough" in h
    assert "nginx.ingress.kubernetes.io/ssl-passthrough" in h
    assert "cert-manager.io/" not in h  # no cert requested
    assert "secretName" not in h


def test_provided_render_uses_attachment_secret_no_cert_manager() -> None:
    h = _render(provided_tls_secret="staging-api-provided-tls")
    assert 'secretName: "staging-api-provided-tls"' in h
    assert "cert-manager.io/" not in h  # customer cert, no platform issuance
    assert "route.openshift.io/termination: passthrough" not in h


def test_split_tls_pem() -> None:
    from opi.handlers.project_file_handler import _split_tls_pem

    pem = (
        "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n"
        "-----BEGIN PRIVATE KEY-----\nBBBB\n-----END PRIVATE KEY-----\n"
    )
    cert, key = _split_tls_pem(pem, "api")
    assert "BEGIN CERTIFICATE" in cert
    assert "PRIVATE" not in cert
    assert "BEGIN PRIVATE KEY" in key
    assert "CERTIFICATE" not in key

    import pytest

    with pytest.raises(ValueError, match="PRIVATE KEY"):  # no key
        _split_tls_pem("-----BEGIN CERTIFICATE-----\nx\n-----END CERTIFICATE-----\n", "api")
    with pytest.raises(ValueError, match="CERTIFICATE"):  # no cert
        _split_tls_pem("-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----\n", "api")
    with pytest.raises(ValueError, match="PRIVATE KEY"):  # two keys
        _split_tls_pem(
            "-----BEGIN CERTIFICATE-----\nx\n-----END CERTIFICATE-----\n"
            "-----BEGIN PRIVATE KEY-----\na\n-----END PRIVATE KEY-----\n"
            "-----BEGIN PRIVATE KEY-----\nb\n-----END PRIVATE KEY-----\n",
            "api",
        )


def test_resolve_publish_certificate_gating() -> None:
    import asyncio

    import pytest

    h = ProjectFileHandler.__new__(ProjectFileHandler)
    # Not provided -> None (no decryption attempted)
    passthrough = {
        "components": [{"name": "api", "services": [{"publish-on-web": {"config": {"tls": "passthrough"}}}]}]
    }
    assert asyncio.run(h.resolve_publish_on_web_certificate(passthrough, "api")) is None
    # provided without an attachment -> ValueError
    no_attach = {"components": [{"name": "api", "services": [{"publish-on-web": {"config": {"tls": "provided"}}}]}]}
    with pytest.raises(ValueError, match="attachment"):
        asyncio.run(h.resolve_publish_on_web_certificate(no_attach, "api"))


def test_standard_render_keeps_cert() -> None:
    h = _render(passthrough=False, cluster_issuer="kind-ca-issuer")
    assert "route.openshift.io/termination: passthrough" not in h
    assert "cert-manager.io/cluster-issuer: kind-ca-issuer" in h
    # Strong key material requested for platform-issued certs (no RSA-2048 default).
    assert "cert-manager.io/private-key-algorithm: ECDSA" in h
    assert 'cert-manager.io/private-key-size: "384"' in h
    assert "cert-manager.io/private-key-rotation-policy: Always" in h
    assert "secretName" in h
