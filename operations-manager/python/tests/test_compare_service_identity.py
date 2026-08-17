"""Tests for scripts/compare_service_identity.py (RC-19 Layer 2 identity comparison).

This is the plan's "belangrijkste toets": a service must still resolve to the SAME
thing after the upgrade. These tests exercise the pure resolve/compare logic with
plaintext docs (no SOPS/AGE needed), which is exactly the layer the value comparison
lives in -- the decrypt step is thin IO on top.
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path

import yaml

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from compare_service_identity import (  # noqa: E402
    WEB_HOSTS_FIELD,
    compare_identities,
    deployment_group,
    extract_identities,
    format_report,
    ingress_hosts_from_doc,
    load_tree,
    secret_data_from_doc,
)


def _b64(value: str) -> str:
    return base64.b64encode(value.encode()).decode()


def _db_secret(host: str, name: str, user: str, schema: str, *, encoded: bool = False) -> dict:
    values = {
        "DATABASE_SERVER_HOST": host,
        "DATABASE_DB": name,
        "DATABASE_SERVER_USER": user,
        "DATABASE_SCHEMA": schema,
    }
    field = "data" if encoded else "stringData"
    payload = {k: (_b64(v) if encoded else v) for k, v in values.items()}
    return {"kind": "Secret", "metadata": {"name": "d1-database"}, field: payload}


def _ingress(host: str) -> dict:
    return {"kind": "Ingress", "metadata": {"name": "d1"}, "spec": {"rules": [{"host": host}]}}


# ---------------------------------------------------------------------------
# Parsing: stringData vs base64 data, ingress hosts.
# ---------------------------------------------------------------------------


def test_secret_data_reads_stringdata_and_base64() -> None:
    string_secret = _db_secret("h", "db", "u", "s")
    b64_secret = _db_secret("h", "db", "u", "s", encoded=True)
    assert secret_data_from_doc(string_secret)["DATABASE_DB"] == "db"
    assert secret_data_from_doc(b64_secret)["DATABASE_DB"] == "db"


def test_non_secret_and_non_base64_are_ignored() -> None:
    assert secret_data_from_doc({"kind": "ConfigMap", "data": {"A": "b"}}) == {}
    # An invalid-base64 data value is skipped, not fatal.
    bad = {"kind": "Secret", "data": {"DATABASE_DB": "not base64!!!"}}
    assert "DATABASE_DB" not in secret_data_from_doc(bad)


def test_ingress_hosts_extracted() -> None:
    assert ingress_hosts_from_doc(_ingress("app.example.nl")) == ["app.example.nl"]
    assert ingress_hosts_from_doc({"kind": "Deployment"}) == []


def test_deployment_group_is_three_segments() -> None:
    assert deployment_group("odcn-production/wies/deployment-1/secret.sops.yaml") == "odcn-production/wies/deployment-1"


# ---------------------------------------------------------------------------
# Resolve + compare.
# ---------------------------------------------------------------------------


def test_identical_identity_yields_no_diff() -> None:
    docs = {
        "odcn/wies/d1/secret.sops.yaml": [_db_secret("pg", "wies_db", "wies_user", "wies")],
        "odcn/wies/d1/ingress.yaml": [_ingress("wies.rijksapps.nl")],
    }
    identities = extract_identities(docs)
    assert compare_identities(identities, identities) == []
    assert "IDENTICAL" in format_report([], 1, 1)


def test_changed_database_name_is_a_finding() -> None:
    baseline = extract_identities({"odcn/wies/d1/secret.sops.yaml": [_db_secret("pg", "wies_db", "u", "wies")]})
    target = extract_identities({"odcn/wies/d1/secret.sops.yaml": [_db_secret("pg", "OTHER_db", "u", "wies")]})

    diffs = compare_identities(baseline, target)
    assert len(diffs) == 1
    diff = diffs[0]
    assert diff.field == "database.name"
    assert diff.baseline == "wies_db"
    assert diff.target == "OTHER_db"

    report = format_report(diffs, 1, 1)
    assert "database.name" in report
    assert "wies_db" in report
    assert "OTHER_db" in report
    assert "finding" in report.lower()


def test_value_change_is_caught_where_the_manifest_diff_would_miss_it() -> None:
    """The manifest diff identifies a line by its KEY and ignores value changes; this
    tool exists precisely to catch a same-key/different-value identity change."""
    baseline = extract_identities({"odcn/p/d1/s.sops.yaml": [_db_secret("host-a", "db", "u", "s")]})
    target = extract_identities({"odcn/p/d1/s.sops.yaml": [_db_secret("host-b", "db", "u", "s")]})
    diffs = compare_identities(baseline, target)
    assert [d.field for d in diffs] == ["database.host"]


def test_appearing_or_vanishing_field_is_a_finding() -> None:
    """A schema that appears (or an OIDC client that vanishes) is an identity change too."""
    with_schema = extract_identities({"odcn/p/d1/s.sops.yaml": [_db_secret("h", "db", "u", "public")]})
    secret_no_schema = {
        "kind": "Secret",
        "stringData": {"DATABASE_SERVER_HOST": "h", "DATABASE_DB": "db", "DATABASE_SERVER_USER": "u"},
    }
    without_schema = extract_identities({"odcn/p/d1/s.sops.yaml": [secret_no_schema]})

    diffs = compare_identities(without_schema, with_schema)
    assert len(diffs) == 1
    assert diffs[0].field == "database.schema"
    assert diffs[0].baseline is None
    assert diffs[0].target == "public"


def test_ingress_host_change_is_a_finding() -> None:
    baseline = extract_identities({"odcn/p/d1/ingress.yaml": [_ingress("old.rijksapps.nl")]})
    target = extract_identities({"odcn/p/d1/ingress.yaml": [_ingress("new.rijksapps.nl")]})
    diffs = compare_identities(baseline, target)
    assert [d.field for d in diffs] == [WEB_HOSTS_FIELD]


def test_unused_service_produces_no_field_and_no_false_diff() -> None:
    """A project without a bucket has no bucket field on either side -- and must not be
    reported as a difference against another side that also lacks it."""
    docs = {"odcn/p/d1/s.sops.yaml": [_db_secret("h", "db", "u", "s")]}
    identities = extract_identities(docs)
    assert "bucket.name" not in identities["odcn/p/d1"]
    assert compare_identities(identities, identities) == []


def test_multiple_secrets_merge_into_one_deployment_identity() -> None:
    """A deployment's db secret and keycloak secret live in separate files but resolve
    into one identity record for that deployment."""
    docs = {
        "odcn/p/d1/db.sops.yaml": [_db_secret("h", "db", "u", "s")],
        "odcn/p/d1/oidc.sops.yaml": [
            {"kind": "Secret", "stringData": {"OIDC_URL": "https://kc", "OIDC_REALM": "p-odcn", "OIDC_CLIENT_ID": "p"}}
        ],
    }
    identities = extract_identities(docs)
    fields = identities["odcn/p/d1"]
    assert fields["database.name"] == "db"
    assert fields["oidc.realm"] == "p-odcn"
    assert fields["oidc.client_id"] == "p"


# ---------------------------------------------------------------------------
# IO: walking a tree of plaintext manifests (the non-SOPS branch; the SOPS
# decrypt branch needs the sops binary + key and runs on the server-sandbox).
# ---------------------------------------------------------------------------


def test_load_tree_reads_plaintext_and_keys_by_relpath(tmp_path: Path) -> None:
    dep_dir = tmp_path / "odcn-production" / "wies" / "deployment-1"
    dep_dir.mkdir(parents=True)
    (dep_dir / "ingress.yaml").write_text(yaml.safe_dump(_ingress("wies.rijksapps.nl")))

    docs_by_path, undecryptable = load_tree(str(tmp_path))

    assert undecryptable == []
    rel = "odcn-production/wies/deployment-1/ingress.yaml"
    assert rel in docs_by_path
    identities = extract_identities(docs_by_path)
    assert identities["odcn-production/wies/deployment-1"][WEB_HOSTS_FIELD] == "wies.rijksapps.nl"
