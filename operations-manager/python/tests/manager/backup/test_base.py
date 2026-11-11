"""``ResourceType`` and the vocabulary a backup label uses outside this module.

The API and the wizard call a bucket backup ``minio``, where the snapshot tag and this enum
say ``bucket``. :meth:`ResourceType.from_string` quietly maps an unknown label to ``PVC``,
which is exactly wrong for ``minio`` (RC-482, finding R-N1): a merged restore use case that
read the API's label through it would restore a bucket snapshot as if it were a PVC.
"""

from __future__ import annotations

import pytest
from opi.manager.backup.base import ResourceType


class TestFromBackupLabel:
    def test_minio_becomes_bucket(self) -> None:
        assert ResourceType.from_backup_label("minio") is ResourceType.BUCKET

    def test_minio_does_not_become_pvc(self) -> None:
        """What ``from_string`` would quietly do, and why this exists."""
        assert ResourceType.from_string("minio") is ResourceType.PVC
        assert ResourceType.from_backup_label("minio") is not ResourceType.from_string("minio")

    @pytest.mark.parametrize("label", ["pvc", "database"])
    def test_the_other_two_labels_pass_through(self, label: str) -> None:
        assert ResourceType.from_backup_label(label) is ResourceType(label)

    def test_an_unknown_label_is_refused(self) -> None:
        with pytest.raises(ValueError, match="geen-type"):
            ResourceType.from_backup_label("geen-type")


class TestBackupLabel:
    def test_bucket_reports_as_minio(self) -> None:
        assert ResourceType.BUCKET.backup_label == "minio"

    @pytest.mark.parametrize("resource_type", [ResourceType.PVC, ResourceType.DATABASE])
    def test_the_other_two_report_as_their_own_value(self, resource_type: ResourceType) -> None:
        assert resource_type.backup_label == resource_type.value

    @pytest.mark.parametrize("resource_type", list(ResourceType))
    def test_round_trips_through_from_backup_label(self, resource_type: ResourceType) -> None:
        assert ResourceType.from_backup_label(resource_type.backup_label) is resource_type
