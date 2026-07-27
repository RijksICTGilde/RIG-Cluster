"""Tests for the duplicate resource-identity failsafe in kustomization generation.

Covers Bevinding B (step 2) of the RC-6 plan: two manifests with the same kustomize
resource identity (apiVersion, kind, namespace, name) make kustomize refuse to build the
whole directory. That failure only surfaces inside the ArgoCD CMP, so OPI catches the class
before commit/push with a clear RuntimeError naming both files.
"""

import os

import pytest
from opi.generation.manifests import ManifestGenerator, check_duplicate_resource_identities

_PVC = """apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: web-data
  namespace: rig-prd-x
spec:
  accessModes: [ReadWriteOnce]
"""


def _write(path: str, content: str) -> None:
    with open(path, "w") as f:
        f.write(content)


def test_duplicate_identity_raises_with_both_filenames(tmp_path):
    _write(str(tmp_path / "web-data-pvc.yaml"), _PVC)
    _write(str(tmp_path / "web-data-pvc.marked-for-deletion.yaml"), _PVC)

    with pytest.raises(RuntimeError) as exc:
        check_duplicate_resource_identities(
            str(tmp_path),
            ["web-data-pvc.yaml", "web-data-pvc.marked-for-deletion.yaml"],
        )

    message = str(exc.value)
    assert "web-data-pvc.yaml" in message
    assert "web-data-pvc.marked-for-deletion.yaml" in message
    assert "web-data" in message  # the resource identity is named


def test_different_kind_same_name_is_allowed(tmp_path):
    _write(
        str(tmp_path / "a.yaml"),
        "apiVersion: v1\nkind: Service\nmetadata:\n  name: web\n  namespace: ns\n",
    )
    _write(
        str(tmp_path / "b.yaml"),
        "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: web\n  namespace: ns\n",
    )
    # Same name, different kind -> distinct identities -> no error.
    check_duplicate_resource_identities(str(tmp_path), ["a.yaml", "b.yaml"])


def test_different_namespace_same_name_is_allowed(tmp_path):
    _write(
        str(tmp_path / "a.yaml"),
        "apiVersion: v1\nkind: PersistentVolumeClaim\nmetadata:\n  name: web-data\n  namespace: ns-a\n",
    )
    _write(
        str(tmp_path / "b.yaml"),
        "apiVersion: v1\nkind: PersistentVolumeClaim\nmetadata:\n  name: web-data\n  namespace: ns-b\n",
    )
    check_duplicate_resource_identities(str(tmp_path), ["a.yaml", "b.yaml"])


def test_files_not_on_disk_are_skipped(tmp_path):
    # Path-rewritten / not-yet-written entries must not crash the scan.
    check_duplicate_resource_identities(str(tmp_path), ["ghost-a.yaml", "ghost-b.yaml"])


def test_malformed_yaml_is_skipped_not_fatal(tmp_path):
    _write(str(tmp_path / "broken.yaml"), "this: : : not valid yaml : [")
    _write(str(tmp_path / "web-data-pvc.yaml"), _PVC)
    # A parse error is a different failure (kustomize surfaces it); the scan must not raise.
    check_duplicate_resource_identities(str(tmp_path), ["broken.yaml", "web-data-pvc.yaml"])


def test_create_kustomization_files_propagates_duplicate_error(tmp_path):
    output_dir = str(tmp_path)
    _write(os.path.join(output_dir, "web-data-pvc.yaml"), _PVC)
    _write(os.path.join(output_dir, "web-data-pvc.marked-for-deletion.yaml"), _PVC)

    generator = ManifestGenerator()
    # RuntimeError must propagate (not be swallowed into a False return) so the deploy stops
    # before the broken kustomization is committed.
    with pytest.raises(RuntimeError):
        generator.create_kustomization_files(
            output_dir=output_dir,
            regular_files=["web-data-pvc.yaml", "web-data-pvc.marked-for-deletion.yaml"],
            sops_files=[],
        )
