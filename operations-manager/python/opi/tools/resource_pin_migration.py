"""Record the resource values that were set by hand before the wish was ever written down.

Until 20 August 2026 (RC-141) a user setting the memory of a component left no trace: the
value landed in the project file and nothing said who put it there. The tuner used to
treat the declared component as a floor, so such a value was safe anyway. RC-518 removed
that floor, so without this one-off those components would be tuned down again, and the
ones that were raised after an OOM would go straight back to falling over.

A separate tool and not a schema migration: this decision rests on the GIT HISTORY of the
project file, which a schema migration cannot see (it gets a parsed dict at load time, per
file, on every read). It also has to run once, deliberately, per cluster, with its list
read by a human before anything is written.

Usage, from ``operations-manager/python``::

    uv run python -m opi.tools.resource_pin_migration            # dry-run, prints the list
    uv run python -m opi.tools.resource_pin_migration --write    # commit the pins

A component whose file was imported wholesale (its oldest revision in this repository is
also the commit that created the file) shows no change before that import, so nothing is
pinned for it. The same holds past ``--max-commits``.

The decision per component: walking the history of its project file from newest to oldest,
the first commit that changed its declared resources and did NOT come from the platform is
a change made by hand. The fields that changed there are pinned, at the value that stands
today. A component that already carries a ``manual`` entry is skipped: it says for itself
what is pinned.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from dataclasses import dataclass
from itertools import pairwise
from typing import TYPE_CHECKING, Any

from opi.connectors.git import create_git_connector_for_project_files
from opi.core.config import settings
from opi.services.project_file_handler import ProjectFileHandler
from opi.utils.yaml_util import dump_yaml_to_string, load_yaml_round_trip

if TYPE_CHECKING:
    from opi.connectors.git import GitConnector

logger = logging.getLogger(__name__)

#: Commit-message prefixes the platform itself writes when it changes resources. The tuner
#: and the OOM watcher share one commit path, so one prefix covers both.
PLATFORM_COMMIT_PREFIXES = ("auto-tune:", "sanitize:")

#: How far back the walk looks per project file. A one-off over a shared repository, and
#: every revision costs a ``git show``: far enough to reach the creation of a component,
#: not the whole repository.
DEFAULT_MAX_COMMITS = 200


@dataclass(frozen=True)
class Snapshot:
    """One revision of a project file: what it said, and who said it."""

    ref: str
    timestamp: str
    message: str
    data: dict[str, Any]


@dataclass(frozen=True)
class PinCandidate:
    """A component whose resources were last changed by hand, and the commit that says so."""

    project: str
    component: str
    fields: tuple[str, ...]
    ref: str
    timestamp: str
    message: str


def is_platform_commit(snapshot: Snapshot, previous: Snapshot, component: str) -> bool:
    """Whether the platform made this change rather than a person.

    Two signals, because neither alone holds over the whole history: the commit message
    (the tuner and the OOM watcher commit under their own prefix) and a resource history
    entry that appeared in this very commit (what the tuner wrote back when it still
    touched the declared component).
    """
    if snapshot.message.startswith(PLATFORM_COMMIT_PREFIXES):
        return True
    return _history_length(snapshot.data, component) > _history_length(previous.data, component)


def _history_length(data: dict[str, Any], component: str) -> int:
    for comp in data.get("components") or []:
        if isinstance(comp, dict) and comp.get("name") == component:
            history = (comp.get("resources") or {}).get("history")
            return len(history) if isinstance(history, list) else 0
    return 0


def _declared_resources(handler: ProjectFileHandler, data: dict[str, Any], component: str) -> dict[str, str] | None:
    """The four effective resource values of a component, or None when it does not exist.

    Effective and not literal: a field the file leaves out runs on the platform default,
    so filling a field with the value it already had is not a change made by hand.
    """
    names = [c.get("name") for c in data.get("components") or [] if isinstance(c, dict)]
    if component not in names:
        return None
    return handler.extract_component_resources(data, component)


def find_pin_candidates(project: str, snapshots: list[Snapshot]) -> list[PinCandidate]:
    """The components of this project whose resources were last changed by hand.

    ``snapshots`` is newest first, as git log hands them over. The newest snapshot is the
    state as it stands now: a component that already carries a ``manual`` entry there needs
    nothing, and a component that no longer exists is not considered either.
    """
    if not snapshots:
        return []

    handler = ProjectFileHandler()
    head = snapshots[0]
    candidates: list[PinCandidate] = []

    for comp in head.data.get("components") or []:
        if not isinstance(comp, dict) or not comp.get("name"):
            continue
        component = str(comp["name"])
        if handler.get_component_resource_pins(head.data, component) is not None:
            continue
        candidate = _last_hand_change(project, component, snapshots, handler)
        if candidate is not None:
            candidates.append(candidate)
    return candidates


def _last_hand_change(
    project: str,
    component: str,
    snapshots: list[Snapshot],
    handler: ProjectFileHandler,
) -> PinCandidate | None:
    """Walk back until a commit changed this component's resources by hand."""
    for newer, older in pairwise(snapshots):
        after = _declared_resources(handler, newer.data, component)
        before = _declared_resources(handler, older.data, component)
        if after is None or before is None:
            # The component is born here; what it was created with is not a wish.
            return None
        changed = tuple(sorted(key for key, value in after.items() if before.get(key) != value))
        if not changed:
            continue
        if is_platform_commit(newer, older, component):
            continue
        return PinCandidate(
            project=project,
            component=component,
            fields=changed,
            ref=newer.ref,
            timestamp=newer.timestamp,
            message=newer.message,
        )
    return None


async def _snapshots(connector: GitConnector, relative_path: str, max_commits: int) -> list[Snapshot]:
    """The revisions of one project file, newest first, parsed."""
    snapshots: list[Snapshot] = []
    for revision in await connector.list_file_revisions(relative_path, limit=max_commits):
        content = await connector.show_file_at(revision["ref"], relative_path)
        if content is None:
            continue
        data = load_yaml_round_trip(content)
        if not isinstance(data, dict):
            continue
        snapshots.append(
            Snapshot(
                ref=revision["ref"],
                timestamp=revision["timestamp"],
                message=revision["message"],
                data=data,
            )
        )
    return snapshots


def _report(candidates: list[PinCandidate]) -> None:
    if not candidates:
        print("Geen component gevonden waarvan de resources met de hand zijn gezet.")
        return
    print(f"{len(candidates)} component(en) met een handmatig gezette resourcewaarde:\n")
    for candidate in candidates:
        print(f"  {candidate.project} / {candidate.component}")
        print(f"      velden : {', '.join(candidate.fields)}")
        print(f"      commit : {candidate.ref[:12]} ({candidate.timestamp}) {candidate.message}")


async def run(write: bool, max_commits: int) -> int:
    """Collect the candidates over every project file, and write them when asked."""
    handler = ProjectFileHandler()
    connector = await create_git_connector_for_project_files("resource-pin-migration", full_history=True)
    try:
        await connector.ensure_repo_cloned()
        filenames = await _project_filenames(connector)
        candidates: list[PinCandidate] = []
        for filename in filenames:
            relative_path = f"projects/{filename}"
            snapshots = await _snapshots(connector, relative_path, max_commits)
            project = str((snapshots[0].data.get("name") if snapshots else None) or filename.removesuffix(".yaml"))
            candidates.extend(find_pin_candidates(project, snapshots))

        _report(candidates)
        if not write or not candidates:
            return 0

        for candidate in candidates:
            relative_path = _path_of(candidate.project, filenames)
            content = await connector.read_file(relative_path)
            data = load_yaml_round_trip(content)
            pinned = handler.pin_resource_fields(
                data,
                candidate.component,
                candidate.fields,
                reason=(
                    f"Migration RC-518: set by hand in commit {candidate.ref[:12]} "
                    f"({candidate.timestamp}), before a wish was recorded"
                ),
            )
            if pinned:
                await connector.create_or_update_file(
                    relative_path, dump_yaml_to_string(data), do_commit_and_push=False
                )
        await connector.commit_and_push("migration: record the resource values that were set by hand (RC-518)")
        print(f"\n{len(candidates)} vastzetting(en) vastgelegd en gepusht.")
    finally:
        await connector.close()
    return 0


async def _project_filenames(connector: GitConnector) -> list[str]:
    """The project files in the repository, by name."""
    working_dir = await connector.get_working_dir()
    projects_dir = os.path.join(working_dir, "projects")
    if not os.path.isdir(projects_dir):
        return []
    return sorted(name for name in os.listdir(projects_dir) if name.endswith((".yaml", ".yml")))


def _path_of(project: str, filenames: list[str]) -> str:
    """The repository path of a project's file; the canonical name unless a legacy one exists."""
    for filename in filenames:
        if filename.removesuffix(".yaml").removesuffix(".yml") == project:
            return f"projects/{filename}"
    return f"projects/{project}.yaml"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--write", action="store_true", help="leg de vastzettingen vast (standaard: alleen tonen)")
    parser.add_argument("--max-commits", type=int, default=DEFAULT_MAX_COMMITS, help="hoe ver de walk terugkijkt")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    print(f"Projectenrepo: {settings.GIT_PROJECTS_SERVER_URL}")
    return asyncio.run(run(write=args.write, max_commits=args.max_commits))


if __name__ == "__main__":
    sys.exit(main())
