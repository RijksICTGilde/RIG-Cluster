"""RC-19 Layer 1: offline upgrade-safety replay over existing project files.

The release on ``branches-samenvoegen-naar-main`` changes how a project file is
read (per-service config models, the four-layer ``validate_service_configs``, the
scope choice and multiple PostgreSQL schemas). Every branch was green against test
data; the question this test answers is the one that matters to users: do the
*existing* project files still pass, or does someone silently lose something when
their file is next reprocessed?

This is the cheap layer of the plan: no cluster, no git, no SOPS, no key. It takes
each project file, runs it through ``migrate_to_latest`` and then the exact
validation chain production runs before any write, and reports per file. It catches
the ``dp-bn7`` class of fault: a file that no longer passes validation and would
therefore stall silently on its next reconcile.

Two things are asserted, mirroring ``ProjectStore._validate`` exactly:

1. ``migrate_to_latest`` — production migrates in memory first, then validates. So
   we validate the *migrated* data, never the raw file, or we would measure
   something other than what production does.
2. ``validate_project_schema`` then ``await validate_project_structure`` — the JSON
   schema, then the structural/cross-field checks (which end in
   ``validate_service_configs``, the per-service typed-config gate).

No AGE key is needed: neither migration nor validation decrypts anything, so the
encrypted blocks are opaque strings here (see the plan, section 2a).

Coverage comes from two sources:
- Committed sanitized fixtures in ``tests/fixtures/upgrade_safety/`` — always run,
  so the harness itself is exercised in CI and acts as a regression guard.
- The real ``zad-projects`` files, when available: set ``RIG_PROJECTS_DIR`` to a
  projects checkout and every ``*.yaml`` there is replayed too. Without it that
  part skips with a clear message, so the test bites locally and on a server with a
  projects checkout, and still runs everywhere on the fixtures.
"""

from __future__ import annotations

import copy
import glob
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from opi.core.project_schema import ProjectIntegrityError, ProjectSchemaError, validate_project_schema
from opi.manager.project_validation import validate_project_structure
from opi.services.schema_migration import LATEST_SCHEMA_VERSION, migrate_to_latest
from opi.utils.yaml_util import load_yaml_from_path

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "upgrade_safety"

#: Env var pointing at a checkout of the projects repo (one YAML per project). When
#: set, every file there is replayed in addition to the committed fixtures.
PROJECTS_DIR_ENV = "RIG_PROJECTS_DIR"


@dataclass
class ReplayOutcome:
    """The result of replaying one project file through migrate + validate."""

    name: str
    #: The stage the file failed at, or "ok". One of: migrate, schema, structure, ok.
    stage: str
    was_migrated: bool
    from_version: int | float | str | None
    to_version: int | float | str | None
    #: Top-level keys the migration added / removed, so the migration outcome is
    #: itself reviewable (plan section 3: "wat verandert migrate_to_latest").
    added_keys: list[str] = field(default_factory=list)
    removed_keys: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.stage == "ok"


async def replay_project_data(raw: dict[str, Any], name: str) -> ReplayOutcome:
    """Migrate then validate one project's data exactly as production does.

    Never mutates ``raw``. Captures the failure class (migrate / schema / structure)
    rather than raising, so a caller can report every file instead of stopping at the
    first bad one.
    """
    before_keys = set(raw.keys())
    before_version = raw.get("schema-version")

    try:
        migrated, was_migrated = migrate_to_latest(copy.deepcopy(raw))
    except (ValueError, KeyError, TypeError) as exc:
        return ReplayOutcome(
            name=name,
            stage="migrate",
            was_migrated=False,
            from_version=before_version,
            to_version=None,
            error=f"{type(exc).__name__}: {exc}",
        )

    after_keys = set(migrated.keys())
    outcome = ReplayOutcome(
        name=name,
        stage="ok",
        was_migrated=was_migrated,
        from_version=before_version,
        to_version=migrated.get("schema-version"),
        added_keys=sorted(after_keys - before_keys),
        removed_keys=sorted(before_keys - after_keys),
    )

    try:
        validate_project_schema(migrated)
    except ProjectSchemaError as exc:
        outcome.stage = "schema"
        outcome.error = str(exc)
        return outcome

    try:
        await validate_project_structure(migrated)
    except ProjectIntegrityError as exc:
        outcome.stage = "structure"
        outcome.error = str(exc)
        return outcome

    return outcome


def _load(path: str) -> dict[str, Any]:
    data = load_yaml_from_path(path)
    if data is None:
        raise AssertionError(f"Could not load YAML from {path}")
    return data


def _fixture_paths() -> list[str]:
    return sorted(glob.glob(str(FIXTURES_DIR / "*.yaml")))


def _project_dir_paths() -> list[str]:
    projects_dir = os.environ.get(PROJECTS_DIR_ENV)
    if not projects_dir or not os.path.isdir(projects_dir):
        return []
    return sorted(glob.glob(os.path.join(projects_dir, "*.yaml")))


# ---------------------------------------------------------------------------
# Committed fixtures: always run, so the harness is exercised in CI.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", _fixture_paths(), ids=lambda p: os.path.basename(p))
async def test_fixture_migrates_and_validates(path: str) -> None:
    """Every committed sanitized project file must migrate AND validate cleanly.

    A failure here is a real finding about the reading code, not a broken test: it
    means an existing-shaped file no longer survives the new validation chain. Fix
    the code (or the migration), not the fixture.
    """
    outcome = await replay_project_data(_load(path), os.path.basename(path))
    assert outcome.ok, f"{outcome.name} failed at {outcome.stage}: {outcome.error}"
    # The migration must always land the file on the latest schema version, so no
    # file is left carrying a stale version number after a successful reprocess.
    assert outcome.to_version == LATEST_SCHEMA_VERSION, (
        f"{outcome.name}: expected schema-version {LATEST_SCHEMA_VERSION}, got {outcome.to_version}"
    )


async def test_migration_does_not_drop_top_level_sections() -> None:
    """Migration may add sections (e.g. relocate invites into services) but must not

    silently drop a whole top-level section. A removed section is the visible edge of
    the "raakt iemand iets kwijt" question at the file level.

    ``invites`` is the one deliberate removal (it moves into ``services/invite``), so
    it is the only allowed disappearance.
    """
    allowed_removals = {"invites"}
    for path in _fixture_paths():
        outcome = await replay_project_data(_load(path), os.path.basename(path))
        unexpected = set(outcome.removed_keys) - allowed_removals
        assert not unexpected, f"{outcome.name}: migration dropped top-level sections {sorted(unexpected)}"


async def test_legacy_invites_block_is_relocated_not_lost() -> None:
    """The legacy top-level ``invites:`` block must land in the invite service, not

    vanish. This guards the exact silent-loss the plan worries about: a section that
    disappears from the file without reappearing where the new code reads it.
    """
    raw = _load(str(FIXTURES_DIR / "invites-legacy.yaml"))
    assert "invites" in raw, "fixture precondition: legacy top-level invites block present"

    migrated, was_migrated = migrate_to_latest(copy.deepcopy(raw))
    assert was_migrated
    assert "invites" not in migrated, "top-level invites should be removed by the v2.6 migration"

    from opi.services.project import Project

    relocated = Project(migrated).get("services/invite/config")
    assert relocated is not None, "invites must be relocated to services/invite/config"
    assert relocated.get("active"), "the active invite must survive the relocation"


# ---------------------------------------------------------------------------
# Real project files: only when a projects checkout is available.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _project_dir_paths(),
    reason=f"no projects checkout at ${PROJECTS_DIR_ENV}; set it to a zad-projects checkout to replay real files",
)
async def test_real_project_files_migrate_and_validate() -> None:
    """Replay every real project file and fail with a per-file report of what broke.

    This is the part that actually answers the plan's question against production
    data. It reports ALL failures at once (not just the first) so an upgrade review
    sees the full blast radius in one run.
    """
    outcomes = [await replay_project_data(_load(p), os.path.basename(p)) for p in _project_dir_paths()]
    failures = [o for o in outcomes if not o.ok]

    report = "\n".join(f"  - {o.name}: FAILED at {o.stage}: {o.error}" for o in failures)
    assert not failures, f"{len(failures)}/{len(outcomes)} project files failed the upgrade replay:\n{report}"


# ---------------------------------------------------------------------------
# The harness must actually detect a bad file (not just pass everything green).
# ---------------------------------------------------------------------------


async def test_replay_reports_structure_failure() -> None:
    """A file whose component references a service missing at project level must be

    reported as a structure failure, proving the replay detects the dp-bn7 class
    rather than passing everything.
    """
    raw = _load(str(FIXTURES_DIR / "algor-odc.yaml"))
    # component-1 references keycloak; remove it from the project-level services so
    # the component reference no longer resolves.
    raw["services"] = ["publish-on-web"]

    outcome = await replay_project_data(raw, "broken-structure")
    assert outcome.stage == "structure", f"expected structure failure, got {outcome.stage}: {outcome.error}"
    assert outcome.error is not None


async def test_replay_reports_schema_failure() -> None:
    """A file that violates the JSON schema must be reported at the schema stage."""
    raw = _load(str(FIXTURES_DIR / "algor-odc.yaml"))
    # An encrypted field carrying a plaintext value fails the AGE pattern in the schema.
    raw["config"]["api-key"] = "not-an-encrypted-value"

    outcome = await replay_project_data(raw, "broken-schema")
    assert outcome.stage == "schema", f"expected schema failure, got {outcome.stage}: {outcome.error}"
    assert outcome.error is not None
