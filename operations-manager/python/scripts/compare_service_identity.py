#!/usr/bin/env python3
"""RC-19 Layer 2: does a service still resolve to the SAME thing after the upgrade?

The zad-deployments diff (``compare_deployments_diff.py``) proves nothing DISAPPEARS.
It deliberately cannot prove the thing that matters most: that a service is still the
*same* service. A deployment that after the upgrade connects cleanly to a DIFFERENT
database is a passing probe and a disaster -- the diff sees the key ``DATABASE_DB`` on
both sides and stays silent, because it identifies a line by its key and treats a
value change as "same identity" on purpose.

Two things stand in the way of catching that, and this tool solves both (plan
section 4, "De belangrijkste toets"):

1. Identity lives in the VALUE, not the key. So this compares resolved values, not
   keys.
2. Those values sit AGE-encrypted in the deployments repo, and SOPS ciphertext is
   non-deterministic -- a raw-text comparison is meaningless in both directions
   (equal content can differ, and a difference can be noise). So this DECRYPTS both
   sides first and compares the plaintext.

For every deployment it resolves the fields that define a service's identity and
asserts they are byte-for-byte equal across the baseline and the upgraded tree:

- database: host, name, user, schema
- oidc: url (issuer), realm, client-id
- object store: bucket name
- web: the published hostname(s) / domain (the Ingress host)

A difference here is not a "judge it" report like the manifest diff -- it is the
assertion this test stands or falls on. When one differs, the report names the
deployment, the field, and both values, and the tool exits non-zero.

Unlike ``compare_deployments_diff.py`` this needs the AGE key that the secrets are
encrypted with (the sandbox key on the server-sandbox run). It stays offline: SOPS +
AGE decrypt locally, no network.

Usage:
    # Take a baseline worktree of zad-deployments at the pre-upgrade commit, and
    # compare it against the post-upgrade tree. Both are plain checkouts/worktrees.
    git -C /path/to/zad-deployments worktree add /tmp/zad-baseline <baseline-sha>

    SOPS_AGE_KEY="$(sed -n '3p' security/sandbox-key.txt)" \
        uv run python scripts/compare_service_identity.py \
            /tmp/zad-baseline /path/to/zad-deployments

    # Or pass the key file explicitly (line 3 holds the AGE private key):
    uv run python scripts/compare_service_identity.py \
        /tmp/zad-baseline /path/to/zad-deployments --key-file security/sandbox-key.txt
"""

from __future__ import annotations

import argparse
import base64
import glob
import os
import subprocess
import sys
from dataclasses import dataclass

import yaml

# The identity of a service is the value of these generated secret/env keys. The key
# names are the canonical env-var names OPI writes into each deployment's secret (see
# the ``variables.py`` in each service's own package under opi/services/catalog/). We
# read the value BY NAME, so a renamed value is caught as a difference.
#
# ``web`` is special: the published host is not a secret but the Ingress host, so it is
# collected separately from the plaintext Ingress manifests.
IDENTITY_SECRET_KEYS: dict[str, str] = {
    "database.host": "DATABASE_SERVER_HOST",
    "database.name": "DATABASE_DB",
    "database.user": "DATABASE_SERVER_USER",
    "database.schema": "DATABASE_SCHEMA",
    "oidc.url": "OIDC_URL",
    "oidc.realm": "OIDC_REALM",
    "oidc.client_id": "OIDC_CLIENT_ID",
    "bucket.name": "OBJECT_STORE_BUCKET_NAME",
}

#: Logical field under which the resolved Ingress host(s) are recorded.
WEB_HOSTS_FIELD = "web.hosts"


@dataclass
class IdentityDiff:
    """One resolved-identity difference for a deployment: field, and both values."""

    group: str
    field: str
    baseline: str | None
    target: str | None

    def render(self) -> str:
        base = self.baseline if self.baseline is not None else "<absent>"
        targ = self.target if self.target is not None else "<absent>"
        return f"    - {self.field}: {base!r} -> {targ!r}"


def deployment_group(path: str) -> str:
    """Group key for a manifest path: ``cluster/project/deployment`` (first 3 segments).

    The zad-deployments layout is ``<cluster>/<project>/<deployment>/<file>``. Identity
    is a per-deployment property (each deployment gets its own database/realm/bucket), so
    the group is finer-grained than the manifest diff's ``cluster/project``. Shorter paths
    fall back to whatever is there.
    """
    parts = [p for p in path.split("/") if p]
    return "/".join(parts[:3]) if len(parts) >= 3 else "/".join(parts)


def secret_data_from_doc(doc: dict) -> dict[str, str]:
    """Return the plaintext key -> value map of a K8s Secret doc.

    Handles both ``stringData`` (already plaintext) and ``data`` (base64). A key present
    in both takes the ``stringData`` value, matching Kubernetes' own precedence. A value
    that is not valid base64 in ``data`` is skipped rather than crashing the whole run.
    """
    if not isinstance(doc, dict) or doc.get("kind") != "Secret":
        return {}

    result: dict[str, str] = {}
    data = doc.get("data") or {}
    if isinstance(data, dict):
        for key, value in data.items():
            if not isinstance(value, str):
                continue
            try:
                result[key] = base64.b64decode(value, validate=True).decode("utf-8")
            except ValueError:
                # binascii.Error and UnicodeDecodeError are both ValueError subclasses,
                # so this covers a non-base64 value and a non-utf-8 payload alike.
                continue

    string_data = doc.get("stringData") or {}
    if isinstance(string_data, dict):
        result.update({key: value for key, value in string_data.items() if isinstance(value, str)})

    return result


def ingress_hosts_from_doc(doc: dict) -> list[str]:
    """Return the host(s) an Ingress publishes on, so the web identity is comparable."""
    if not isinstance(doc, dict) or doc.get("kind") != "Ingress":
        return []
    rules = (doc.get("spec") or {}).get("rules") or []
    hosts: list[str] = []
    for rule in rules:
        if isinstance(rule, dict):
            host = rule.get("host")
            if isinstance(host, str) and host:
                hosts.append(host)
    return hosts


def extract_identities(docs_by_path: dict[str, list[dict]]) -> dict[str, dict[str, str]]:
    """Resolve each deployment's identity fields from its decrypted docs.

    Merges every Secret's data and every Ingress host under the deployment group. A field
    is only recorded when the underlying key/host is present, so a project that does not
    use a service simply has no field for it (rather than an empty string that would
    invent a difference against a side that also lacks it).
    """
    per_group_secret: dict[str, dict[str, str]] = {}
    per_group_hosts: dict[str, set[str]] = {}

    for path, docs in docs_by_path.items():
        group = deployment_group(path)
        for doc in docs:
            merged = secret_data_from_doc(doc)
            if merged:
                per_group_secret.setdefault(group, {}).update(merged)
            for host in ingress_hosts_from_doc(doc):
                per_group_hosts.setdefault(group, set()).add(host)

    identities: dict[str, dict[str, str]] = {}
    groups = set(per_group_secret) | set(per_group_hosts)
    for group in groups:
        fields: dict[str, str] = {}
        secret = per_group_secret.get(group, {})
        for field_name, secret_key in IDENTITY_SECRET_KEYS.items():
            if secret_key in secret:
                fields[field_name] = secret[secret_key]
        hosts = per_group_hosts.get(group)
        if hosts:
            # Sorted + comma-joined so the comparison is order-independent.
            fields[WEB_HOSTS_FIELD] = ",".join(sorted(hosts))
        if fields:
            identities[group] = fields
    return identities


def compare_identities(baseline: dict[str, dict[str, str]], target: dict[str, dict[str, str]]) -> list[IdentityDiff]:
    """Every resolved-identity field that differs across the two sides, most stable order.

    Reports a field that is present on one side and absent on the other too: a database
    that gained or lost its schema, an OIDC client-id that appeared or vanished, are both
    identity changes worth surfacing.
    """
    diffs: list[IdentityDiff] = []
    for group in sorted(set(baseline) | set(target)):
        base_fields = baseline.get(group, {})
        targ_fields = target.get(group, {})
        for field_name in sorted(set(base_fields) | set(targ_fields)):
            base_value = base_fields.get(field_name)
            targ_value = targ_fields.get(field_name)
            if base_value != targ_value:
                diffs.append(IdentityDiff(group=group, field=field_name, baseline=base_value, target=targ_value))
    return diffs


def format_report(diffs: list[IdentityDiff], baseline_groups: int, target_groups: int) -> str:
    """Render the identity comparison as a human-readable report."""
    if not diffs:
        return (
            f"Service identity is IDENTICAL across both sides "
            f"({baseline_groups} baseline / {target_groups} upgraded deployments compared). "
            "Every database, realm, client, bucket and published host resolves to the same value."
        )

    by_group: dict[str, list[IdentityDiff]] = {}
    for diff in diffs:
        by_group.setdefault(diff.group, []).append(diff)

    lines = [
        f"SERVICE IDENTITY CHANGED for {len(by_group)} deployment(s) -- a service now resolves to a",
        "different thing. This is a finding, not a wanted migration difference: judge each line.",
    ]
    for group in sorted(by_group):
        lines.append(f"\n## {group}")
        lines.extend(diff.render() for diff in by_group[group])
    lines.append("")
    return "\n".join(lines)


def _sops_decrypt(path: str) -> str | None:
    """Decrypt a SOPS file with the AGE key from the environment. None on failure.

    Uses the same ``SOPS_AGE_KEY`` env the rest of the codebase relies on (see
    opi/utils/sops.py). A decrypt failure returns None so one unreadable file (e.g. a key
    mismatch) does not sink the whole comparison; the caller records it.
    """
    proc = subprocess.run(  # noqa: S603 (fixed argv, no shell)
        ["sops", "--decrypt", path],  # noqa: S607 (sops resolved from PATH)
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout


def _parse_docs(text: str) -> list[dict]:
    """Parse a possibly multi-document YAML string into a list of mapping docs."""
    return [doc for doc in yaml.safe_load_all(text) if isinstance(doc, dict)]


def load_tree(root: str) -> tuple[dict[str, list[dict]], list[str]]:
    """Load every relevant manifest under ``root`` into ``relpath -> [docs]``.

    - ``*.sops.yaml`` are decrypted with the AGE key (they hold the service secrets).
    - other ``*.yaml`` are read as-is (the Ingress host is plaintext).

    Returns the docs keyed by path RELATIVE to ``root`` (so baseline and target line up
    regardless of where each tree is checked out) plus the list of files that could not be
    decrypted, which the caller reports rather than silently dropping.
    """
    docs_by_path: dict[str, list[dict]] = {}
    undecryptable: list[str] = []

    for path in sorted(glob.glob(os.path.join(root, "**", "*.yaml"), recursive=True)):
        rel = os.path.relpath(path, root)
        if path.endswith(".sops.yaml"):
            decrypted = _sops_decrypt(path)
            if decrypted is None:
                undecryptable.append(rel)
                continue
            docs_by_path[rel] = _parse_docs(decrypted)
        else:
            try:
                with open(path) as handle:
                    docs_by_path[rel] = _parse_docs(handle.read())
            except OSError, yaml.YAMLError:
                undecryptable.append(rel)

    return docs_by_path, undecryptable


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Compare resolved service identity across two zad-deployments trees (RC-19 Layer 2)"
    )
    parser.add_argument("baseline_dir", help="zad-deployments checkout/worktree at the pre-upgrade baseline")
    parser.add_argument("target_dir", help="zad-deployments tree after the upgrade + refresh")
    parser.add_argument(
        "--key-file",
        help="AGE key file (line 3 holds the private key); sets SOPS_AGE_KEY. "
        "Omit to use SOPS_AGE_KEY already in the environment.",
    )
    args = parser.parse_args(argv)

    if args.key_file:
        with open(args.key_file) as handle:
            lines = handle.read().splitlines()
        if len(lines) < 3:
            print(f"Key file {args.key_file} has fewer than 3 lines; expected the AGE key on line 3", file=sys.stderr)
            return 2
        os.environ["SOPS_AGE_KEY"] = lines[2].strip()

    baseline_docs, baseline_bad = load_tree(args.baseline_dir)
    target_docs, target_bad = load_tree(args.target_dir)

    if baseline_bad or target_bad:
        bad = sorted(set(baseline_bad) | set(target_bad))
        print(
            "Could not decrypt/parse the following file(s); the identity comparison would be "
            "incomplete, so aborting:\n" + "\n".join(f"  - {name}" for name in bad),
            file=sys.stderr,
        )
        return 2

    baseline_identities = extract_identities(baseline_docs)
    target_identities = extract_identities(target_docs)
    diffs = compare_identities(baseline_identities, target_identities)

    print(format_report(diffs, len(baseline_identities), len(target_identities)))
    return 1 if diffs else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
