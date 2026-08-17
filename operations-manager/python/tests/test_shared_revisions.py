"""Tests for the shared clone-state model (``generation`` + ``revisions``).

The shape is a recurring pattern, not a property of one service: postgresql-database and
minio-storage both carry it, and a future service composes the same model at whatever layer
it needs. Required fields mirror what 33 real revisions in the production project files carry.
"""

from __future__ import annotations

import pytest
from opi.services.catalog.minio.config_model import MinioStorageConfig
from opi.services.catalog.postgresql_database.config_model import PostgresqlDatabaseConfig
from opi.services.catalog.shared.revisions import CloneState
from pydantic import ValidationError

REAL_REVISION = {
    "generation": 1,
    "resource": "amtbz_2m9_productie_v1",
    "status": "active",
    "created_at": "2026-02-04T14:45:30.479744+00:00",
    "actions": [{"type": "create", "source": "provision", "timestamp": "2026-02-04T14:45:30.479744+00:00"}],
}


class TestCloneState:
    def test_accepts_a_real_revision(self):
        state = CloneState.model_validate({"generation": 1, "revisions": [REAL_REVISION]})
        assert state.generation == 1
        assert state.revisions[0].resource == "amtbz_2m9_productie_v1"
        assert state.revisions[0].actions[0].type == "create"
        # Only set once a revision is retired, so it stays absent on an active one.
        assert state.revisions[0].superseded_at is None

    def test_accepts_an_empty_block(self):
        # A service that has never been cloned carries neither key.
        assert CloneState.model_validate({}).revisions == []

    def test_rejects_an_unknown_key(self):
        with pytest.raises(ValidationError):
            CloneState.model_validate({"generatie": 1})

    def test_rejects_a_revision_missing_a_required_field(self):
        broken = {k: v for k, v in REAL_REVISION.items() if k != "resource"}
        with pytest.raises(ValidationError):
            CloneState.model_validate({"revisions": [broken]})


class TestComposingServices:
    def test_postgresql_database_is_clone_state_only(self):
        cfg = PostgresqlDatabaseConfig.model_validate({"generation": 2, "revisions": [REAL_REVISION]})
        assert cfg.generation == 2

    def test_minio_validates_both_of_its_layers(self):
        # config_model is one class per service, so the project-level setting and the
        # deployment-level clone state must both validate against the same model.
        assert MinioStorageConfig.model_validate({"enable-versioning": True}).enable_versioning is True
        assert MinioStorageConfig.model_validate({"generation": 1}).enable_versioning is None

    def test_minio_still_rejects_a_key_belonging_to_neither_layer(self):
        with pytest.raises(ValidationError):
            MinioStorageConfig.model_validate({"enable-versionning": True})
