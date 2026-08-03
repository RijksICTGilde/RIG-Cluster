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
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from opi.core.project_schema import ProjectIntegrityError, ProjectSchemaError, validate_project_schema
from opi.manager.project_validation import validate_project_structure
from opi.services.schema_migration import LATEST_SCHEMA_VERSION, migrate_to_latest
from opi.utils.yaml_util import load_yaml_from_path

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "upgrade_safety"

#: Env var pointing at a checkout of the projects repo (one YAML per project). When
#: set, every file there is replayed in addition to the committed fixtures.
PROJECTS_DIR_ENV = "RIG_PROJECTS_DIR"

#: Read-only production projects repo (plan section 2a / open decision 1). Cloned
#: shallow for the ``requires_infra`` replay when no local checkout is given. No key
#: is needed: nothing here decrypts the AGE blocks.
PROJECTS_REPO_ENV = "RIG_PROJECTS_REPO"
DEFAULT_PROJECTS_REPO = "https://git.claude.robbertuittenbroek.nl/robbert/rig-cluster-projects-github.git"

#: These files declare the odcn-production cluster; the domain-format checks read that
#: cluster's config (e.g. ``rijks.app`` supports dots there but not under ``local``).
PRODUCTION_CLUSTER = "odcn-production"

#: Known, PRE-EXISTING structural defects in the production data (not regressions this
#: release introduces): both list ``publish-on-web`` twice at project level, which the
#: structural validator rejects. They are the dp-bn7 class made visible -- on their next
#: processing run they stall silently. The source repo is read-only here, so they are
#: baselined by filename: the real-file replay stays green on today's data and turns RED
#: the moment a NEW file fails (a genuine regression) or a baselined file starts passing
#: (someone de-duplicated it -- drop it from the baseline).
KNOWN_REAL_FAILURES: dict[str, str] = {
    "dsm1j2-2ws.yaml": "staat meerdere keren in de services-lijst",
    "ug-zxt.yaml": "staat meerdere keren in de services-lijst",
}


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
# Real project files: reproduce what production actually does, then replay.
# ---------------------------------------------------------------------------


@pytest.fixture
def production_reality(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the replay reflect production, not the test host.

    Two production realities decide whether the verdict is faithful:

    * ``CLUSTER_MANAGER`` -> the cluster these files declare (odcn-production). The
      domain-format check reads per-cluster domain config, so validating odcn files
      under the default ``local`` cluster invents failures that production never sees.
    * ``SubdomainConnector`` -> stubbed to "available". It is the one genuinely external
      dependency in ``validate_project_structure`` (a live registry / DNS lookup); it
      raises ``gaierror`` on a host without network, which is neither a validation
      verdict nor a property of the file. For an existing project being reprocessed the
      registry already records that project as the subdomain owner, so the real check is
      a no-op there anyway.
    """
    import opi.core.config as opi_config
    import opi.forms.editables.enforcers as enforcers

    monkeypatch.setattr(opi_config.settings, "CLUSTER_MANAGER", PRODUCTION_CLUSTER)
    subdomain_stub = MagicMock()  # SubdomainConnector() is a sync call; only the method is awaited
    subdomain_stub.return_value.get_by_subdomain = AsyncMock(return_value=None)
    monkeypatch.setattr(enforcers, "SubdomainConnector", subdomain_stub)


def _assert_real_files_ok(outcomes: list[ReplayOutcome]) -> None:
    """Assert every real file passes, except the documented pre-existing baseline.

    Reports ALL failures at once so an upgrade review sees the full blast radius in one
    run, and holds the baseline both ways (a new failure OR a baselined file that now
    passes both fail the test).
    """
    failures = {o.name: f"{o.stage}: {o.error}" for o in outcomes if not o.ok}

    new_failures = {name: why for name, why in failures.items() if name not in KNOWN_REAL_FAILURES}
    report = "\n".join(f"  - {name}: {why}" for name, why in new_failures.items())
    assert not new_failures, (
        f"{len(new_failures)}/{len(outcomes)} production files NEWLY fail the upgrade replay "
        f"(the dp-bn7 class -- they would stall silently on their next reprocess):\n{report}"
    )

    fixed = [name for name in KNOWN_REAL_FAILURES if name not in failures]
    assert not fixed, f"Baselined-as-failing files now pass; drop them from KNOWN_REAL_FAILURES: {fixed}"

    for name, expected in KNOWN_REAL_FAILURES.items():
        assert expected in failures.get(name, ""), (
            f"Baselined file '{name}' fails for a different reason than documented "
            f"(expected '{expected}'): {failures.get(name)}"
        )


@pytest.mark.skipif(
    not _project_dir_paths(),
    reason=f"no projects checkout at ${PROJECTS_DIR_ENV}; set it to a zad-projects checkout to replay real files",
)
@pytest.mark.usefixtures("production_reality")
async def test_real_project_files_migrate_and_validate() -> None:
    """Replay every real project file from a local ``$RIG_PROJECTS_DIR`` checkout.

    Offline (no clone), so it can run in the default suite when a checkout is present.
    """
    paths = _project_dir_paths()
    outcomes = [await replay_project_data(_load(p), os.path.basename(p)) for p in paths]
    _assert_real_files_ok(outcomes)


def _clone_projects_repo(dest: Path) -> list[str]:
    """Shallow-clone the read-only production projects repo (open decision 1).

    Fails (rather than skips) when unreachable -- a safety test that silently never runs
    is worse than a red one -- unless ``RIG_PROJECTS_SKIP_IF_OFFLINE=1`` is set for a
    build machine without network access.
    """
    repo = os.environ.get(PROJECTS_REPO_ENV, DEFAULT_PROJECTS_REPO)
    result = subprocess.run(
        ["git", "clone", "--depth", "1", repo, str(dest)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        message = f"Could not clone the production projects repo ({repo}):\n{result.stderr.strip()}"
        if os.environ.get("RIG_PROJECTS_SKIP_IF_OFFLINE") == "1":
            pytest.skip(message)
        pytest.fail(message)
    files = sorted(glob.glob(str(dest / "projects" / "*.yaml"))) or sorted(glob.glob(str(dest / "**" / "*.yaml")))
    assert files, f"No project YAML files found in {repo}"
    return files


@pytest.mark.requires_infra
@pytest.mark.upgrade_safety
@pytest.mark.usefixtures("production_reality")
async def test_cloned_production_files_migrate_and_validate(tmp_path: Path) -> None:
    """Replay every real project file, cloned fresh from the read-only projects repo.

    This is the plan's open-decision-1 form: no local checkout, no key, always the
    current production data. ``requires_infra`` (needs network to clone) keeps it out of
    the default offline suite; run it with ``-m "requires_infra and upgrade_safety"``.
    """
    paths = _clone_projects_repo(tmp_path / "projects-repo")
    outcomes = [await replay_project_data(_load(p), os.path.basename(p)) for p in paths]
    _assert_real_files_ok(outcomes)


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
