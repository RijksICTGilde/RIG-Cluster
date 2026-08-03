"""Tests for scripts/compare_deployments_diff.py (RC-19 Layer 2 diff summarizer).

The summarizer must turn a raw zad-deployments ``git diff`` into a per-project list
of what disappeared, filtering pure value changes so only genuine removals surface.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from compare_deployments_diff import format_report, summarize_diff  # noqa: E402

# A diff touching two projects: moza loses a secret data key, an env var and a whole
# Ingress resource; regelrecht only has a changed value (must NOT be reported).
SAMPLE_DIFF = """\
diff --git a/sandboxed-local/moza/deployment-1/secret.yaml b/sandboxed-local/moza/deployment-1/secret.yaml
index 111..222 100644
--- a/sandboxed-local/moza/deployment-1/secret.yaml
+++ b/sandboxed-local/moza/deployment-1/secret.yaml
@@ -3,7 +3,6 @@ data:
   DATABASE_PASSWORD: c2VjcmV0
-  REDIS_PASSWORD: b2xk
   OBJECT_STORE_BUCKET_NAME: bW96YQ==
diff --git a/sandboxed-local/moza/deployment-1/deployment.yaml b/sandboxed-local/moza/deployment-1/deployment.yaml
index 333..444 100644
--- a/sandboxed-local/moza/deployment-1/deployment.yaml
+++ b/sandboxed-local/moza/deployment-1/deployment.yaml
@@ -10,8 +10,6 @@ spec:
         env:
-        - name: DATABASE_SCHEMA_REPORTING
-          value: reporting
           volumeMounts:
-          - mountPath: /data
diff --git a/sandboxed-local/moza/deployment-1/ingress.yaml b/sandboxed-local/moza/deployment-1/ingress.yaml
index 555..666 100644
--- a/sandboxed-local/moza/deployment-1/ingress.yaml
+++ b/sandboxed-local/moza/deployment-1/ingress.yaml
@@ -1,5 +1,0 @@
-kind: Ingress
-metadata:
-  name: moza-admin
-spec:
-  host: admin.moza.example
diff --git a/sandboxed-local/regelrecht/deployment-1/config.yaml b/sandboxed-local/regelrecht/deployment-1/config.yaml
index 777..888 100644
--- a/sandboxed-local/regelrecht/deployment-1/config.yaml
+++ b/sandboxed-local/regelrecht/deployment-1/config.yaml
@@ -1,2 +1,2 @@
-  LOG_LEVEL: debug
+  LOG_LEVEL: info
"""


def test_groups_by_cluster_and_project() -> None:
    summary = summarize_diff(SAMPLE_DIFF)
    # Only moza has genuine removals; regelrecht's value-only change is not a
    # disappearance, so it does not appear in the summary at all.
    assert set(summary) == {"sandboxed-local/moza"}


def test_pure_value_change_is_not_a_removal() -> None:
    """regelrecht only changes LOG_LEVEL's value; the key stays, so nothing disappeared."""
    summary = summarize_diff(SAMPLE_DIFF)
    assert "sandboxed-local/regelrecht" not in summary


def test_removed_secret_key_and_env_var_categorized() -> None:
    moza = summarize_diff(SAMPLE_DIFF)["sandboxed-local/moza"]

    data_keys = moza.removed_by_category.get("data-key", [])
    assert any("REDIS_PASSWORD" in line for line in data_keys)

    # DATABASE_SCHEMA_REPORTING matches the schema category (schema wins over env-var).
    schema = moza.removed_by_category.get("schema", [])
    assert any("DATABASE_SCHEMA_REPORTING" in line for line in schema)


def test_removed_resource_and_mount_and_host() -> None:
    moza = summarize_diff(SAMPLE_DIFF)["sandboxed-local/moza"]

    assert any("kind: Ingress" in line for line in moza.removed_by_category.get("resource", []))
    assert any("mountPath" in line for line in moza.removed_by_category.get("mount", []))
    assert any("host:" in line for line in moza.removed_by_category.get("ingress-host", []))


def test_kept_data_key_not_reported() -> None:
    """Unchanged lines (DATABASE_PASSWORD, OBJECT_STORE_BUCKET_NAME) must not appear."""
    moza = summarize_diff(SAMPLE_DIFF)["sandboxed-local/moza"]
    all_removed = [line for items in moza.removed_by_category.values() for line in items]
    assert not any("DATABASE_PASSWORD" in line for line in all_removed)
    assert not any("OBJECT_STORE_BUCKET_NAME" in line for line in all_removed)


def test_empty_diff_reports_nothing() -> None:
    assert summarize_diff("") == {}
    assert "nothing disappeared" in format_report({}).lower()


def test_report_mentions_judging() -> None:
    report = format_report(summarize_diff(SAMPLE_DIFF))
    assert "sandboxed-local/moza" in report
    assert "regression" in report.lower()
