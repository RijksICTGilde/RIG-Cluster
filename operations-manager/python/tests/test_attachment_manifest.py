"""Render deployment.yaml.jinja and assert attachment file-mode mounts/volumes appear."""

from opi.generation.manifests import render_template


def _render(attachment_secret_mounts: list[dict[str, str]]) -> str:
    """Rendert via de productie-omgeving, want die heeft de filters die het sjabloon
    gebruikt; een eigen Environment hier mist yaml_scalar en tolereert andere waarden."""
    return render_template(
        "deployment.yaml.jinja",
        {
            "name": "prod-api",
            "deployment_name": "prod",
            "namespace": "rig-prd-demo",
            "project": {"name": "demo"},
            "cluster": "odcn-production",
            "pod_replacement_mode": "RollingUpdate",
            "imageURL": "ghcr.io/org/api:1",
            "imagePullPolicy": "IfNotPresent",
            "application_port": 8080,
            "attachment_secret_mounts": attachment_secret_mounts,
        },
    )


def test_attachment_mounts_rendered() -> None:
    mounts = [
        {
            "name": "attch-mtlskeystore",
            "secret_name": "prod-attch-mtlskeystore",
            "mount_path": "/etc/tls/keystore.p12",
            "sub_path": "mtlskeystore",
        }
    ]
    out = _render(mounts)
    assert 'mountPath: "/etc/tls/keystore.p12"' in out
    assert 'subPath: "mtlskeystore"' in out
    assert "readOnly: true" in out
    assert 'secretName: "prod-attch-mtlskeystore"' in out
    assert 'name: "attch-mtlskeystore"' in out


def test_no_attachments_no_mounts() -> None:
    out = _render([])
    assert "attch-" not in out
