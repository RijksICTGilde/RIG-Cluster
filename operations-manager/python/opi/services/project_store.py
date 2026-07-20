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
import copy
import logging
import os
import shutil
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from opi.connectors.git import GitConnector, GitPushConflictError, create_git_connector_for_project_files
from opi.core.config import settings
from opi.core.project_schema import (
    ProjectIntegrityError,
    ProjectSchemaError,
    find_plaintext_secret_violations,
    validate_project_schema,
)
from opi.manager.project_validation import validate_project_structure
from opi.services.project_service import Project, ProjectUser, get_project_service
from opi.services.schema_migration import migrate_to_latest
from opi.services.user_service import get_user_service
from opi.utils.age import decrypt_age_content, get_decoded_project_private_key
from opi.utils.yaml_util import dump_yaml_to_string, load_yaml_from_string

logger = logging.getLogger(__name__)

PROJECTS_SUBDIR = "projects"
MAX_MUTATION_ATTEMPTS = 5
AGE_HEADER = "-----BEGIN AGE ENCRYPTED FILE-----"


class ProjectStoreError(RuntimeError):
    """Base error for project store operations."""


class ConcurrencyError(ProjectStoreError):
    """A mutation could not be applied within the allowed number of attempts.

    Bounded and surfaced rather than retried forever or silently dropped.
    """


class ProjectNotFoundError(ProjectStoreError):
    """The requested project does not exist in the store."""


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
    def get(self, name: str) -> Project | None: ...

    @abstractmethod
    def get_all(self) -> list[Project]: ...

    @abstractmethod
    def get_by_api_key(self, api_key: str) -> Project | None: ...

    @abstractmethod
    def exists(self, name: str) -> bool: ...

    @abstractmethod
    async def get_decrypted(self, name: str) -> dict[str, Any] | None: ...

    # ---- mutations: serialized read-modify-write, validated before persist ----

    @abstractmethod
    async def create(self, name: str, data: dict[str, Any], *, message: str, actor: str) -> Project: ...

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

    def get(self, name: str) -> Project | None:
        return get_project_service().get_project(name)

    def get_all(self) -> list[Project]:
        return list(get_project_service().get_all_projects().values())

    def get_by_api_key(self, api_key: str) -> Project | None:
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

    async def create(self, name: str, data: dict[str, Any], *, message: str, actor: str) -> Project:
        """Create a new project file. Fails if the project already exists."""
        async with self._lock:
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
                    await connector.reset_to_remote()
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
    ) -> MutationResult:
        """Persist a pre-built project dict (create or update).

        This is the compatibility path for callers that still build the full
        dict themselves before saving. It gains the store's lock, validation,
        rollback and write-through cache, but NOT the read-modify-write
        guarantee: the dict was built from state read earlier, so two concurrent
        savers can still lose an update *on this file*. Prefer ``mutate`` for
        anything that reads-then-writes -- it re-reads under the lock and re-applies.

        On a push conflict the same dict is re-published on top of whatever landed. That
        is last-writer-wins for this project file, but it can no longer clobber *other*
        projects: the commit only ever contains this one path.
        """
        async with self._lock:
            connector = await self.get_connector()
            relative_path = self._relative_path(name, filename)
            last_error: Exception | None = None

            await self._validate(data, enforce=enforce_validation)

            for attempt in range(MAX_MUTATION_ATTEMPTS):
                before = await self._read_committed(connector, relative_path)
                try:
                    ref = await self._persist(connector, relative_path, data, message, actor)
                except GitPushConflictError as e:
                    last_error = e
                    logger.warning(
                        "Push conflict on save '%s' (attempt %d/%d); re-syncing and retrying",
                        message,
                        attempt + 1,
                        MAX_MUTATION_ATTEMPTS,
                    )
                    await connector.reset_to_remote()
                    continue

                landed = await self._read_committed(connector, relative_path) or data
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
        async with self._lock:
            connector = await self.get_connector()
            relative_path = self._relative_path(name, filename)
            last_error: Exception | None = None

            for attempt in range(MAX_MUTATION_ATTEMPTS):
                current = await self._read_committed(connector, relative_path)
                if current is None:
                    raise ProjectNotFoundError(f"Project '{name}' does not exist")

                mutated = change(copy.deepcopy(current))
                if asyncio.iscoroutine(mutated):
                    mutated = await mutated

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
                    await connector.reset_to_remote()
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
        async with self._lock:
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
                    await connector.reset_to_remote()
            else:
                raise ConcurrencyError(
                    f"Could not delete project '{name}' after {MAX_MUTATION_ATTEMPTS} attempts: {last_error}"
                ) from last_error

        get_project_service().remove_project(name)
        logger.info("Deleted project '%s' (actor=%s)", name, actor)

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
        logger.debug("Persisting %s for actor=%s: %s", relative_path, actor, message)
        return await self._commit_and_publish(connector, {relative_path: dump_yaml_to_string(data)}, message)

    async def _commit_and_publish(self, connector: GitConnector, changes: dict[str, str | None], message: str) -> str:
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
            The published commit ref, or the unchanged HEAD when there was nothing to commit.
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
            await connector.reset_to_remote()

        old_head = await connector.get_local_commit_hash()

        commit = await connector.build_commit(changes, message)
        if commit is None:
            logger.debug("Nothing to commit for %s", list(changes))
            return old_head

        await connector.set_branch_ref(commit)
        try:
            # allow_rebase=False: divergence must reach the caller so it can re-read,
            # re-apply and re-validate. A silent rebase would publish an unvalidated merge.
            await connector.push_changes(allow_rebase=False)
        except BaseException:
            await connector.set_branch_ref(old_head)
            raise

        # The checkout is stale now that the ref moved; keep the warm copy mirroring HEAD.
        await connector.sync_worktree_to_head()
        return commit

    def _refresh_cache(self, name: str, data: dict[str, Any], filename: str) -> Project:
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
            project = service.get_project(name) or Project(
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

    async def reconcile(self) -> None:
        """Pull external edits into the cache, incrementally.

        Fetches, diffs the old and new HEAD for changed paths, and re-reads only
        the project files that actually changed. Replaces the old 30-second
        full re-clone. Because ZAD's own writes are write-through, this usually
        finds nothing.
        """
        async with self._lock:
            connector = await self.get_connector()
            old_head = await connector.get_local_commit_hash()
            await connector.reset_to_remote()
            new_head = await connector.get_local_commit_hash()

            if old_head == new_head:
                logger.debug("Reconcile: no new commits")
                return

            changed = await connector.list_changed_files(old_head, new_head)
            project_files = [
                p for p in changed if p.startswith(f"{PROJECTS_SUBDIR}/") and p.endswith((".yaml", ".yml"))
            ]
            logger.info("Reconcile: %d changed project file(s) between %s..%s", len(project_files), old_head, new_head)

            for relative_path in project_files:
                data = await self._read_committed(connector, relative_path)
                filename = os.path.basename(relative_path)
                if data is None:
                    # Deleted externally: evict by filename.
                    removed = [p.name for p in self.get_all() if p.filename == filename]
                    for name in removed:
                        get_project_service().remove_project(name)
                        logger.info("Reconcile: evicted externally deleted project '%s'", name)
                    continue
                get_project_service().load_project_from_data(data, filename)
                logger.info("Reconcile: reloaded '%s'", filename)

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

            loaded: dict[str, Project] = {}
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

            logger.info("ProjectStore bootstrap loaded %d project(s)", len(loaded))


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


__all__ = [
    "ConcurrencyError",
    "GitProjectStore",
    "MutationResult",
    "ProjectNotFoundError",
    "ProjectStore",
    "ProjectStoreError",
    "Revision",
    "get_project_store",
    "reset_project_store",
]
