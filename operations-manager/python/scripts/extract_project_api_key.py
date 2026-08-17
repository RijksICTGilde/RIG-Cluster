#!/usr/bin/env python
"""Extract a project's plaintext API key from its (AGE-encrypted) project file.

The API key lives in a project file as ``config.api-key``, AGE-encrypted with the project's
own key; that project key (``config.age-private-key``) is in turn AGE-encrypted with the
cluster master key. So decrypting the API key needs two steps and two keys - the cluster
master key and the per-project key - which is exactly the chain OPI runs in
``ProjectService._resolve_plaintext_api_key``.

This is a standalone, reusable tool (there is no endpoint that returns the plaintext key, by
design). It fetches:
  - the master key from the cluster secret ``sops-age-key`` (via kubectl), unless
    ``--master-key-file`` / ``SOPS_AGE_KEY_CONTENT`` is given; and
  - the project file from Forgejo ``zad-projects`` (via the API), unless ``--project-file``
    is given.

Usage (from operations-manager/python):
    uv run python scripts/extract_project_api_key.py <project-name>
    uv run python scripts/extract_project_api_key.py --project-file /path/to/proj.yaml

Environment overrides (sandbox defaults shown):
    KUBE_NAMESPACE=rig-system
    SOPS_KEY_SECRET=sops-age-key
    FORGEJO_URL=https://forgejo.sandbox.rijksapp.dev
    FORGEJO_USER=rig-admin  FORGEJO_PASSWORD=admin1234
    FORGEJO_PROJECTS_REPO=rig-admin/zad-projects  FORGEJO_BRANCH=main
"""

from __future__ import annotations

import argparse
import base64
import os
import subprocess
import sys

import requests
import urllib3
from opi.core.config import parse_sops_age_key_content
from opi.utils.age import decrypt_age_content_sync
from opi.utils.yaml_util import load_yaml_from_string

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def _fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(1)


def get_master_private_key(args: argparse.Namespace) -> str:
    """Master AGE private key: from a file/env, else the cluster ``sops-age-key`` secret."""
    content = None
    if args.master_key_file:
        with open(args.master_key_file) as f:
            content = f.read()
    elif os.environ.get("SOPS_AGE_KEY_CONTENT"):
        content = os.environ["SOPS_AGE_KEY_CONTENT"]
    else:
        namespace = os.environ.get("KUBE_NAMESPACE", "rig-system")
        secret = os.environ.get("SOPS_KEY_SECRET", "sops-age-key")
        result = subprocess.run(  # noqa: S603 - trusted, fixed kubectl invocation
            ["kubectl", "-n", namespace, "get", "secret", secret, "-o", "jsonpath={.data.key}"],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0 or not result.stdout:
            _fail(f"could not read secret {secret} in {namespace}: {result.stderr.strip()}")
        content = base64.b64decode(result.stdout).decode()

    _, private_key = parse_sops_age_key_content(content)
    if not private_key:
        _fail("could not parse an AGE-SECRET-KEY from the master key content")
    return private_key


def get_project_config(args: argparse.Namespace) -> dict:
    """Project ``config`` block: from a local file, else fetched from Forgejo zad-projects."""
    if args.project_file:
        with open(args.project_file) as f:
            raw = f.read()
    else:
        if not args.project:
            _fail("provide a project name or --project-file")
        base = os.environ.get("FORGEJO_URL", "https://forgejo.sandbox.rijksapp.dev").rstrip("/")
        repo = os.environ.get("FORGEJO_PROJECTS_REPO", "rig-admin/zad-projects")
        branch = os.environ.get("FORGEJO_BRANCH", "main")
        user = os.environ.get("FORGEJO_USER", "rig-admin")
        password = os.environ.get("FORGEJO_PASSWORD", "admin1234")
        url = f"{base}/api/v1/repos/{repo}/raw/projects/{args.project}.yaml?ref={branch}"
        response = requests.get(url, auth=(user, password), verify=False, timeout=30)  # noqa: S501 - sandbox self-signed cert
        if response.status_code != 200:
            _fail(f"could not fetch project file ({response.status_code}): {url}")
        raw = response.text

    data = load_yaml_from_string(raw)
    if not data or "config" not in data:
        _fail("project file has no config block")
    return data["config"]


def extract_api_key(config: dict, master_private_key: str) -> str:
    """Decrypt ``config.api-key`` following OPI's two-step chain."""
    api_key = config.get("api-key")
    if not api_key:
        _fail("project config has no api-key")
    if not str(api_key).startswith("-----BEGIN AGE"):
        return str(api_key)  # already plaintext (legacy/test data)

    encoded_private_key = config.get("age-private-key")
    if not encoded_private_key:
        _fail("project config has no age-private-key to decrypt the api-key with")

    project_private_key = decrypt_age_content_sync(str(encoded_private_key), master_private_key)
    if not project_private_key:
        _fail("could not decrypt the project age-private-key with the master key")

    plaintext = decrypt_age_content_sync(str(api_key), project_private_key)
    if not plaintext:
        _fail("could not decrypt the api-key with the project key")
    return plaintext


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract a project's plaintext API key.")
    parser.add_argument("project", nargs="?", help="Project name (fetched from Forgejo zad-projects)")
    parser.add_argument("--project-file", help="Local project YAML instead of fetching from Forgejo")
    parser.add_argument("--master-key-file", help="Master AGE key file instead of the cluster secret")
    args = parser.parse_args()

    master_private_key = get_master_private_key(args)
    config = get_project_config(args)
    print(extract_api_key(config, master_private_key))


if __name__ == "__main__":
    main()
