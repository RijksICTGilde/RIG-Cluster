"""ProjectStore: the single authority for reading and writing project files.

Why this exists
---------------
Project definitions live as YAML in the ``zad-projects`` git repo. Before this
module every mutation built a fresh ProjectManager, which cloned the whole repo
to read authoritative state and cloned it again to write, so a single edit cost
about two full-history clones plus a push, and that cost grew with the repo's
history. There was also no write lock: concurrency rested entirely on git's
fetch+rebase+retry, and a rebase merges *text* without re-validating the merged
*semantics*, leaving a lost-update window on the same file.

The store fixes both:

* **Reads** are served from an in-memory parsed cache. No I/O.
* **Writes** go through one warm, long-lived working copy (cloned once per
  process, never rmtree'd) and are serialized by a per-repo asyncio.Lock, so
  same-project mutations queue instead of racing. Each waiter re-reads the
  previous winner's result and applies on top of it.
* **Mutations are functions**, not pre-built dicts. ``mutate`` re-reads current
  state, applies the change, validates the FINAL state, then commits and pushes.
  That removes the read-early/write-late TOCTOU window that causes lost updates.
* **Cross-process contention** (another cluster, or a human editing the repo)
  is handled optimistically: a non-fast-forward push means "HEAD moved", so we
  reset to the remote, re-read, re-apply the change function, re-validate and
  retry, bounded. A true semantic conflict surfaces as a validation error rather
  than a silent bad text merge.

Backend-agnostic on purpose: nothing git-specific leaks through the interface
(``ref`` is an opaque string, not a commit SHA), so a DatabaseProjectStore could
replace GitProjectStore without touching a single caller.

Scope: this owns ``zad-projects`` ONLY. The deployments and argo-applications
repos keep using their own ProjectManager git connectors.

Single-replica assumption: the asyncio.Lock serializes writers *in this
process*. OPI runs one replica per cluster today. Multi-replica would swap this
lock for a Postgres advisory lock or a single-writer leader; the interface does
not change.
"""

import asyncio
import contextlib
import copy
import inspect
import logging
import os
import re
import shutil
import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from deepdiff import DeepDiff, Delta
from deepdiff.delta import DeltaError
from deepdiff.path import extract

from opi.connectors.git import GitConnector, GitPushConflictError, create_git_connector_for_project_files
from opi.core.config import settings
from opi.core.project_schema import (
    ProjectIntegrityError,
    ProjectSchemaError,
    find_plaintext_secret_violations,
    validate_project_schema,
)
from opi.manager.project_validation import validate_project_structure
from opi.services.project_service import ProjectSummary, ProjectUser, get_project_service
from opi.services.schema_migration import migrate_to_latest
from opi.services.user_service import get_user_service
from opi.utils.age import decrypt_age_content, get_decoded_project_private_key
from opi.utils.yaml_util import dump_yaml_to_string, load_yaml_from_string

logger = logging.getLogger(__name__)

PROJECTS_SUBDIR = "projects"
MAX_MUTATION_ATTEMPTS = 5

# A single process-wide lock serializes every project-file write, so one slow
# write delays the next. Above these thresholds the timing is logged at WARNING
# rather than DEBUG/INFO, so an operator sees contention without turning on debug
# logging. Tune from what the logs actually show.
_LOCK_WAIT_WARN_SECONDS = 2.0
_PERSIST_WARN_SECONDS = 3.0
AGE_HEADER = "-----BEGIN AGE ENCRYPTED FILE-----"

# Version tokens travel to the browser and back, so they are validated before they
# reach git: a blob SHA is exactly 40 hex characters, anything else is not asked for.
_BLOB_SHA_RE = re.compile(r"[0-9a-f]{40}")


class ProjectStoreError(RuntimeError):
    """Base error for project store operations."""


class ConcurrencyError(ProjectStoreError):
    """A mutation could not be applied within the allowed number of attempts.

    Bounded and surfaced rather than retried forever or silently dropped.
    """


class ProjectNotFoundError(ProjectStoreError):
    """The requested project does not exist in the store."""


class ConflictError(ProjectStoreError):
    """The project file changed after the caller read it, and the two changes
    could not be combined automatically.

    Retryable: the caller should re-read and re-apply. Raised instead of silently
    publishing the caller's version, which would drop the other writer's change.
    """


@dataclass(frozen=True)
class Revision:
    """One historical version of a project file. Backend-neutral."""

    ref: str
    message: str
    author: str
    timestamp: str


@dataclass(frozen=True)
class MutationResult:
    """Outcome of a mutation.

    Carries ``before`` and ``after`` so downstream reconcile logic gets the diff
    handed to it instead of reconstructing it from history, and ``ref`` so the
    caller can record a checkpoint for the change-detection baseline.

    ``committed`` is False for an idempotent no-op: a change function may return
    None to say "already applied", which succeeds without producing a commit.
    """

    before: dict[str, Any] | None
    after: dict[str, Any]
    ref: str
    committed: bool = True


# A change function receives the freshest project data and returns the mutated
# dict, or None to signal "already applied" (idempotent no-op). Both sync and
# async functions are accepted so existing mutators wrap without rewriting.
ChangeFunction = Callable[[dict[str, Any]], dict[str, Any] | None | Awaitable[dict[str, Any] | None]]


class ProjectStore(ABC):
    """Single authority for reading and writing project files.

    Locking is deliberately NOT on this interface: ``mutate`` is the atomic
    unit. A multi-step read-decide-write is expressed as one change function
    inside one ``mutate`` call.
    """

    # ---- reads: served from the in-memory cache, no I/O ----

    @abstractmethod
    def get(self, name: str) -> ProjectSummary | None: ...

    @abstractmethod
    def get_all(self) -> list[ProjectSummary]: ...

    @abstractmethod
    def get_by_api_key(self, api_key: str) -> ProjectSummary | None: ...

    @abstractmethod
    def exists(self, name: str) -> bool: ...

    @abstractmethod
    async def get_decrypted(self, name: str) -> dict[str, Any] | None: ...

    # ---- mutations: serialized read-modify-write, validated before persist ----

    @abstractmethod
    async def create(self, name: str, data: dict[str, Any], *, message: str, actor: str) -> ProjectSummary: ...

    @abstractmethod
    async def mutate(self, name: str, change: ChangeFunction, *, message: str, actor: str) -> MutationResult: ...

    @abstractmethod
    async def delete(self, name: str, *, message: str, actor: str) -> None: ...

    # ---- audit / history ----

    @abstractmethod
    async def history(self, name: str) -> list[Revision]: ...

    @abstractmethod
    async def read_at(self, name: str, ref: str) -> dict[str, Any] | None: ...

    @abstractmethod
    async def previous(self, name: str) -> dict[str, Any] | None: ...

    # ---- freshness / lifecycle ----

    @abstractmethod
    async def reconcile(self) -> None: ...

    @abstractmethod
    async def bootstrap(self) -> None: ...

    @abstractmethod
    def cache_head(self) -> str | None: ...


class GitProjectStore(ProjectStore):
    """ProjectStore backed by the ``zad-projects`` git repository.

    Holds one warm working copy and one lock for the whole process. Reads come
    from the in-memory cache (backed by ProjectService, which remains the single
    cache instance the rest of the app already reads from).
    """

    def __init__(self, working_dir: str | None = None) -> None:
        self._lock = asyncio.Lock()
        self._connector: GitConnector | None = None
        self._working_dir = working_dir or os.path.join(settings.TEMP_DIR, "zad-projects-warm")
        # The commit the cache is known to fully reflect. Set only where the whole
        # cache is loaded consistently: bootstrap and reconcile. It is the backstop
        # against cache/disk drift -- reconcile reloads everything changed between
        # this and the current tree, so a path that advances the disk without the
        # cache is caught within one reconcile tick instead of surviving to a restart.
        # Kept as a lower bound on purpose: if it lags, reconcile re-reads a few files
        # redundantly (safe); it is never allowed to run ahead of what the cache holds.
        self._cache_head: str | None = None

    # ------------------------------------------------------------------
    # warm working copy
    # ------------------------------------------------------------------

    async def get_connector(self) -> GitConnector:
        """Return the warm, long-lived connector, cloning once if needed.

        The clone happens once per process instead of twice per request. The
        directory is recreated on first use so a leftover clone from a previous
        process cannot confuse GitConnector.ensure_repo_cloned (which clones
        into an empty directory).

        This connector must never be closed: GitConnector.close() rmtree's the
        working directory. Callers that receive it (ProjectManager) treat it as
        not-owned and therefore never close it.
        """
        if self._connector is not None:
            return self._connector

        if os.path.exists(self._working_dir):
            shutil.rmtree(self._working_dir, ignore_errors=True)
        os.makedirs(self._working_dir, exist_ok=True)

        connector = await create_git_connector_for_project_files(
            "project-store", full_history=True, working_dir=self._working_dir
        )
        await connector.ensure_repo_cloned()
        self._connector = connector
        logger.info("ProjectStore warm working copy ready at %s", self._working_dir)
        return connector

    @contextlib.asynccontextmanager
    async def _locked(self, operation: str, name: str):
        """Acquire the store lock, timing the wait and the hold.

        The single process-wide lock serializes every project-file write, so its
        wait time is the queueing delay one writer imposes on the next. Logging
        wait and hold separately tells "the store is busy" (long waits) apart from
        "one write is slow" (long holds) - which have different fixes. A slow hold
        is dominated by the git push; see _persist for the push timing itself.
        """
        requested = time.monotonic()
        await self._lock.acquire()
        waited = time.monotonic() - requested
        held_start = time.monotonic()
        log = logger.warning if waited >= _LOCK_WAIT_WARN_SECONDS else logger.debug
        log("store-lock %s '%s': waited %.2fs for the lock", operation, name, waited)
        try:
            yield
        finally:
            held = time.monotonic() - held_start
            log_held = logger.warning if held >= _PERSIST_WARN_SECONDS else logger.info
            log_held(
                "store-lock %s '%s': held %.2fs (waited %.2fs)",
                operation,
                name,
                held,
                waited,
            )
            self._lock.release()

    def _relative_path(self, name: str, filename: str | None = None) -> str:
        """Repo-relative path of a project file.

        An explicit ``filename`` from the caller wins (it already knows which
        file it opened), then the filename recorded in the cache so a project
        stored under a legacy filename keeps resolving, then the canonical form.
        """
        if filename:
            return f"{PROJECTS_SUBDIR}/{os.path.basename(filename)}"
        cached = get_project_service().get_project(name)
        if cached and cached.filename:
            return f"{PROJECTS_SUBDIR}/{os.path.basename(cached.filename)}"
        return f"{PROJECTS_SUBDIR}/{name}.yaml"

    async def _absolute_path(self, name: str) -> str:
        connector = await self.get_connector()
        working_dir = await connector.get_working_dir()
        return os.path.join(working_dir, self._relative_path(name))

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------

    def get(self, name: str) -> ProjectSummary | None:
        return get_project_service().get_project(name)

    def get_all(self) -> list[ProjectSummary]:
        return list(get_project_service().get_all_projects().values())

    def get_by_api_key(self, api_key: str) -> ProjectSummary | None:
        return get_project_service().get_project_by_api_key(api_key)

    def exists(self, name: str) -> bool:
        return get_project_service().get_project(name) is not None

    async def get_decrypted(self, name: str) -> dict[str, Any] | None:
        """Return a throwaway deep copy with AGE-encrypted values decrypted.

        For display/edit only. Derived state -- it must never be written back.
        """
        project = self.get(name)
        if project is None or project.data is None:
            return None

        view = copy.deepcopy(project.data)
        try:
            private_key = await get_decoded_project_private_key(view)
        except (ValueError, KeyError) as e:
            logger.warning("Could not get project private key for decrypted view of '%s': %s", name, e)
            return view

        if not private_key:
            return view

        await self._decrypt_tree(view, private_key)
        return view

    async def _decrypt_tree(self, data: Any, private_key: str) -> None:
        """Recursively decrypt AGE-encrypted string values in place."""
        if isinstance(data, dict):
            for key in list(data.keys()):
                value = data[key]
                if isinstance(value, str) and AGE_HEADER in value:
                    decrypted = await self._try_decrypt(value, private_key, key)
                    if decrypted is not None:
                        data[key] = decrypted
                elif isinstance(value, dict | list):
                    await self._decrypt_tree(value, private_key)
        elif isinstance(data, list):
            for index, item in enumerate(data):
                if isinstance(item, str) and AGE_HEADER in item:
                    decrypted = await self._try_decrypt(item, private_key, str(index))
                    if decrypted is not None:
                        data[index] = decrypted
                elif isinstance(item, dict | list):
                    await self._decrypt_tree(item, private_key)

    @staticmethod
    async def _try_decrypt(value: str, private_key: str, field: str) -> str | None:
        try:
            return await decrypt_age_content(value, private_key)
        except ValueError, RuntimeError:
            logger.debug("Failed to decrypt field '%s', leaving as-is", field)
            return None

    # ------------------------------------------------------------------
    # mutations
    # ------------------------------------------------------------------

    async def create(self, name: str, data: dict[str, Any], *, message: str, actor: str) -> ProjectSummary:
        """Create a new project file. Fails if the project already exists."""
        async with self._locked("create", name):
            connector = await self.get_connector()
            relative_path = f"{PROJECTS_SUBDIR}/{os.path.basename(name)}.yaml"
            last_error: Exception | None = None

            await self._validate(data, enforce=True)

            for attempt in range(MAX_MUTATION_ATTEMPTS):
                # Re-checked every attempt: a competing create may have landed remotely
                # while we were pushing.
                if await self._read_committed(connector, relative_path) is not None:
                    raise ProjectStoreError(f"Project '{name}' already exists")
                try:
                    await self._persist(connector, relative_path, data, message, actor)
                    break
                except GitPushConflictError as e:
                    last_error = e
                    logger.warning(
                        "Push conflict on create of '%s' (attempt %d/%d); re-syncing and retrying",
                        name,
                        attempt + 1,
                        MAX_MUTATION_ATTEMPTS,
                    )
                    await self._resync_after_conflict(connector)
            else:
                raise ConcurrencyError(
                    f"Could not create project '{name}' after {MAX_MUTATION_ATTEMPTS} attempts: {last_error}"
                ) from last_error

        project = self._refresh_cache(name, data, os.path.basename(relative_path))
        logger.info("Created project '%s' (actor=%s)", name, actor)
        return project

    async def save(
        self,
        name: str,
        data: dict[str, Any],
        *,
        message: str,
        actor: str,
        enforce_validation: bool = True,
        filename: str | None = None,
        refresh_cache: bool = True,
        base: dict[str, Any] | None = None,
    ) -> MutationResult:
        """Persist a pre-built project dict (create or update).

        This is the compatibility path for callers that build the full dict
        themselves before saving. It gains the store's lock, validation, rollback
        and write-through cache.

        ``base`` is the state the caller read before building ``data``. Pass it and
        the save becomes a compare-and-swap: if the committed file no longer matches
        ``base``, someone wrote in between, and silently publishing ``data`` would
        drop their change. The store then three-way merges and re-validates, and
        raises ConflictError when that cannot be resolved -- a visible failure
        instead of a silent lost update.

        Without ``base`` the behaviour is the old last-writer-wins, kept so call
        sites can adopt this incrementally. It still cannot clobber *other* projects:
        the commit only ever contains this one path.

        Note this is still weaker than ``mutate``, which re-runs the caller's change
        function on fresh state and therefore resolves the case a text merge cannot:
        two edits to the same YAML list, e.g. both adding a component.
        """
        async with self._locked("save", name):
            connector = await self.get_connector()
            relative_path = self._relative_path(name, filename)
            last_error: Exception | None = None

            for attempt in range(MAX_MUTATION_ATTEMPTS):
                # Re-applied EVERY attempt, against the caller's original dict. The
                # lock only covers writers in this process; a change pushed straight
                # to git is invisible to the warm copy until the push is rejected and
                # reset_to_remote() fetches it. Merging once before the loop would
                # therefore republish the caller's version over whatever was just
                # fetched -- the same lost update, arriving by the other route.
                logger.info(
                    "store.save '%s' attempt %d/%d (msg=%r, has_base=%s)",
                    name,
                    attempt + 1,
                    MAX_MUTATION_ATTEMPTS,
                    message,
                    base is not None,
                )
                attempt_data = data
                if base is not None:
                    attempt_data = await self._reconcile_with_concurrent_write(
                        connector, relative_path, name=name, base=base, data=data, actor=actor
                    )

                await self._validate(attempt_data, enforce=enforce_validation)

                before = await self._read_committed(connector, relative_path)
                try:
                    ref = await self._persist(connector, relative_path, attempt_data, message, actor)
                except GitPushConflictError as e:
                    last_error = e
                    logger.warning(
                        "Push conflict on save '%s' (attempt %d/%d); re-syncing and re-applying",
                        message,
                        attempt + 1,
                        MAX_MUTATION_ATTEMPTS,
                    )
                    await self._resync_after_conflict(connector)
                    continue

                landed = await self._read_committed(connector, relative_path) or attempt_data
                if refresh_cache:
                    self._refresh_cache(name, landed, os.path.basename(relative_path))
                return MutationResult(before=before, after=landed, ref=ref)

            raise ConcurrencyError(
                f"Could not save project '{name}' after {MAX_MUTATION_ATTEMPTS} attempts: {last_error}"
            ) from last_error

    async def mutate(
        self,
        name: str,
        change: ChangeFunction,
        *,
        message: str,
        actor: str,
        enforce_validation: bool = True,
        filename: str | None = None,
    ) -> MutationResult:
        """Serialized read-modify-write of a single project file.

        Under the lock, for each attempt: read the freshest committed state,
        apply ``change`` to it, validate the FINAL result, write, commit and
        push. A non-fast-forward push means someone else's write landed first,
        so we reset to the remote and re-apply the change on top of what
        actually landed -- never a blind text rebase.

        The cache is updated only after a successful push, so readers never see
        a version that is not durable. On a push failure that cannot be
        resolved, the local commit is discarded (reset --hard) so the warm copy
        is left clean.
        """
        async with self._locked("mutate", name):
            connector = await self.get_connector()
            relative_path = self._relative_path(name, filename)
            last_error: Exception | None = None

            for attempt in range(MAX_MUTATION_ATTEMPTS):
                current = await self._read_committed(connector, relative_path)
                if current is None:
                    raise ProjectNotFoundError(f"Project '{name}' does not exist")

                # A ChangeFunction may be sync or async. Awaiting into a second name
                # keeps the awaited result's type visible; reassigning the same name
                # left it a union with the un-awaited Awaitable for everything below.
                changed = change(copy.deepcopy(current))
                mutated = await changed if inspect.isawaitable(changed) else changed

                if mutated is None:
                    # Idempotent no-op: the change is already applied.
                    logger.info("Project mutation already applied, nothing to commit: %s", message)
                    ref = await connector.get_local_commit_hash()
                    return MutationResult(before=current, after=current, ref=ref, committed=False)

                await self._validate(mutated, enforce=enforce_validation)

                try:
                    ref = await self._persist(connector, relative_path, mutated, message, actor)
                except GitPushConflictError as e:
                    last_error = e
                    # HEAD moved under us. Re-sync to whatever actually landed and re-apply
                    # the change function on top of it, so the next attempt validates the
                    # real combined state. Never a blind text merge: if the two changes are
                    # genuinely incompatible (say both add the same component), validation
                    # rejects it on the retry and the caller gets a clear domain error
                    # instead of an invalid file reaching git.
                    logger.warning(
                        "Push conflict on '%s' (attempt %d/%d); re-reading remote and re-applying",
                        message,
                        attempt + 1,
                        MAX_MUTATION_ATTEMPTS,
                    )
                    await self._resync_after_conflict(connector)
                    continue

                # Write-through under the lock, from what actually landed rather than from
                # our local dict, so the cache can never drift from git.
                landed = await self._read_committed(connector, relative_path)
                if landed is None:
                    landed = mutated
                self._refresh_cache(name, landed, os.path.basename(relative_path))
                return MutationResult(before=current, after=landed, ref=ref)

            raise ConcurrencyError(
                f"Could not apply mutation to project '{name}' after {MAX_MUTATION_ATTEMPTS} attempts: {last_error}"
            ) from last_error

    async def delete(self, name: str, *, message: str, actor: str) -> None:
        """Remove a project file and evict it from the cache.

        Goes through the same publish path as every other mutation, so a failed push
        rolls back cleanly. The previous implementation called ``git rm`` directly, which
        staged the removal in the shared index straight away: if the push then failed the
        deletion stayed staged, and the next commit for an unrelated project carried it to
        the remote -- silently deleting a project file under someone else's commit message.
        """
        async with self._locked("delete", name):
            connector = await self.get_connector()
            relative_path = self._relative_path(name)
            last_error: Exception | None = None

            for attempt in range(MAX_MUTATION_ATTEMPTS):
                if await self._read_committed(connector, relative_path) is None:
                    logger.info("Project '%s' already absent, nothing to delete", name)
                    break
                try:
                    await self._commit_and_publish(connector, {relative_path: None}, message)
                    break
                except GitPushConflictError as e:
                    last_error = e
                    logger.warning(
                        "Push conflict on delete of '%s' (attempt %d/%d); re-syncing and retrying",
                        name,
                        attempt + 1,
                        MAX_MUTATION_ATTEMPTS,
                    )
                    await self._resync_after_conflict(connector)
            else:
                raise ConcurrencyError(
                    f"Could not delete project '{name}' after {MAX_MUTATION_ATTEMPTS} attempts: {last_error}"
                ) from last_error

        get_project_service().remove_project(name)
        logger.info("Deleted project '%s' (actor=%s)", name, actor)

    async def _reconcile_with_concurrent_write(
        self,
        connector: GitConnector,
        relative_path: str,
        *,
        name: str,
        base: dict[str, Any],
        data: dict[str, Any],
        actor: str = "?",
    ) -> dict[str, Any]:
        """Compare-and-swap for a pre-built dict, with a three-way merge fallback.

        Called under the lock. ``base`` is what the caller read; if the committed
        file still matches it, nothing happened in between and ``data`` is published
        as-is. If it does not, another writer landed first and publishing ``data``
        would silently drop their work -- the exact lost update this exists to stop.

        The merge is the same one a git rebase would have produced, except the result
        is validated before it can be published. That ordering is the whole point:
        the old code rebased and pushed unvalidated text, which is how two valid
        edits became a project file with a duplicated component.

        Raises ConflictError when the merge conflicts or the merged result does not
        validate, so the caller can surface a retryable error instead of losing data.
        """
        current = await self._read_committed(connector, relative_path)
        if current is None or current == base:
            logger.info(
                "reconcile '%s' [%s]: current==base (no concurrent change since read) -> publishing as-is",
                name,
                actor,
            )
            return data

        # Our change IS the committed state already: a request handler saved it and a
        # follow-up re-writes the same content against the version it started from (the
        # modal edit hands its result to the deployment task, which saves it again).
        # There is nothing to merge, and re-applying a delta that is already applied
        # would be reported as a conflict.
        if current == data:
            logger.info(
                "reconcile '%s' [%s]: current==data (our change already committed by a prior save) -> no-op",
                name,
                actor,
            )
            return data

        logger.info(
            "reconcile '%s' [%s]: file changed since read (current!=base, current!=data) -> three-way merging",
            name,
            actor,
        )

        merged = _apply_our_change_to(base=base, ours=data, theirs=current)
        if merged is None:
            raise ConflictError(
                f"Project '{name}' is gewijzigd sinds je begon met bewerken, en die wijziging raakt "
                f"hetzelfde onderdeel als dat van jou. Haal de laatste versie op en voer je wijziging opnieuw uit."
            )

        # Fails closed: a merge is only accepted when the RESULT is valid, never on
        # the grounds that both inputs were.
        try:
            validate_project_schema(merged)
            await validate_project_structure(merged)
        except (ProjectSchemaError, ProjectIntegrityError) as e:
            raise ConflictError(
                f"Project '{name}' is gewijzigd sinds je begon met bewerken en de samengevoegde "
                f"versie is ongeldig ({e}). Haal de laatste versie op en probeer opnieuw."
            ) from e

        logger.info("Three-way merge for '%s' applied and validated; both changes preserved", name)
        return merged

    async def _validate(self, data: dict[str, Any], *, enforce: bool) -> None:
        """Schema + structural validation of the FINAL state, before any write.

        ``enforce=False`` is for trusted, narrow programmatic mutators (auto-tune,
        oom_watcher, restore, keycloak config): the same checks still run and any
        violation is logged, but pre-existing unrelated drift does not block a
        recovery write. It is not a less-validated path -- the checks are identical.
        """
        try:
            validate_project_schema(data)
            await validate_project_structure(data)
        except (ProjectSchemaError, ProjectIntegrityError) as e:
            if enforce:
                raise
            # enforce=False tolerates pre-existing drift, never a decrypted secret.
            # Writing back a get_decrypted() view would otherwise land plaintext
            # credentials in git through one of the 11 non-enforcing call sites.
            leaked = find_plaintext_secret_violations(data)
            if leaked:
                raise ProjectSchemaError(
                    f"Projectbestand '{data.get('name', '(onbekend)')}' is geweigerd: "
                    f"de volgende velden moeten AGE-versleuteld zijn maar bevatten platte tekst: "
                    f"{', '.join(leaked)}. Dit wordt ook met enforce_validation=False geweigerd."
                ) from e
            logger.warning(
                "Persisting project '%s' despite a validation failure (enforce_validation=False); "
                "the project file has pre-existing drift that should be repaired: %s",
                data.get("name", "(onbekend)"),
                e,
            )

    async def _persist(
        self, connector: GitConnector, relative_path: str, data: dict[str, Any], message: str, actor: str
    ) -> str:
        """Commit and push a single project file. Returns the resulting commit ref.

        The push is the durability ack: nothing is reflected in the cache until it
        succeeds.
        """
        started = time.monotonic()
        ref, committed = await self._commit_and_publish(connector, {relative_path: dump_yaml_to_string(data)}, message)
        elapsed = time.monotonic() - started

        # Say which of the two happened. A save whose result is byte-identical to what
        # is already committed produces no commit, which is correct but indistinguishable
        # in the log from a real write except by the absence of a store-push line next to
        # it. Callers save unconditionally (they cannot know whether one of the managers
        # mutated project_data in memory), so no-ops are normal and frequent -- naming
        # them, with the message that was attempted, is what makes that legible.
        outcome = f"committed {ref[:8]}" if committed else "no change"
        log = logger.warning if elapsed >= _PERSIST_WARN_SECONDS else logger.info
        log(
            "store-persist %s: %s in %.2fs (actor=%s) -- %s",
            relative_path,
            outcome,
            elapsed,
            actor,
            message,
        )
        return ref

    async def _commit_and_publish(
        self, connector: GitConnector, changes: dict[str, str | None], message: str
    ) -> tuple[str, bool]:
        """Build a commit for exactly these paths, publish it, and roll back cleanly on failure.

        Two properties matter here, and both come from building the commit from git objects
        instead of from the working tree:

        * **Only the named paths are committed.** The previous path wrote the file into the
          shared warm copy and ran ``git add -A``, which stages everything -- so one
          project's commit could carry another writer's half-written file, or a pending
          deletion, to the remote. Here the commit cannot contain anything we did not name.
        * **Rollback is exact.** Nothing is written to disk, so undoing a failed push is just
          moving the branch ref back. There is no half-applied state to reset away, and no
          window in which a concurrent reconcile could discard uncommitted work.

        Args:
            changes: repo-relative path -> new content, or None to delete the path
            message: commit message

        Returns:
            ``(ref, committed)``: the published commit ref and True, or the unchanged HEAD
            and False when the new content matched what was already committed. Callers use
            the flag to report which of the two happened; deriving it by re-reading HEAD
            would cost an extra git subprocess per save.
        """
        # Self-heal before building on top of local state. The rollback below is itself
        # an await, so a teardown that interrupts it (double cancel, loop shutdown) can
        # leave the branch on a commit that was never acknowledged by a push. Building
        # the next commit on that parent would publish it silently, under an unrelated
        # message. Local-only check, no network on the happy path.
        unpushed = await connector.count_unpushed_commits()
        if unpushed:
            logger.warning(
                "Warm copy has %d unpushed commit(s) -- discarding them before publishing. "
                "This means an earlier rollback did not complete.",
                unpushed,
            )
            # reset_to_remote() is `fetch` + `reset --hard origin/main`, so it does not
            # only discard the local-ahead commit: it also fast-forwards the disk over
            # every commit another writer pushed meanwhile. Those external projects are
            # now on disk but NOT in the cache, and reconcile's fast path (remote head
            # == local head) will then read "nothing to do" forever -- so the cache
            # served a project without its newest deployments until a restart. Reload
            # the diff into the cache, exactly as the push-conflict path does.
            before_reset = await connector.get_local_commit_hash()
            await connector.reset_to_remote()
            after_reset = await connector.get_local_commit_hash()
            if before_reset != after_reset:
                await self._reload_changed_into_cache(connector, before_reset, after_reset)

        old_head = await connector.get_local_commit_hash()

        commit = await connector.build_commit(changes, message)
        if commit is None:
            logger.debug("Nothing to commit for %s", list(changes))
            return old_head, False

        await connector.set_branch_ref(commit)
        try:
            # allow_rebase=False: divergence must reach the caller so it can re-read,
            # re-apply and re-validate. A silent rebase would publish an unvalidated merge.
            push_started = time.monotonic()
            await connector.push_changes(allow_rebase=False)
            logger.info("store-push %s: push took %.2fs", list(changes), time.monotonic() - push_started)
        except BaseException:
            await connector.set_branch_ref(old_head)
            raise

        # The checkout is stale now that the ref moved; keep the warm copy mirroring HEAD.
        await connector.sync_worktree_to_head()
        return commit, True

    def _refresh_cache(self, name: str, data: dict[str, Any], filename: str) -> ProjectSummary:
        """Write-through cache update. Called only after a successful push."""
        service = get_project_service()
        service.load_project_from_data(data, filename)
        project = service.get_project(name)
        if project is None:
            # load_project_from_data rejected the data (e.g. missing api-key).
            # The write is already durable, so register a minimal entry rather than
            # leaving readers with a stale version -- but carry the members over, or
            # every team member of this project silently loses portal access until
            # the next restart.
            users = _users_from_data(data)
            service.register(name, "", filename, users, data)
            project = service.get_project(name) or ProjectSummary(
                name=name, api_key="", filename=filename, users=users, data=data
            )
        return project

    async def _read_committed(
        self, connector: GitConnector, relative_path: str, *, ref: str = "HEAD"
    ) -> dict[str, Any] | None:
        """Read the committed state of a project file from the warm copy."""
        content = await connector.show_file_at(ref, relative_path)
        if content is None:
            return None
        data = load_yaml_from_string(content)
        if data is None:
            return None
        migrated, _ = migrate_to_latest(data)
        return migrated

    # ------------------------------------------------------------------
    # history / audit
    # ------------------------------------------------------------------

    async def history(self, name: str) -> list[Revision]:
        connector = await self.get_connector()
        raw = await connector.list_file_revisions(self._relative_path(name))
        return [Revision(ref=r["ref"], message=r["message"], author=r["author"], timestamp=r["timestamp"]) for r in raw]

    async def read_at(self, name: str, ref: str) -> dict[str, Any] | None:
        connector = await self.get_connector()
        return await self._read_committed(connector, self._relative_path(name), ref=ref)

    async def version_of(self, relative_path: str) -> str | None:
        """Version token for a project file: its git blob SHA at HEAD.

        Handed to a client that is about to build an edit from what it just read
        (a form, a wizard step). Sent back with the resulting write it becomes the
        compare-and-swap base, so an edit is applied as a *change* relative to the
        version the user actually saw, not as a wholesale overwrite of whatever is
        there by then. None when the file does not exist yet.
        """
        connector = await self.get_connector()
        return await connector.get_blob_sha(relative_path)

    async def read_version(self, version: str) -> dict[str, Any] | None:
        """Project data exactly as it was in the given ``version_of`` token.

        None when the token is malformed or its blob is not in the clone, so the
        caller falls back rather than merging against a guess.
        """
        if not _BLOB_SHA_RE.fullmatch(version):
            logger.warning("Rejected malformed project version token: %r", version)
            return None

        connector = await self.get_connector()
        content = await connector.read_blob(version)
        if content is None:
            return None
        data = load_yaml_from_string(content)
        if data is None:
            return None
        migrated, _ = migrate_to_latest(data)
        return migrated

    async def read_path(self, relative_path: str, ref: str = "HEAD") -> dict[str, Any] | None:
        """Read a project file by its repo-relative path (``projects/<file>.yaml``).

        For callers that hold a path rather than a project name -- notably
        ProjectManager, which is constructed from a file path and may run before the
        project exists in the cache. Same authoritative source and schema migration as
        ``read_at``; it exists so those callers never need a working directory of their
        own, which is what let them read and write around the store.

        At HEAD this is served from the in-memory cache. ``get_contents()`` has ~54 call
        sites, and going to git each time forks a subprocess per call (~5ms against
        ~0.1ms for the previous working-tree read) -- which would make the store's one
        remaining read path its slowest, right after the rest of the app was moved onto
        the instant cached one. The cache is the same write-through state ``get()``
        serves to every other reader, so this is no weaker a guarantee than the rest of
        the read path; a non-HEAD ref still goes to git.
        """
        if ref == "HEAD":
            cached = self._cached_by_filename(os.path.basename(relative_path))
            if cached is not None:
                return copy.deepcopy(cached)

        connector = await self.get_connector()
        return await self._read_committed(connector, relative_path, ref=ref)

    def _cached_by_filename(self, filename: str) -> dict[str, Any] | None:
        """Cached project data for a project file, by its basename.

        Returns None when the project is not cached, so the caller falls back to
        git -- ProjectManager can run before a project reaches the cache.
        """
        for project in self.get_all():
            if project.filename == filename and project.data is not None:
                return project.data
        return None

    async def previous(self, name: str) -> dict[str, Any] | None:
        """The last version of THIS file before HEAD.

        Resolved file-scoped (the last commit that touched this path), not
        HEAD~1: the projects repo is shared, so HEAD's parent usually belongs to
        a different project and would yield a bogus baseline.
        """
        connector = await self.get_connector()
        content = await connector.get_previous_file_content(self._relative_path(name))
        if content is None:
            return None
        data = load_yaml_from_string(content)
        if data is None:
            return None
        migrated, _ = migrate_to_latest(data)
        return migrated

    # ------------------------------------------------------------------
    # freshness / lifecycle
    # ------------------------------------------------------------------

    def cache_head(self) -> str | None:
        """Commit the cache was last loaded at, or None before the first load."""
        return self._cache_head

    async def reconcile(self) -> None:
        """Pull edits made outside ZAD into the cache, incrementally.

        Only needed for changes that did not come through the store: someone
        editing the repo by hand, or another cluster pushing. ZAD's own writes are
        write-through, so the cache is already correct for them.

        Asks the remote for its branch tip first (``ls-remote``, no object
        transfer, ~60ms). When it matches what we already have there is nothing to
        do, and the expensive part -- fetch, hard reset, re-read -- is skipped
        entirely. Otherwise it diffs the old and new HEAD and re-reads only the
        project files that actually changed.
        """
        connector = await self.get_connector()

        # Cheap check outside the lock: no divergence means no work, and no reason
        # to make readers queue behind a write that can hold the lock for seconds.
        # The cache is provably fresh only when it matches BOTH the remote (no external
        # commit waiting) AND its own last-loaded head (no silent disk advance). Gating
        # on remote == disk alone was the gap that let a self-heal reset move the disk
        # past the cache and never recover: reconcile read "nothing to do" while the
        # cache served a stale project until a restart.
        remote_head = await connector.get_remote_commit_hash()
        disk_head = await connector.get_local_commit_hash()
        if remote_head is not None and remote_head == disk_head == self._cache_head:
            logger.debug("Reconcile: remote, disk and cache all current, nothing to do")
            return

        async with self._locked("reconcile", "*"):
            disk_before = await connector.get_local_commit_hash()
            await connector.reset_to_remote()
            new_head = await connector.get_local_commit_hash()

            # Reload from what the CACHE last reflected, not from the disk's prior head:
            # if some path advanced the disk without the cache, only cache_head knows
            # how far back the cache actually is, and therefore what must be re-read.
            base = self._cache_head or new_head
            changed = await self._reload_changed_into_cache(connector, base, new_head) if base != new_head else []
            self._cache_head = new_head

            # A reset that moved the disk means external commits were waiting -- normal:
            # another cluster or a hand edit pushed, and pulling them in is this loop's
            # job. But a reload that CHANGED cached data while the disk did NOT move means
            # the disk already matched the remote, yet the cache was stale. That is a path
            # advancing the working copy without updating the cache -- the class of bug
            # this backstop exists to catch. It should not happen; log it loudly so the
            # path gets found, not just silently repaired.
            disk_moved = disk_before != new_head
            if changed and not disk_moved:
                logger.error(
                    "ProjectStore cache had drifted from git: %s were stale in the cache while the working "
                    "copy already matched the remote (%s). Some write path advanced the disk without updating "
                    "the cache. reconcile repaired it, but the path must be found -- this should not happen.",
                    changed,
                    new_head[:8],
                )
            elif changed:
                logger.info("Reconcile: pulled external change(s) into the cache: %s", changed)

    async def _resync_after_conflict(self, connector: GitConnector) -> None:
        """Fetch the remote after a rejected push, and take the whole tree with us.

        A non-fast-forward push means someone else's commit landed first. Resolving
        it fetches and hard-resets, which updates every project file, not only the
        one being written -- so this also loads any other project that changed into
        the cache. Answers "can we commit and end up not up to date?": no, provided
        the changes that arrived alongside are picked up, which is what this does.
        """
        # Callers are the push-conflict retry paths in create/save/mutate/delete,
        # which all hold the lock. Checked rather than assumed: this hard-resets the
        # shared working copy and rewrites cache entries, so running it concurrently
        # with a writer would reintroduce exactly the class of race the store exists
        # to remove.
        #
        # A real check, not an assert: `python -O` strips asserts, and a guard that
        # disappears under an optimisation flag is not a guard.
        if not self._lock.locked():
            raise ProjectStoreError("_resync_after_conflict must be called while holding the store lock")

        old_head = await connector.get_local_commit_hash()
        await connector.reset_to_remote()
        new_head = await connector.get_local_commit_hash()
        if old_head != new_head:
            await self._reload_changed_into_cache(connector, old_head, new_head)

    async def _reload_changed_into_cache(self, connector: GitConnector, old_head: str, new_head: str) -> list[str]:
        """Re-read the project files that changed between two commits into the cache.

        Shared by reconcile and by the push-conflict retry path. A rejected push is
        resolved with fetch + hard reset, which brings the WHOLE repo up to date, not
        just the file being written -- so any other project that changed comes along
        on disk. Without this the cache would keep serving the old version of those
        projects until someone triggered a reconcile, which since the removal of the
        30-second poll may be a long time. The fetch is already paid for here.

        Returns the filenames whose cached data ACTUALLY changed (not merely files that
        differ between the two commits). Write-through means the cache is often already
        current for a file that git shows as changed; only a genuine change is drift
        worth flagging, so reconcile uses this to tell its own writes from a real gap.
        """
        changed = await connector.list_changed_files(old_head, new_head)
        project_files = [p for p in changed if p.startswith(f"{PROJECTS_SUBDIR}/") and p.endswith((".yaml", ".yml"))]
        if not project_files:
            return []

        logger.info("Reloading %d changed project file(s) between %s..%s", len(project_files), old_head, new_head)

        really_changed: list[str] = []
        for relative_path in project_files:
            data = await self._read_committed(connector, relative_path)
            filename = os.path.basename(relative_path)
            if data is None:
                # Deleted externally: evict by filename.
                removed = [p.name for p in self.get_all() if p.filename == filename]
                for name in removed:
                    get_project_service().remove_project(name)
                    logger.info("Evicted externally deleted project '%s'", name)
                if removed:
                    really_changed.append(filename)
                continue
            before = self._cached_by_filename(filename)
            get_project_service().load_project_from_data(data, filename)
            if before != data:
                really_changed.append(filename)
                logger.info("Reloaded '%s' from git", filename)
        return really_changed

    async def _list_project_files(self, connector: GitConnector) -> list[str]:
        """Filenames of the project files present in the warm working copy."""
        working_dir = await connector.get_working_dir()
        projects_dir = os.path.join(working_dir, PROJECTS_SUBDIR)

        if not os.path.exists(projects_dir):
            logger.warning("Projects directory not found in warm copy: %s", projects_dir)
            return []

        return sorted(f for f in os.listdir(projects_dir) if f.endswith((".yaml", ".yml")))

    async def bootstrap(self) -> None:
        """Startup: clone the warm copy and load every project into the cache.

        Starts from the remote state (reset --hard origin) because only pushed
        writes were ever acknowledged, so discarding local-only state is safe.

        One pass, through the same parser every other load uses, so entries land
        fully formed -- api-key already decrypted. An earlier version registered
        the raw ciphertext and decrypted in a second pass outside the lock, which
        left a window where API-key authentication compared against ciphertext.
        """
        async with self._lock:
            connector = await self.get_connector()
            service = get_project_service()

            loaded: dict[str, ProjectSummary] = {}
            member_emails: list[str] = []
            for filename in await self._list_project_files(connector):
                data = await self._read_committed(connector, f"{PROJECTS_SUBDIR}/{filename}")
                if data is None:
                    logger.warning("Could not read project file %s", filename)
                    continue
                project = service.build_project_from_data(data, filename)
                if project is None:
                    logger.warning("Could not load project file %s into the cache", filename)
                    continue
                loaded[project.name] = project
                member_emails.extend(user.email for user in project.users or [])

            # Build the full set first, then swap, so concurrent readers never
            # observe an empty or half-populated cache.
            service.replace_all_projects(loaded)

            # replace_all_projects bypasses register(), which is what normally keeps
            # the access allowlist in sync, so feed it here.
            if member_emails:
                get_user_service().add_allowed_emails(member_emails)

            # The cache now reflects the tree at this commit; record it as the
            # freshness baseline reconcile checks against.
            self._cache_head = await connector.get_local_commit_hash()

            logger.info("ProjectStore bootstrap loaded %d project(s) at %s", len(loaded), (self._cache_head or "?")[:8])


def _apply_our_change_to(
    *, base: dict[str, Any], ours: dict[str, Any], theirs: dict[str, Any]
) -> dict[str, Any] | None:
    """Re-apply our change (base -> ours) on top of ``theirs``. None on conflict.

    This is the git model -- replay the change onto what is there now -- but on the
    parsed structure rather than on lines of text. That distinction is the whole
    reason this is not a git merge: measured, ``git merge-file``, ``cherry-pick`` and
    ``rebase`` all conflict when two writers each append a component, because the two
    additions land on adjacent lines. Structurally they do not collide at all, and
    both changes survive.

    Conservative by construction: for a field that already existed, the delta records
    the value the edit started from and refuses to apply when that value has changed
    underneath, so a same-field collision is reported rather than silently overwritten.

    A newly *added* key needs a separate check. deepdiff represents it as
    ``dictionary_item_added`` and applies it unconditionally -- it verifies the old
    value only for keys that were already there -- so two writers each adding the same
    previously-absent key (a config block, an alias, a component's first ``resources``)
    would silently resolve to ours. ``_conflicting_added_keys`` catches exactly that
    and turns it into a conflict.

    Known limitation: deltas address list entries by index, so a concurrent
    *removal* earlier in a list shifts the positions of later entries and is reported
    as a conflict even when the two edits are semantically unrelated. That fails
    closed -- an error, never a lost update -- and matching entries by their name key
    would refine it.
    """
    try:
        diff = DeepDiff(base, ours)

        collisions = _conflicting_added_keys(diff, ours=ours, theirs=theirs)
        if collisions:
            logger.info(
                "Both versions added the same field(s) with different values, treating as a conflict: %s",
                ", ".join(collisions),
            )
            return None

        # log_errors=False: a conflict is an expected outcome here, not a fault. Left on,
        # deepdiff logs it at ERROR and every ordinary concurrent edit would page the
        # log watch. The caller gets a ConflictError and logs it once, with context.
        delta = Delta(diff, raise_errors=True, bidirectional=True, log_errors=False)
        return copy.deepcopy(theirs) + delta
    except DeltaError as e:
        logger.info("Could not re-apply our change on top of the newer version: %s", e)
        return None
    except (TypeError, ValueError, KeyError, IndexError) as e:
        # A malformed or unrepresentable delta must not take the write down; treat it
        # as a conflict so the caller re-reads instead.
        logger.warning("Structural merge failed unexpectedly, treating as a conflict: %s", e)
        return None


def _conflicting_added_keys(diff: DeepDiff, *, ours: dict[str, Any], theirs: dict[str, Any]) -> list[str]:
    """Paths that BOTH sides added since ``base``, with different values.

    Exists because a delta applies ``dictionary_item_added`` without checking whether
    the key is already present in the target: bidirectional deltas verify the previous
    value only where there was one. Without this check, two writers each setting a
    previously-absent field resolve silently to whoever pushed second, which is the
    lost update the compare-and-swap exists to prevent.

    Only a *differing* value is a conflict. Both sides adding the same key with the
    same value is agreement, not collision, and must not block the write.
    """
    conflicts: list[str] = []
    for path in diff.get("dictionary_item_added", []):
        try:
            their_value = extract(theirs, path)
        except KeyError, IndexError, TypeError:
            # Not present on their side: only we added it, so there is nothing to lose.
            continue
        try:
            our_value = extract(ours, path)
        except KeyError, IndexError, TypeError:
            continue
        if their_value != our_value:
            conflicts.append(path)
    return sorted(conflicts)


def _users_from_data(data: dict[str, Any]) -> list[ProjectUser] | None:
    """Members declared in a project file, for the degraded _refresh_cache path."""
    users_data = data.get("users", [])
    if not isinstance(users_data, list):
        return None
    users = [
        ProjectUser(email=u["email"], role=u["role"])
        for u in users_data
        if isinstance(u, dict) and "email" in u and "role" in u
    ]
    return users or None


_store: GitProjectStore | None = None


def get_project_store() -> GitProjectStore:
    """Return the process-wide ProjectStore singleton for ``zad-projects``."""
    global _store
    if _store is None:
        _store = GitProjectStore()
    return _store


def reset_project_store() -> None:
    """Drop the singleton. For tests only."""
    global _store
    _store = None


_reconcile_poll_task: asyncio.Task[None] | None = None


async def _reconcile_poll_loop(interval_seconds: int) -> None:
    """Periodically pull out-of-band edits into the cache.

    ZAD's own writes are write-through, so this poll exists for changes made
    outside ZAD: a member removed or an invite key revoked by pushing straight to
    ``zad-projects`` must stop working within a bounded window, not only after an
    explicit refresh or a restart. reconcile() starts with an ls-remote check
    (~60ms, no object transfer), so an idle tick costs next to nothing.
    """
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await get_project_store().reconcile()
        except Exception as e:
            # Broad on purpose: this loop is the revocation bound. A transient
            # git/network error must not end it, or out-of-band revocations
            # silently stop propagating until restart.
            logger.error(f"Error in project-store reconcile poll: {e}")


def start_reconcile_poll() -> None:
    """Start the fallback reconcile poll. Safe to call multiple times."""
    global _reconcile_poll_task
    interval = settings.PROJECT_STORE_RECONCILE_INTERVAL_SECONDS
    if interval <= 0:
        logger.info("Project-store reconcile poll disabled (interval <= 0)")
        return
    if _reconcile_poll_task and not _reconcile_poll_task.done():
        return
    _reconcile_poll_task = asyncio.create_task(_reconcile_poll_loop(interval))
    logger.info(f"Started project-store reconcile poll (every {interval}s)")


def stop_reconcile_poll() -> None:
    """Stop the fallback reconcile poll."""
    global _reconcile_poll_task
    if _reconcile_poll_task and not _reconcile_poll_task.done():
        _reconcile_poll_task.cancel()
        logger.info("Stopped project-store reconcile poll")
    _reconcile_poll_task = None


__all__ = [
    "ConcurrencyError",
    "ConflictError",
    "GitProjectStore",
    "MutationResult",
    "ProjectNotFoundError",
    "ProjectStore",
    "ProjectStoreError",
    "Revision",
    "get_project_store",
    "reset_project_store",
    "start_reconcile_poll",
    "stop_reconcile_poll",
]
