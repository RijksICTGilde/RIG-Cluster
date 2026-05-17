"""Regression test: the ArgoCD repository template must not embed a private key.

argo-repository.yaml.jinja used to hard-code a real OpenSSH private key as the
`sshPrivateKey` literal. Every SSH repository secret generated from it
propagated that one (now compromised, since it is in git history) key, and the
key sat in cleartext in version control.

The fix renders `sshPrivateKey` from a template variable that argo_manager
reads from settings.GIT_SERVER_KEY_PATH at runtime.

Red (vulnerable template): the template source contains a BEGIN OPENSSH
PRIVATE KEY block, so the assertion fails.
Green (fixed template): no key material in the template; the rendered secret
carries exactly the key passed in as a variable.
"""

import os

from opi.generation.manifests import render_template
from ruamel.yaml import YAML

TEMPLATE = "argo-repository.yaml.jinja"
TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "..", "manifests", TEMPLATE)


def test_template_source_has_no_embedded_private_key() -> None:
    with open(TEMPLATE_PATH) as f:
        source = f.read()
    assert "BEGIN OPENSSH PRIVATE KEY" not in source, "private key material is hard-coded in the template"
    assert "PRIVATE KEY" not in source, "private key material is hard-coded in the template"


def test_rendered_secret_uses_supplied_key_only() -> None:
    fake_key = "-----BEGIN OPENSSH PRIVATE KEY-----\nFAKEKEYMATERIAL==\n-----END OPENSSH PRIVATE KEY-----\n"
    rendered = render_template(
        TEMPLATE,
        {
            "name": "myproj-repo",
            "namespace": "rig-prd-operations",
            "type": "git",
            "repository_url": "ssh://git@example.com/x.git",
            "ssh_private_key": fake_key,
        },
    )
    doc = YAML().load(rendered)
    assert doc["kind"] == "Secret"
    assert doc["metadata"]["labels"]["argocd.argoproj.io/secret-type"] == "repository"
    # The secret carries exactly the supplied key, nothing baked in.
    assert doc["stringData"]["sshPrivateKey"] == fake_key
    assert "robbertuittenbroek" not in rendered  # the old hard-coded key's comment
