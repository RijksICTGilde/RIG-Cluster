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
import logging
import re
from typing import cast

import pytest
from opi.core.git_monitor import file_change_handler
from opi.core.project_schema import (
    LEGACY_PATCH_DIR,
    SCHEMA_PATH,
    ProjectSchemaError,
    SchemaVersion,
    check_schema_versions,
    known_schema_versions,
    latest_schema_version,
    parse_schema_version,
    validate_declared_project_schema,
    validate_project_schema,
    version_key,
)
from opi.services.schema_migration import (
    LATEST_SCHEMA_VERSION,
    LATEST_SCHEMA_VERSION_VALUE,
    MIGRATION_STEPS,
    SCHEMA_VERSIONS,
)

#: De versie waarin ``users[].role`` een lijst ``users[].roles`` werd.
_ROLLEN_ALS_LIJST = parse_schema_version("2.13")


def _lid(versie: object) -> dict:
    """Het lid in de vorm die DEZE versie kent: ``role`` tot 2.12, ``roles`` vanaf 2.13.

    Op de versie en niet op "is dit de nieuwste": met die vergelijking viel elke versie na
    2.13 terug op de oude vorm, en een nieuwe schemastap maakte de minimale projecttoets
    rood op een rolmigratie waar hij niet over gaat.
    """
    try:
        geparsed = parse_schema_version(versie)  # type: ignore[arg-type]
    except ProjectSchemaError, TypeError, ValueError:
        return {"email": "admin@rijksoverheid.nl", "role": "admin"}
    if geparsed >= _ROLLEN_ALS_LIJST:
        return {"email": "admin@rijksoverheid.nl", "roles": ["admin", "ops"]}
    return {"email": "admin@rijksoverheid.nl", "role": "admin"}


def _project(**extra: object) -> dict:
    """Minimal project that validates at every version."""
    versie = cast("SchemaVersion | int | float | str", extra.get("schema-version", LATEST_SCHEMA_VERSION_VALUE))
    base: dict = {
        "name": "voorbeeld",
        "schema-version": LATEST_SCHEMA_VERSION_VALUE,
        "users": [_lid(versie)],
        "components": [{"name": "frontend", "type": "deployment"}],
    }
    base.update(extra)
    return base


def _oud_project(**extra: object) -> dict:
    """Hetzelfde, maar met de ledenvorm van VOOR 2.13 (``role`` in plaats van ``roles``).

    Voor een toets die dezelfde gegevens tegen twee OUDE versies legt: geen enkele
    ledenvorm valideert zowel onder 2.12 als onder 2.13, want 2.13 verving het veld.
    """
    data = _project(**extra)
    data["users"] = [{"email": "admin@rijksoverheid.nl", "role": "admin"}]
    return data


class TestSchemaSetCompleteness:
    def test_every_migration_version_has_a_schema(self) -> None:
        check_schema_versions(SCHEMA_VERSIONS)

    def test_latest_schema_annotation_matches_the_migration_chain(self) -> None:
        assert latest_schema_version() == LATEST_SCHEMA_VERSION

    def test_known_versions_are_exactly_the_migration_chain(self) -> None:
        assert known_schema_versions() == SCHEMA_VERSIONS

    def test_a_new_migration_without_a_schema_fails_loudly(self) -> None:
        # The situation this guards: someone adds the next migration and forgets the
        # schema. Files stamped with that version would then be rejected by the gate with
        # nobody noticing; instead startup stops here. The version used is deliberately one
        # past the real chain, so this test does not have to be touched again when a
        # migration lands (which is exactly what happened when 2.7 became real).
        latest = SCHEMA_VERSIONS[-1]
        next_version = SchemaVersion(latest.major, latest.minor + 1)
        with pytest.raises(ProjectSchemaError, match=re.escape(version_key(next_version))):
            check_schema_versions((*SCHEMA_VERSIONS, next_version))

    def test_a_gap_in_the_chain_fails_loudly(self) -> None:
        # A version in the middle of the chain without a patch.
        latest = SCHEMA_VERSIONS[-1]
        zonder_patch = SchemaVersion(latest.major, latest.minor + 2)
        with pytest.raises(ProjectSchemaError, match=re.escape(version_key(zonder_patch))):
            check_schema_versions((*SCHEMA_VERSIONS[:-1], zonder_patch, latest))

    def test_a_schema_without_a_migration_fails_loudly(self) -> None:
        chain = tuple(v for v in SCHEMA_VERSIONS if v != parse_schema_version("2.4"))
        with pytest.raises(ProjectSchemaError, match=r"2\.4"):
            check_schema_versions(chain)

    def test_migration_steps_and_versions_agree(self) -> None:
        assert tuple(v for v, _ in MIGRATION_STEPS) == SCHEMA_VERSIONS[2:]

    @pytest.mark.parametrize("version", [v for v in SCHEMA_VERSIONS if v != LATEST_SCHEMA_VERSION])
    def test_each_legacy_patch_is_a_json_object(self, version: SchemaVersion) -> None:
        patch = json.loads((LEGACY_PATCH_DIR / f"v{version_key(version)}.json").read_text(encoding="utf-8"))
        assert isinstance(patch, dict)
        assert patch.get("$comment"), "a patch must say which migration it is the schema side of"

    @pytest.mark.parametrize("version", SCHEMA_VERSIONS)
    def test_a_minimal_project_validates_at_every_version(self, version: SchemaVersion) -> None:
        validate_project_schema(_project(**{"schema-version": version_key(version)}), schema_version=version)


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

    def test_malformed_version_is_refused(self) -> None:
        with pytest.raises(ProjectSchemaError, match="schema-version"):
            validate_declared_project_schema(_project(**{"schema-version": "2.x"}))

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
        data = _oud_project(config={"keycloak": [{"host": "https://kc", "realm": "r", "username": "u"}]})
        validate_project_schema(data, schema_version=parse_schema_version("2.2"))
        with pytest.raises(ProjectSchemaError, match="config"):
            validate_project_schema(data, schema_version=parse_schema_version("2.3"))

    def test_string_path_accepted_at_2_1_rejected_at_2_2(self) -> None:
        data = _oud_project(components=[{"name": "frontend", "path": "/api", "rewrite-path": "/"}])
        validate_project_schema(data, schema_version=parse_schema_version("2.1"))
        with pytest.raises(ProjectSchemaError, match="path"):
            validate_project_schema(data, schema_version=parse_schema_version("2.2"))

    def test_component_root_flag_accepted_at_2_rejected_at_2_1(self) -> None:
        data = _oud_project(components=[{"name": "frontend", "root": True}])
        validate_project_schema(data, schema_version=parse_schema_version("2"))
        with pytest.raises(ProjectSchemaError, match="root"):
            validate_project_schema(data, schema_version=parse_schema_version("2.1"))

    def test_root_domains_block_accepted_at_2_4_rejected_at_2_5(self) -> None:
        data = _oud_project(domains={"allowed-domains": [{"domain": "rijksapps.nl", "status": "approved"}]})
        validate_project_schema(data, schema_version=parse_schema_version("2.4"))
        with pytest.raises(ProjectSchemaError, match="domains"):
            validate_project_schema(data, schema_version=parse_schema_version("2.5"))

    def test_root_invites_block_accepted_at_2_5_rejected_at_2_6(self) -> None:
        data = _oud_project(invites={"settings": {"default_language": "nl"}, "active": []})
        validate_project_schema(data, schema_version=parse_schema_version("2.5"))
        with pytest.raises(ProjectSchemaError, match="invites"):
            validate_project_schema(data, schema_version=parse_schema_version("2.6"))

    def test_uses_services_accepted_at_v1_rejected_at_v2(self) -> None:
        data = _oud_project(components=[{"name": "frontend", "uses-services": ["publish-on-web"]}])
        validate_project_schema(data, schema_version=parse_schema_version("1"))
        with pytest.raises(ProjectSchemaError, match="uses-services"):
            validate_project_schema(data, schema_version=parse_schema_version("2"))

    def test_v0_publish_on_web_boolean_accepted_at_v1_rejected_at_v2(self) -> None:
        data = _oud_project(components=[{"name": "frontend", "publish-on-web": True}])
        validate_project_schema(data, schema_version=parse_schema_version("1"))
        with pytest.raises(ProjectSchemaError, match="publish-on-web"):
            validate_project_schema(data, schema_version=parse_schema_version("2"))

    def test_component_description_rejected_at_2_14_accepted_at_2_15(self) -> None:
        """The reverse of the rest of this class: a FIELD ADDED, not removed.

        2.14 does not know `description` and the component is a closed object, so an
        old file carrying it is rejected rather than silently accepted.
        """
        new_form = _project(components=[{"name": "frontend", "type": "deployment", "description": "Wat het doet"}])
        validate_project_schema(new_form, schema_version=parse_schema_version("2.15"))

        old_form = _project(components=[{"name": "frontend", "type": "deployment", "description": "Wat het doet"}])
        with pytest.raises(ProjectSchemaError, match="description"):
            validate_project_schema(old_form, schema_version=parse_schema_version("2.14"))

    def test_component_description_over_500_chars_is_rejected(self) -> None:
        data = _project(components=[{"name": "frontend", "type": "deployment", "description": "x" * 501}])
        with pytest.raises(ProjectSchemaError, match="description"):
            validate_project_schema(data, schema_version=LATEST_SCHEMA_VERSION)

    def test_component_description_at_exactly_500_chars_is_accepted(self) -> None:
        data = _project(components=[{"name": "frontend", "type": "deployment", "description": "x" * 500}])
        validate_project_schema(data, schema_version=LATEST_SCHEMA_VERSION)

    def test_the_newest_schema_is_the_newest_shape_only(self) -> None:
        # The regression this locks: someone re-adds a legacy form to the newest
        # schema "so the gate stops rejecting file X". That is what made the schema
        # unfinishable; the form belongs in the patch of X's own version.
        latest = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        assert "domains" not in latest["properties"]
        assert "invites" not in latest["properties"]
        component = latest["$defs"]["component"]["properties"]
        assert "uses-services" not in component
        assert "storage" not in component
        assert "publish-on-web" not in component


class TestGitMonitorGate:
    """The gate is wired to the declared version, and says so when it rejects."""

    @pytest.mark.asyncio
    async def test_file_without_declared_version_is_rejected_and_logged(self, caplog) -> None:
        data = _project(deployments=[{"name": "prod", "cluster": "local", "namespace": "voorbeeld"}])
        del data["schema-version"]

        with caplog.at_level(logging.ERROR, logger="opi.core.git_monitor"):
            await file_change_handler("voorbeeld.yaml", data)

        # Loud, and explicit that the file was skipped: the old silent warning is
        # exactly how 22 production files went unprocessed unnoticed.
        assert any("NIET verwerkt" in record.message for record in caplog.records)

    @pytest.mark.asyncio
    async def test_legacy_file_is_no_longer_rejected_by_the_gate(self, caplog) -> None:
        # A v2.2 file with the pre-v2.3 config.keycloak block: rejected by the old
        # gate (it validated against the newest schema), accepted now.
        data = _project(
            **{
                "schema-version": 2.2,
                "config": {"keycloak": [{"host": "https://kc", "realm": "r", "username": "u"}]},
            }
        )
        validate_declared_project_schema(data)

        with caplog.at_level(logging.ERROR, logger="opi.core.git_monitor"):
            await file_change_handler("voorbeeld.yaml", data)

        # No deployments block, so nothing downstream runs; the point is that the
        # gate did not reject it.
        assert not any("NIET verwerkt" in record.message for record in caplog.records)
