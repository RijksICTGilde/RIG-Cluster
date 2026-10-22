#!/usr/bin/env python
"""Extract a project's plaintext API key from its (AGE-encrypted) project file.

The API key lives in a project file as ``config.api-key``, AGE-encrypted with the project's
own key; that project key (``config.age-private-key``) is in turn wrapped with the project-KEK,
and with the cluster master key on a cluster that has not been converted yet. So decrypting the
API key needs two steps and two keys, which is exactly the chain OPI runs in
``ProjectService._resolve_plaintext_api_key``.

This is a standalone, reusable tool (there is no endpoint that returns the plaintext key, by
design). It fetches:
  - the master key from the cluster secret ``sops-age-key`` (via kubectl), unless
    ``--master-key-file`` / ``SOPS_AGE_KEY_CONTENT`` is given;
  - the project-KEK from the cluster secret ``project-kek``, unless ``--kek-file`` is given.
    No such secret is a normal answer: then the master key still opens the project key; and
  - the project file from Forgejo ``zad-projects`` (via the API), unless ``--project-file``
    is given.

Usage (from operations-manager/python):
    uv run python scripts/extract_project_api_key.py <project-name>
    uv run python scripts/extract_project_api_key.py --project-file /path/to/proj.yaml

Environment overrides (sandbox defaults shown):
    KUBE_NAMESPACE=rig-system
    SOPS_KEY_SECRET=sops-age-key
    PROJECT_KEK_SECRET=project-kek
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
from opi.utils.age import decrypt_age_content_sync, project_key_candidates_for, unwrap_project_key_sync
from opi.utils.yaml_util import load_yaml_from_string

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def _fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(1)


def _key_content_from_secret(secret: str) -> str | None:
    """The whole key file out of a cluster secret, or None when that secret is not there."""
    namespace = os.environ.get("KUBE_NAMESPACE", "rig-system")
    # Zonder kubectl op het pad is er geen cluster om te lezen, en dat is hier hetzelfde
    # antwoord als een ontbrekend secret: een run met alles uit bestanden heeft er geen nodig.
    try:
        result = subprocess.run(  # noqa: S603 - trusted, fixed kubectl invocation
            ["kubectl", "-n", namespace, "get", "secret", secret, "-o", "jsonpath={.data.key}"],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return None
    if result.returncode != 0 or not result.stdout:
        return None
    return base64.b64decode(result.stdout).decode()


def get_master_private_key(args: argparse.Namespace) -> str:
    """Master AGE private key: from a file/env, else the cluster ``sops-age-key`` secret."""
    content = None
    if args.master_key_file:
        with open(args.master_key_file) as f:
            content = f.read()
    elif os.environ.get("SOPS_AGE_KEY_CONTENT"):
        content = os.environ["SOPS_AGE_KEY_CONTENT"]
    else:
        secret = os.environ.get("SOPS_KEY_SECRET", "sops-age-key")
        content = _key_content_from_secret(secret)
        if content is None:
            _fail(f"could not read secret {secret} in {os.environ.get('KUBE_NAMESPACE', 'rig-system')}")

    _, private_key = parse_sops_age_key_content(content)
    if not private_key:
        _fail("could not parse an AGE-SECRET-KEY from the master key content")
    return private_key


def get_project_kek_private_key(args: argparse.Namespace) -> str | None:
    """The project-KEK, if there is one: a project key is wrapped with it rather than the master key.

    Absent is a normal answer: a cluster that has not been converted yet has no such secret,
    and then the master key still opens the project key.
    """
    if args.kek_file:
        with open(args.kek_file) as f:
            content = f.read()
    else:
        content = _key_content_from_secret(os.environ.get("PROJECT_KEK_SECRET", "project-kek"))
        if content is None:
            return None
    _, private_key = parse_sops_age_key_content(content)
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


def extract_api_key(config: dict, master_private_key: str, kek_private_key: str | None = None) -> str:
    """Decrypt ``config.api-key`` following OPI's two-step chain."""
    api_key = config.get("api-key")
    if not api_key:
        _fail("project config has no api-key")
    if not str(api_key).startswith("-----BEGIN AGE"):
        return str(api_key)  # already plaintext (legacy/test data)

    encoded_private_key = config.get("age-private-key")
    if not encoded_private_key:
        _fail("project config has no age-private-key to decrypt the api-key with")

    try:
        project_private_key = unwrap_project_key_sync(
            str(encoded_private_key), project_key_candidates_for(master_private_key, kek_private_key)
        )
    except ValueError as e:
        _fail(f"could not unwrap the project age-private-key: {e}")

    plaintext = decrypt_age_content_sync(str(api_key), project_private_key)
    if not plaintext:
        _fail("could not decrypt the api-key with the project key")
    return plaintext


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract a project's plaintext API key.")
    parser.add_argument("project", nargs="?", help="Project name (fetched from Forgejo zad-projects)")
    parser.add_argument("--project-file", help="Local project YAML instead of fetching from Forgejo")
    parser.add_argument("--master-key-file", help="Master AGE key file instead of the cluster secret")
    parser.add_argument("--kek-file", help="Project-KEK file instead of the cluster 'project-kek' secret")
    args = parser.parse_args()

    master_private_key = get_master_private_key(args)
    kek_private_key = get_project_kek_private_key(args)
    config = get_project_config(args)
    print(extract_api_key(config, master_private_key, kek_private_key))


if __name__ == "__main__":
    main()
