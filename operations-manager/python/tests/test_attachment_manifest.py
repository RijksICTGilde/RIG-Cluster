"""Render deployment.yaml.jinja and assert attachment file-mode mounts/volumes appear."""

from pathlib import Path

import jinja2

MANIFESTS = Path(__file__).resolve().parent.parent / "manifests"


def _render(attachment_secret_mounts: list[dict[str, str]]) -> str:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(MANIFESTS)),
        undefined=jinja2.ChainableUndefined,
        autoescape=False,
    )
    template = env.get_template("deployment.yaml.jinja")
    return template.render(
        name="prod-api",
        namespace="rig-prd-demo",
        application_port=8080,
        attachment_secret_mounts=attachment_secret_mounts,
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
