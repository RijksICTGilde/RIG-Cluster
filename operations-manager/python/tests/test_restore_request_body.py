"""What the database and bucket restore endpoints require of a request.

Asked by the zad-cli project: ``POST /api/v1/restore/database/...`` answered 422 and
they suspected the ``reference_name`` in the path. It is not the path - the endpoints
take a required JSON body with the target to restore INTO, and a request without one
is a body validation error. These tests pin which fields are required, so the answer
stays true if the models change.
"""

import pytest
from opi.api.restore_router import BucketRestoreRequest, DatabaseRestoreRequest
from pydantic import ValidationError


def _missing_fields(exc: ValidationError) -> set[str]:
    return {str(error["loc"][0]) for error in exc.errors() if error["type"] == "missing"}


class TestDatabaseRestoreRequest:
    def test_empty_body_names_every_missing_target_field(self):
        with pytest.raises(ValidationError) as excinfo:
            DatabaseRestoreRequest()

        assert _missing_fields(excinfo.value) == {
            "target_database_host",
            "target_database_name",
            "target_database_user",
            "target_database_password",
        }

    def test_snapshot_id_is_optional_and_defaults_to_the_latest(self):
        request = DatabaseRestoreRequest(
            target_database_host="postgresql.rig-p0.svc.cluster.local",
            target_database_name="app",
            target_database_user="app",
            target_database_password="secret",
        )

        assert request.snapshot_id is None
        assert request.target_database_port == 5432


class TestBucketRestoreRequest:
    def test_empty_body_names_every_missing_target_field(self):
        with pytest.raises(ValidationError) as excinfo:
            BucketRestoreRequest()

        assert "target_bucket_name" in _missing_fields(excinfo.value)
