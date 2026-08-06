"""One schema per schema-version (RC-32).

The project schema used to be a single file for the whole 2.x range, and
``git_monitor`` validated a file straight from git against it, before any
migration. That combination meant every old form had to stay in the schema for
as long as one unprocessed file still carried it, so a migration could be
written but never finished.

These tests hold the new arrangement in place:

- every version in the migration chain is validatable, and a migration without a
  schema fails loudly instead of quietly rejecting files (``check_schema_versions``);
- the gate validates against the version the file declares, and refuses a missing,
  malformed or unknown declaration rather than falling back to the newest or the
  loosest schema;
- an old form is accepted at the version that carried it and REJECTED at the
  version whose migration removed it - that is what "a migration can be finished"
  means, and it is what the previous arrangement could not do.
"""

import json

import pytest
from opi.core.project_schema import (
    LEGACY_PATCH_DIR,
    ProjectSchemaError,
    check_schema_versions,
    known_schema_versions,
    latest_schema_version,
    validate_declared_project_schema,
    validate_project_schema,
    version_key,
)
from opi.services.schema_migration import LATEST_SCHEMA_VERSION, MIGRATION_STEPS, SCHEMA_VERSIONS


def _project(**extra: object) -> dict:
    """Minimal project that validates at every version."""
    base: dict = {
        "name": "voorbeeld",
        "schema-version": LATEST_SCHEMA_VERSION,
        "users": [{"email": "admin@rijksoverheid.nl", "role": "admin"}],
        "components": [{"name": "frontend", "type": "deployment"}],
    }
    base.update(extra)
    return base


class TestSchemaSetCompleteness:
    def test_every_migration_version_has_a_schema(self) -> None:
        check_schema_versions(SCHEMA_VERSIONS)

    def test_latest_schema_annotation_matches_the_migration_chain(self) -> None:
        assert latest_schema_version() == LATEST_SCHEMA_VERSION

    def test_known_versions_are_exactly_the_migration_chain(self) -> None:
        assert known_schema_versions() == tuple(float(v) for v in SCHEMA_VERSIONS)

    def test_a_new_migration_without_a_schema_fails_loudly(self) -> None:
        # The situation this guards: someone adds a v2.6 -> v2.7 migration and
        # forgets the schema. Files stamped 2.7 would then be rejected by the gate
        # with nobody noticing; instead startup stops here.
        with pytest.raises(ProjectSchemaError, match=r"2\.7"):
            check_schema_versions((*SCHEMA_VERSIONS, 2.7))

    def test_a_gap_in_the_chain_fails_loudly(self) -> None:
        with pytest.raises(ProjectSchemaError, match=r"2\.15"):
            check_schema_versions((*SCHEMA_VERSIONS[:-1], 2.15, SCHEMA_VERSIONS[-1]))

    def test_a_schema_without_a_migration_fails_loudly(self) -> None:
        chain = tuple(v for v in SCHEMA_VERSIONS if v != 2.4)
        with pytest.raises(ProjectSchemaError, match=r"2\.4"):
            check_schema_versions(chain)

    def test_migration_steps_and_versions_agree(self) -> None:
        assert tuple(v for v, _ in MIGRATION_STEPS) == SCHEMA_VERSIONS[2:]

    @pytest.mark.parametrize("version", [v for v in SCHEMA_VERSIONS if v != LATEST_SCHEMA_VERSION])
    def test_each_legacy_patch_is_a_json_object(self, version: float) -> None:
        patch = json.loads((LEGACY_PATCH_DIR / f"v{version_key(version)}.json").read_text(encoding="utf-8"))
        assert isinstance(patch, dict)
        assert patch.get("$comment"), "a patch must say which migration it is the schema side of"

    @pytest.mark.parametrize("version", SCHEMA_VERSIONS)
    def test_a_minimal_project_validates_at_every_version(self, version: float) -> None:
        validate_project_schema(_project(**{"schema-version": version}), schema_version=float(version))


class TestDeclaredVersionGate:
    """The declared version comes from the same untrusted file as the rest."""

    def test_missing_version_is_refused(self) -> None:
        data = _project()
        del data["schema-version"]
        with pytest.raises(ProjectSchemaError, match="schema-version"):
            validate_declared_project_schema(data)

    def test_unknown_version_is_refused(self) -> None:
        with pytest.raises(ProjectSchemaError, match="onbekend"):
            validate_declared_project_schema(_project(**{"schema-version": 99}))

    def test_non_numeric_version_is_refused(self) -> None:
        with pytest.raises(ProjectSchemaError, match="schema-version"):
            validate_declared_project_schema(_project(**{"schema-version": "2.6"}))

    def test_boolean_version_is_refused(self) -> None:
        # bool is an int subclass; a YAML `schema-version: true` must not pass for 1.
        with pytest.raises(ProjectSchemaError, match="schema-version"):
            validate_declared_project_schema(_project(**{"schema-version": True}))

    def test_current_file_passes(self) -> None:
        validate_declared_project_schema(_project())

    def test_hostile_namespace_is_still_refused_at_an_old_version(self) -> None:
        # An old version is a different schema, not a laxer gate: the security
        # constraints that were always there still bite.
        hostile = _project(
            **{
                "schema-version": 2,
                "deployments": [{"name": "prod", "cluster": "local", "namespace": "kube-system\nowner: root"}],
            }
        )
        with pytest.raises(ProjectSchemaError):
            validate_declared_project_schema(hostile)


class TestMigrationsCanBeFinished:
    """Each old form: accepted at the version that carried it, rejected at the next.

    Rejected-at-the-newest is the whole point. Before RC-32 the newest schema had
    to keep accepting all of these, so none of these migrations could be closed.
    """

    def test_keycloak_config_block_accepted_at_2_2_rejected_at_2_3(self) -> None:
        data = _project(config={"keycloak": [{"host": "https://kc", "realm": "r", "username": "u"}]})
        validate_project_schema(data, schema_version=2.2)
        with pytest.raises(ProjectSchemaError, match="config"):
            validate_project_schema(data, schema_version=2.3)

    def test_string_path_accepted_at_2_1_rejected_at_2_2(self) -> None:
        data = _project(components=[{"name": "frontend", "path": "/api", "rewrite-path": "/"}])
        validate_project_schema(data, schema_version=2.1)
        with pytest.raises(ProjectSchemaError, match="path"):
            validate_project_schema(data, schema_version=2.2)

    def test_component_root_flag_accepted_at_2_rejected_at_2_1(self) -> None:
        data = _project(components=[{"name": "frontend", "root": True}])
        validate_project_schema(data, schema_version=2)
        with pytest.raises(ProjectSchemaError, match="root"):
            validate_project_schema(data, schema_version=2.1)
