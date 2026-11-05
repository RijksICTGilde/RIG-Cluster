"""Eén beheerde git-repo per remote: lezen uit objecten, schrijven zonder werkboom.

De deployments- en de argo-repo werden per taak gekloond en met een ``add -A`` gecommit.
Dat kostte per ronde drie klonen van een repo met tienduizenden commits, en het liet twee
taken op dezelfde werkboom werken. :class:`GitRepo` doet het zoals de ProjectStore het voor
``zad-projects`` doet: één bare blobless kopie per (remote, branch) die blijft staan, lezen
per revisie, en schrijven door een commit uit git-objecten te bouwen en die te pushen. De
remote weigert dan zelf een push die niet op de laatste stand is gebouwd, en dat is de
compare-and-swap waar de hele gelijktijdigheid op rust.

Zie ``features/git-repo.md``.
"""

import asyncio
import hashlib
import logging
import os
import shutil
import tarfile
import tempfile
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractContextManager, asynccontextmanager, nullcontext
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from opi.connectors.git import (
    EXEC_MODE,
    FILE_MODE,
    GIT_CLONE_TIMEOUT_SECONDS,
    GIT_PUSH_TIMEOUT_SECONDS,
    GIT_RETRY_BUDGET_SECONDS,
    SYMLINK_MODE,
    FileChange,
    GitCommandTimeoutError,
    GitPushConflictError,
    GitPushRejectedError,
    GitRemoteUnavailableError,
    build_tree_commit,
    credential_env,
    git_push_rejection_reason,
    git_stderr_detail,
    is_git_push_collision,
    project_write_lock,
    push_lock_for,
    repo_lock_key,
    run_git,
    run_git_bytes,
    strip_url_credentials,
)
from opi.core.config import settings
from opi.core.metrics import PHASE_GIT_PREPARE, PHASE_GIT_PUSH, rollout_phase
from opi.generation.manifests import CMP_ENV_PLAINTEXT_FILENAME
from opi.utils.age import decrypt_password_smart_auto_sync
from opi.utils.connect_retry import retry_on_connect_error
from opi.utils.naming import generate_argocd_project_folder_path, generate_infrastructure_argocd_folder_path
from opi.utils.sops import TO_SOPS_SUFFIX

if TYPE_CHECKING:
    from opi.core.actor import Actor

logger = logging.getLogger(__name__)

#: Waar de bare kopieen staan. Een map per (remote zonder credentials, branch).
GIT_REPOS_DIR = os.path.join(settings.TEMP_DIR, "git-repos")

#: Grens op schijfruimte, niet aantal: kopieen verschillen een orde in omvang.
#: TEMP_DIR is een emptyDir van 8Gi op productie; vol = OPI stil.
GIT_REPOS_MAX_BYTES = 1024 * 1024 * 1024

#: Hoe vaak een write opnieuw op de laatste stand wordt toegepast na een afgewezen push.
#: Dezelfde grens als ``MAX_MUTATION_ATTEMPTS`` van de ProjectStore.
MAX_WRITE_ATTEMPTS = 5

#: The environment for every git command that must stay off the network. The copy is
#: blobless, and git fetches a missing blob from the promisor remote by itself, without the
#: credentials that only clone, fetch and push carry: that broke every write on the sandbox.
#: With this set, a missing blob is an error at once instead of an unauthenticated request.
OFFLINE_ENV = {"GIT_NO_LAZY_FETCH": "1"}

#: Serialiseert het bijwerken van één kopie, en houdt hem tegen de opruiming vast.
_repo_locks: dict[str, asyncio.Lock] = {}

#: Hoeveel taken er op dit moment uit een kopie lezen of erin schrijven. Houdt hem tegen
#: de opruiming vast, ook buiten :meth:`GitRepo.tip` en :meth:`GitRepo.fetch`, waar de
#: lock niet vastzit.
_repo_users: dict[str, int] = {}

#: De kopieen van dit proces, per (remote, branch).
_repos: dict[str, GitRepo] = {}


class GitRepoConflictError(RuntimeError):
    """De remote heeft een pad binnen de scope anders gewijzigd dan wij het schrijven.

    De aanroeper moet zijn stap opnieuw doen op de nieuwe stand: materialiseren,
    genereren, schrijven. Een blinde text-merge zou een stand publiceren die niemand
    gevalideerd heeft.
    """


@dataclass(frozen=True)
class GitCredentials:
    """De toegang van de AANROEPER tot een remote. Nooit bij de repo bewaard."""

    username: str | None = None
    password: str | None = None

    def env(self, repo_url: str) -> dict[str, str]:
        return credential_env(repo_url, self.username, self.password)


@dataclass(frozen=True)
class WriteResult:
    """Wat er geland is: de commit, of er iets te committen was, en welke paden."""

    ref: str
    committed: bool
    paths: list[str] = field(default_factory=list)


def _repo_key(url: str, branch: str) -> str:
    return f"{repo_lock_key(url)}#{branch}"


def _repo_lock(key: str) -> asyncio.Lock:
    return _repo_locks.setdefault(key, asyncio.Lock())


def get_git_repo(url: str, branch: str = "main") -> GitRepo:
    """De beheerde kopie van deze (remote, branch), procesbreed en lui aangemaakt.

    De sleutel is de URL ZONDER credentials: alle projecten op dezelfde remote delen één
    kopie. Dat mag omdat er maar één partij toegang heeft tot deze repo's; zie
    :meth:`GitRepo.resolve`.
    """
    key = _repo_key(url, branch)
    repo = _repos.get(key)
    if repo is None:
        repo = GitRepo(url, branch)
        _repos[key] = repo
    return repo


def reset_git_repos() -> None:
    """Vergeet de kopieen van dit proces. Voor tests."""
    _repos.clear()
    _repo_locks.clear()
    _repo_users.clear()


def _directory_size(path: str) -> int:
    total = 0
    for root, _, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                continue
    return total


def _prune_repos(keep: str) -> None:
    """Ruim de minst recent gebruikte kopieen op tot het geheel onder de grens past.

    Een kopie die in gebruik is blijft staan: daar leest of schrijft een taak uit, en die
    mag niet onder een lopende ``git archive`` of commit wegvallen.
    ``keep`` is de kopie die de aanroeper zelf gaat gebruiken.
    """
    busy = (
        {_repo_path_for(key) for key, lock in _repo_locks.items() if lock.locked()}
        | {_repo_path_for(key) for key, users in _repo_users.items() if users}
        | {keep}
    )
    try:
        entries = [
            (path, _directory_size(path), os.path.getmtime(path))
            for name in os.listdir(GIT_REPOS_DIR)
            if os.path.isdir(path := os.path.join(GIT_REPOS_DIR, name))
        ]
    except OSError:
        return

    total = sum(size for _, size, _ in entries)
    for path, size, _ in sorted(entries, key=lambda entry: entry[2]):
        if total <= GIT_REPOS_MAX_BYTES:
            return
        if path in busy:
            continue
        logger.info("Git-kopie %s opgeruimd: samen %d bytes, grens is %d", path, total, GIT_REPOS_MAX_BYTES)
        shutil.rmtree(path, ignore_errors=True)
        total -= size


def _repo_path_for(key: str) -> str:
    """Het pad van een kopie. Gehasht, want een repo-URL is geen mapnaam."""
    return os.path.join(GIT_REPOS_DIR, hashlib.sha256(key.encode()).hexdigest()[:16])


def _mode_of(path: str) -> str:
    if os.path.islink(path):
        return SYMLINK_MODE
    return EXEC_MODE if os.access(path, os.X_OK) else FILE_MODE


def _read_as_blob(path: str) -> FileChange:
    """De inhoud van een bestand zoals git hem opslaat: een symlink is zijn doelpad."""
    if os.path.islink(path):
        return FileChange(os.readlink(path).encode("utf-8"), SYMLINK_MODE)
    with open(path, "rb") as handle:
        return FileChange(handle.read(), _mode_of(path))


def _scan_directory(source_dir: str, scope_paths: list[str]) -> dict[str, FileChange]:
    """Alles wat er onder de scope in ``source_dir`` staat, als paden in de repo."""
    found: dict[str, FileChange] = {}
    for scope in scope_paths:
        root = os.path.join(source_dir, scope)
        if os.path.islink(root) or not os.path.isdir(root):
            if os.path.exists(root):
                found[scope] = _read_as_blob(root)
            continue
        for current, dirs, files in os.walk(root):
            # Symlinks naar directories: als files, niet directories.
            for name in list(dirs):
                link = os.path.join(current, name)
                if os.path.islink(link):
                    dirs.remove(name)
                    files.append(name)
            for name in files:
                full = os.path.join(current, name)
                found[os.path.relpath(full, source_dir)] = _read_as_blob(full)
    return found


def _guard_changes(changes: dict[str, FileChange | None], repo_url: str) -> None:
    """Weiger een bestandsset met secrets in platte tekst.

    Op de SET en niet op een walk of een ``git status``: zo geldt de controle voor elke
    aanroeper van :meth:`GitRepo.commit`, en kan een onaangeroerd bestand van een ander
    project deze commit niet ophouden. Een verwijdering mag juist wel.
    """
    plaintext = sorted(path for path, change in changes.items() if change is not None and path.endswith(TO_SOPS_SUFFIX))
    if plaintext:
        raise RuntimeError(
            f"Geweigerd om te committen naar {repo_url}: {len(plaintext)} onversleuteld(e) "
            f"{TO_SOPS_SUFFIX} bestand(en) in de commit: {', '.join(plaintext)}. "
            "Dit zou secrets in platte tekst naar git committen; operatie afgebroken."
        )

    cmp_env = sorted(
        path
        for path, change in changes.items()
        if change is not None and os.path.basename(path) == CMP_ENV_PLAINTEXT_FILENAME
    )
    if cmp_env:
        raise RuntimeError(
            f"Geweigerd om te committen naar {repo_url}: {len(cmp_env)} toegevoegd(e) of "
            f"gewijzigd(e) {CMP_ENV_PLAINTEXT_FILENAME}-bestand(en) in de commit: "
            f"{', '.join(cmp_env)}. De env-vars van een helmfile-deployment horen "
            "versleuteld; operatie afgebroken."
        )


class RepoAt:
    """Een lezer op één revisie, zoals een ``build_changes`` hem krijgt."""

    def __init__(self, repo: GitRepo, ref: str, credentials: GitCredentials) -> None:
        self.repo = repo
        self.ref = ref
        self._credentials = credentials

    async def read_file(self, path: str) -> bytes | None:
        return await self.repo.read_file(path, self.ref, self._credentials)

    async def list_tree(self, prefix: str = "") -> list[str]:
        return await self.repo.list_tree(prefix, self.ref)

    async def entries(self, prefix: str = "") -> dict[str, tuple[str, str]]:
        return await self.repo.entries(prefix, self.ref)

    async def changed_paths(self, since: str, prefix: str = "") -> list[str]:
        return await self.repo.changed_paths(since, self.ref, prefix)


BuildChanges = Callable[[RepoAt], Awaitable[dict[str, FileChange | None]]]


#: Markers in git's stderr that mean the remote was never reached or dropped us halfway.
_NETWORK_MARKERS = (
    "could not resolve host",
    "failed to connect to",
    "connection refused",
    "connection timed out",
    "connection reset",
    "early eof",
    "unexpected disconnect",
    "rpc failed",
    "the requested url returned error: 5",
    "gnutls_handshake() failed",
    "ssl_connect",
    "tls connection was non-properly terminated",
)

#: Markers that mean the server refused our credentials. Repeating that changes nothing.
_AUTH_MARKERS = (
    "authentication failed",
    "the requested url returned error: 401",
    "the requested url returned error: 403",
)


def is_git_network_failure(stderr: str) -> bool:
    """Whether a failed git call could not reach the remote, so repeating it may help."""
    lowered = stderr.lower()
    if any(marker in lowered for marker in _AUTH_MARKERS):
        return False
    return any(marker in lowered for marker in _NETWORK_MARKERS)


def is_git_auth_failure(stderr: str) -> bool:
    """Whether the remote refused our credentials."""
    lowered = stderr.lower()
    return any(marker in lowered for marker in _AUTH_MARKERS)


class GitRepo:
    """De beheerde kopie van één (remote, branch). Bewaart nooit credentials."""

    def __init__(self, url: str, branch: str = "main") -> None:
        #: Zonder credentials: de kopie op schijf mag er geen bevatten.
        self.url = strip_url_credentials(url)
        self.branch = branch
        self._key = _repo_key(url, branch)
        self.path = _repo_path_for(self._key)

    def __repr__(self) -> str:
        return f"GitRepo({self.url}#{self.branch})"

    # ------------------------------------------------------------------
    # de kopie op schijf
    # ------------------------------------------------------------------

    async def resolve(self, credentials: GitCredentials) -> str | None:
        """The SHA of our branch on the remote (``ls-remote``), only for :meth:`sync`.

        Not an access check per caller, as it was until RC-423: only one party has access
        to these repos. Every URL that reaches this comes from the OPI settings. The argo
        repo uses the platform token, and a project file's ``repositories[].url`` is
        written from ``PROJECT_REPO_URL`` on creation and is ``readonly_on_edit`` after
        that, which ``process_json_submission`` enforces server-side. The password such a
        file carries is a copy of that same platform token. This call checked the same
        credentials every time and could not stop anything. Should per-project credentials
        ever arrive, the copy must be keyed per credential identity, not per remote.
        """

        async def ls_remote() -> str:
            try:
                stdout, stderr, code = await run_git(
                    ["ls-remote", self.url, f"refs/heads/{self.branch}"],
                    cwd=settings.TEMP_DIR,
                    env=credentials.env(self.url),
                    timeout=GIT_PUSH_TIMEOUT_SECONDS,
                )
            except GitCommandTimeoutError as error:
                raise self._timeout_error("Opvragen van", GIT_PUSH_TIMEOUT_SECONDS) from error
            if code != 0:
                raise self._remote_error("Opvragen van", stderr)
            return stdout

        # Read-only, so a retry cannot leave anything else behind on the remote.
        stdout = await self._with_retry(ls_remote, "ls-remote op")
        first = stdout.strip().split("\n", 1)[0]
        return first.split("\t", 1)[0].strip() or None

    async def tip(self, credentials: GitCredentials) -> str:
        """The tip of our branch in the LOCAL copy, without network.

        Our own writes are write-through (the ``update-ref`` in :meth:`_push`), so the
        local ref is the state this task should build on. A commit from outside comes in
        through :meth:`sync` or through a rejected push, as with the ProjectStore.
        """
        async with _repo_lock(self._key):
            with rollout_phase(PHASE_GIT_PREPARE):
                return await self._tip_locked(credentials)

    async def sync(self, credentials: GitCredentials) -> str:
        """Pull in outside changes: one ``ls-remote``, and a fetch only when they differ.

        The equivalent of ``GitProjectStore.reconcile`` for these repos.
        """
        local = await self.tip(credentials)
        remote = await self.resolve(credentials)
        if remote is None:
            # The orphan sweep decides on removals from what sync returns; a branch that
            # vanished from the remote must not pass for the current state.
            raise RuntimeError(f"{self.url} has no branch {self.branch}")
        if remote == local:
            return local
        fetched = await self.fetch(credentials)
        logger.info("Fetched external change(s) in %s: %s..%s", self, local[:8], fetched[:8])
        return fetched

    async def fetch(self, credentials: GitCredentials) -> str:
        """Bring in the remote state of our branch and return the new local tip."""
        async with _repo_lock(self._key):
            with rollout_phase(PHASE_GIT_PREPARE):
                if await self._is_repo() and not await self._fetch(credentials):
                    await self._recreate(credentials)
                return await self._tip_locked(credentials)

    async def _tip_locked(self, credentials: GitCredentials) -> str:
        if not await self._is_repo():
            await self._create(credentials)
        revision = await self._local_tip()
        if revision is None:
            # A half-written or damaged copy (a leftover index.lock, an aborted clone)
            # heals by throwing it away and building it again; that must never cost a task.
            await self._recreate(credentials)
            revision = await self._local_tip()
            if revision is None:
                raise RuntimeError(f"{self.url}#{self.branch} has no branch {self.branch} after a fresh copy")
        os.utime(self.path)
        return revision

    async def _local_tip(self) -> str | None:
        stdout, _, code = await run_git(["rev-parse", f"refs/heads/{self.branch}"], cwd=self.path, env=OFFLINE_ENV)
        return stdout.strip() if code == 0 else None

    async def _recreate(self, credentials: GitCredentials) -> None:
        logger.warning("Git-kopie %s is onbruikbaar; opnieuw opbouwen", self.path)
        shutil.rmtree(self.path, ignore_errors=True)
        await self._create(credentials)

    async def _is_repo(self) -> bool:
        if not os.path.isdir(self.path):
            return False
        _, _, code = await run_git(["rev-parse", "--git-dir"], cwd=self.path, env=OFFLINE_ENV)
        return code == 0

    async def _create(self, credentials: GitCredentials) -> None:
        """A bare blobless clone WITH history: a per-file diff needs history."""
        os.makedirs(GIT_REPOS_DIR, exist_ok=True)
        _prune_repos(self.path)
        shutil.rmtree(self.path, ignore_errors=True)

        async def clone() -> None:
            # Every attempt starts with an empty directory: the previous one cleaned up its
            # half-finished copy.
            os.makedirs(self.path, exist_ok=True)
            try:
                _, stderr, code = await run_git(
                    [
                        "clone",
                        "--bare",
                        "--single-branch",
                        "--branch",
                        self.branch,
                        "--filter=blob:none",
                        self.url,
                        ".",
                    ],
                    cwd=self.path,
                    env=credentials.env(self.url),
                    timeout=GIT_CLONE_TIMEOUT_SECONDS,
                )
            except GitCommandTimeoutError as error:
                shutil.rmtree(self.path, ignore_errors=True)
                raise self._timeout_error("Klonen van", GIT_CLONE_TIMEOUT_SECONDS) from error
            if code != 0:
                shutil.rmtree(self.path, ignore_errors=True)
                raise self._remote_error("Klonen van", stderr)

        # Read-only, and a half-finished copy is cleaned up before the next attempt.
        started = time.monotonic()
        await self._with_retry(clone, "kloon van")
        # The copy never commits in a worktree, so a gc that prunes objects a running write
        # is looking at is pure risk of damage with nothing gained.
        await run_git(["config", "gc.auto", "0"], cwd=self.path, env=OFFLINE_ENV)
        logger.info(
            "Bare git-kopie van %s#%s klaargezet in %s in %.1fs",
            self.url,
            self.branch,
            self.path,
            time.monotonic() - started,
        )

    async def _fetch(self, credentials: GitCredentials) -> bool:
        """Fetch our branch. False means: the copy is unusable, build it again.

        An unreachable remote, a timeout and refused credentials are raised instead of
        returning False: the caller would rebuild the copy with a clone, which runs into
        the same wall and costs the warm copy as well.
        """

        async def fetch_once() -> str | None:
            try:
                _, stderr, code = await run_git(
                    ["fetch", "--force", "origin", f"+refs/heads/{self.branch}:refs/heads/{self.branch}"],
                    cwd=self.path,
                    env=credentials.env(self.url),
                    timeout=GIT_PUSH_TIMEOUT_SECONDS,
                )
            except GitCommandTimeoutError as error:
                raise self._timeout_error("Fetch van", GIT_PUSH_TIMEOUT_SECONDS) from error
            if code == 0:
                return None
            if is_git_network_failure(stderr) or is_git_auth_failure(stderr):
                raise self._remote_error("Fetch van", stderr)
            return git_stderr_detail(stderr)

        # Read-only, so a retry cannot leave anything else behind on the remote.
        failure = await self._with_retry(fetch_once, "fetch van")
        if failure is not None:
            logger.warning("Fetch in %s mislukte: %s", self.path, failure)
            return False
        os.utime(self.path)
        return True

    @asynccontextmanager
    async def _in_use(self) -> AsyncIterator[None]:
        """Houd de kopie vast zolang deze operatie eruit leest of erin schrijft."""
        _repo_users[self._key] = _repo_users.get(self._key, 0) + 1
        try:
            yield
        finally:
            _repo_users[self._key] -= 1

    def _timeout_error(self, operation: str, timeout: float) -> GitCommandTimeoutError:
        """The same timeout, named with the operation and the remote this copy belongs to.

        ``self.url`` carries no credentials, so neither does the message a task shows.
        """
        return GitCommandTimeoutError(
            f"{operation} {self.url}#{self.branch} kreeg geen antwoord binnen {timeout}s. "
            f"Dit ligt aan het platform, niet aan het project."
        )

    def _remote_error(self, operation: str, stderr: str) -> RuntimeError:
        """A failed git call to our remote, as one readable sentence.

        The stderr follows as detail. ``self.url`` carries no credentials, and a URL with
        userinfo in git's stderr loses it in :func:`git_stderr_detail`.
        """
        detail = git_stderr_detail(stderr)
        if is_git_network_failure(stderr):
            return GitRemoteUnavailableError(
                f"De git-server van {self.url}#{self.branch} is niet bereikbaar. "
                f"Dit ligt aan het platform, niet aan het project. Detail: {detail}",
                detail,
            )
        if is_git_auth_failure(stderr):
            return RuntimeError(
                f"De git-server van {self.url}#{self.branch} weigert de inloggegevens van OPI. "
                f"Dit ligt aan het platform, niet aan het project. Detail: {detail}"
            )
        # No "this is the platform" here: an unclassified git failure can just as well
        # come from what the project had us write.
        return RuntimeError(f"{operation} {self.url}#{self.branch} mislukte. Detail: {detail}")

    async def _with_retry[T](self, operation: Callable[[], Awaitable[T]], description: str) -> T:
        """Retry a network step to our remote for as long as it is unreachable.

        Comply or explain: ``retry_on_connect_error`` only retries failures where the
        connection never came up. For git it may be wider, and why is stated at each call
        next to the operation itself. A timeout is NOT retried (RC-421) and a collision
        runs through the loop in :meth:`commit`, not through here.
        """
        try:
            return await retry_on_connect_error(
                operation,
                budget_seconds=GIT_RETRY_BUDGET_SECONDS,
                description=f"{description} {self.url}#{self.branch}",
                connect_errors=(GitRemoteUnavailableError,),
            )
        except GitRemoteUnavailableError as error:
            raise GitRemoteUnavailableError(
                f"De git-server van {self.url}#{self.branch} is niet bereikbaar, ook niet na herhalen "
                f"gedurende {GIT_RETRY_BUDGET_SECONDS}s. Dit ligt aan het platform, niet aan het project; "
                f"probeer het later opnieuw. Detail: {error.detail}",
                error.detail,
            ) from error

    # ------------------------------------------------------------------
    # lezen
    # ------------------------------------------------------------------

    async def read_file(self, path: str, ref: str, credentials: GitCredentials | None = None) -> bytes | None:
        """The content of one path on ``ref``, or None when it is not there.

        A blob this copy does not have is fetched from the remote with ``credentials``. If
        that fails it raises: "could not read" is not "not there", and a caller that took it
        for absent would write or delete on a false premise.
        """
        async with self._in_use():
            oid = await self._object_id(ref, path)
            if oid is None:
                return None
            stdout, stderr, code = await run_git_bytes(
                ["cat-file", "blob", oid],
                cwd=self.path,
                env=(credentials or GitCredentials()).env(self.url),
            )
        if code != 0:
            detail = " ".join(stderr.split())
            raise RuntimeError(f"Kon {path} op {ref[:8]} niet lezen uit {self.url}: {git_stderr_detail(detail)}")
        return stdout

    async def list_tree(self, prefix: str = "", ref: str = "HEAD") -> list[str]:
        """Alle bestandspaden onder ``prefix`` op ``ref``. Geen blobs nodig."""
        args = ["ls-tree", "-r", "--name-only", "-z", ref]
        if prefix:
            args += ["--", prefix]
        async with self._in_use():
            stdout, _, code = await run_git(args, cwd=self.path, env=OFFLINE_ENV)
        if code != 0:
            return []
        return _split_nul(stdout)

    async def entries(self, prefix: str = "", ref: str = "HEAD") -> dict[str, tuple[str, str]]:
        """Pad -> (modus, blob-sha) onder ``prefix`` op ``ref``."""
        args = ["ls-tree", "-r", "-z", ref]
        if prefix:
            args += ["--", prefix]
        async with self._in_use():
            stdout, _, code = await run_git(args, cwd=self.path, env=OFFLINE_ENV)
        if code != 0:
            return {}
        found: dict[str, tuple[str, str]] = {}
        for line in _split_nul(stdout):
            if "\t" not in line:
                continue
            meta, path = line.split("\t", 1)
            mode, _, rest = meta.partition(" ")
            _, _, sha = rest.partition(" ")
            found[path] = (mode, sha.strip())
        return found

    async def changed_paths(self, old_ref: str, new_ref: str, prefix: str = "") -> list[str]:
        """De paden die tussen twee revisies wijzigden, eventueel beperkt tot ``prefix``.

        ``--no-renames`` om twee redenen. Een hernoeming met inhoudswijziging laat het
        OUDE pad anders uit de uitkomst verdwijnen, en dat pad is wel gewijzigd: de
        conflictgrendel van :meth:`write_dir` zou het missen. En inexacte
        rename-detectie heeft de blob-inhoud nodig, die op een blobless kopie via de
        promisor-remote gehaald moet worden.
        """
        args = ["diff", "--no-renames", "--name-only", "-z", old_ref, new_ref]
        if prefix:
            args += ["--", prefix]
        async with self._in_use():
            stdout, stderr, code = await run_git(args, cwd=self.path, env=OFFLINE_ENV)
        if code != 0:
            raise RuntimeError(f"git diff {old_ref[:8]}..{new_ref[:8]} op {self.url} faalde (exit {code}): {stderr}")
        return _split_nul(stdout)

    async def path_unchanged_since(self, revision: str, path: str, ref: str) -> bool:
        """Of ``path`` op ``revision`` byte-voor-byte gelijk is aan wat er op ``ref`` staat.

        Zo is te bewijzen dat een ander (ArgoCD) precies deze bestanden al heeft toegepast,
        ook als zijn revisie ouder is dan de onze: op een repo die meerdere projecten delen
        loopt de tip altijd voor op de laatste vergelijking van EEN applicatie.

        Een revisie die we niet hebben, of wat er ook mis gaat, geeft False: dan is er
        niets bewezen en doet de aanroeper zijn volle werk.
        """
        if not revision or not path:
            return False
        async with self._in_use():
            _, _, code = await run_git(
                ["diff", "--quiet", revision, ref, "--", path],
                cwd=self.path,
                env=OFFLINE_ENV,
            )
        return code == 0

    async def materialize(
        self, paths: list[str], ref: str, target_dir: str, credentials: GitCredentials | None = None
    ) -> list[str]:
        """Zet ``paths`` op ``ref`` in ``target_dir`` neer, met modus en symlinks intact.

        Via ``git archive``: dat kent de modi (uitvoerbaar, symlink) en haalt de ontbrekende
        blobs van een blobless kopie bij de promisor-remote. Lezen is zo veilig naast een
        andere lezer: er wordt niets in de kopie gewijzigd.

        Returns:
            De paden die er daadwerkelijk stonden.
        """
        async with self._in_use():
            env = (credentials or GitCredentials()).env(self.url)
            present = [path for path in paths if await self._object_id(ref, path) is not None]
            if not present:
                return []

            os.makedirs(target_dir, exist_ok=True)
            handle, archive = tempfile.mkstemp(prefix="git-archive-", suffix=".tar", dir=settings.TEMP_DIR)
            os.close(handle)
            try:
                # The clone's limit: on a fresh copy this fetches every blob under the paths,
                # and a whole cluster tree (materialize_cluster_tree) is a bulk transfer too.
                try:
                    _, stderr, code = await run_git(
                        ["archive", "--format=tar", "-o", archive, ref, "--", *present],
                        cwd=self.path,
                        env=env,
                        timeout=GIT_CLONE_TIMEOUT_SECONDS,
                    )
                except GitCommandTimeoutError as error:
                    raise self._timeout_error("Uitpakken uit", GIT_CLONE_TIMEOUT_SECONDS) from error
                if code != 0:
                    raise RuntimeError(
                        f"Kon {present} op {ref} niet uitpakken uit {self.url}: {' '.join(stderr.split())}"
                    )
                await asyncio.to_thread(_extract_tar, archive, target_dir)
            finally:
                if os.path.exists(archive):
                    os.unlink(archive)
        return present

    async def _object_id(self, ref: str, path: str) -> str | None:
        """The object of ``path`` on ``ref``, or None when the path is not there.

        Resolved from the trees alone, which a blobless copy has in full. ``cat-file -e``
        checked the blob itself, so a blob it could not fetch looked like an absent path.
        """
        stdout, _, code = await run_git(
            ["rev-parse", "--verify", "--quiet", f"{ref}:{path}"], cwd=self.path, env=OFFLINE_ENV
        )
        return stdout.strip() if code == 0 else None

    # ------------------------------------------------------------------
    # schrijven
    # ------------------------------------------------------------------

    async def commit(
        self,
        build_changes: BuildChanges,
        *,
        message: str,
        actor: Actor | None,
        credentials: GitCredentials,
    ) -> WriteResult:
        """Bouw een commit op de laatste stand en publiceer hem.

        ``build_changes`` krijgt een lezer op de actuele tip en geeft de paden terug die
        moeten veranderen. Weigert de remote de push (non-fast-forward), dan wordt
        ``build_changes`` OPNIEUW aangeroepen op de nieuwe tip: opnieuw lezen, opnieuw
        toepassen, begrensd door :data:`MAX_WRITE_ATTEMPTS`. Dezelfde lus als
        ``GitProjectStore.mutate``, en hier generiek. Er wordt alleen gefetcht als de
        nieuwe tip niet van een write van dit proces zelf komt.

        De commit wordt als los object gepusht naar ``refs/heads/<branch>``, dus de remote
        doet zelf de compare-and-swap en er is geen lokale ref om na een mislukte push
        terug te zetten.
        """
        async with self._in_use():
            started = time.monotonic()
            last_error: Exception | None = None
            for attempt in range(MAX_WRITE_ATTEMPTS):
                parent = await self.tip(credentials)
                changes = await build_changes(RepoAt(self, parent, credentials))
                if not changes:
                    logger.debug("Niets te committen naar %s: %s", self, message)
                    return WriteResult(ref=parent, committed=False)

                _guard_changes(changes, self.url)
                commit = await build_tree_commit(
                    cwd=self.path, changes=changes, message=message, parent=parent, actor=actor, env=OFFLINE_ENV
                )
                if commit is None:
                    return WriteResult(ref=parent, committed=False)

                try:
                    await self._push(commit, credentials)
                except GitPushConflictError as error:
                    last_error = error
                    # If the local ref moved meanwhile, another task in this process
                    # pushed and its tip is already here: retry without network.
                    moved = await self._local_tip() not in (None, parent)
                    if not moved:
                        await self.fetch(credentials)
                    logger.warning(
                        "Push naar %s afgewezen (%d/%d); opnieuw lezen en toepassen%s: %s",
                        self,
                        attempt + 1,
                        MAX_WRITE_ATTEMPTS,
                        "" if moved else " na een fetch",
                        message,
                    )
                    continue

                logger.info(
                    "Commit %s naar %s geland voor %d pad(en) in %.1fs na %d poging(en): %s",
                    commit[:8],
                    self,
                    len(changes),
                    time.monotonic() - started,
                    attempt + 1,
                    message,
                )
                return WriteResult(ref=commit, committed=True, paths=sorted(changes))

            raise GitPushConflictError(
                f"Kon na {MAX_WRITE_ATTEMPTS} pogingen niet naar {self.url}#{self.branch} pushen: {last_error}"
            )

    async def _push(self, commit: str, credentials: GitCredentials) -> None:
        """Push one commit object to our branch, serialized per (repo, branch)."""
        async with push_lock_for(self.url, self.branch):
            # Retrying inside the lock keeps it at most as long as the retry budget.
            # One commit without force lands exactly once, re-runs say "up-to-date", a moved ref says non-fast-forward.
            await self._with_retry(lambda: self._push_once(commit, credentials), "push naar")

            # Move our own ref along, so the next reader finds this commit without a
            # fetch. It is the remote tip by definition now.
            await run_git(["update-ref", f"refs/heads/{self.branch}", commit], cwd=self.path, env=OFFLINE_ENV)

    async def _push_once(self, commit: str, credentials: GitCredentials) -> None:
        """One attempt. Tells a collision, a refusal by the server and an outage apart."""
        try:
            with rollout_phase(PHASE_GIT_PUSH):
                # pack.window=0: delta compression reads the old version of each changed
                # file as a base, and on a blobless copy that is a fetch of blobs we never
                # had. A push carries one project's manifests, so the pack stays small.
                _, stderr, code = await run_git(
                    ["-c", "pack.window=0", "push", "origin", f"{commit}:refs/heads/{self.branch}"],
                    cwd=self.path,
                    env={**credentials.env(self.url), **OFFLINE_ENV},
                    timeout=GIT_PUSH_TIMEOUT_SECONDS,
                )
        except GitCommandTimeoutError as error:
            raise self._timeout_error("Push naar", GIT_PUSH_TIMEOUT_SECONDS) from error
        if code == 0:
            return

        if is_git_push_collision(stderr):
            raise GitPushConflictError(
                f"Push naar {self.url}#{self.branch} afgewezen, de remote is verder: {git_stderr_detail(stderr)}"
            )
        if is_git_network_failure(stderr) or is_git_auth_failure(stderr):
            raise self._remote_error("Push naar", stderr)
        raise GitPushRejectedError(
            f"De git-server weigerde de push naar {self.url}#{self.branch}: {git_push_rejection_reason(stderr)}."
        )

    async def write_dir(
        self,
        *,
        base_ref: str,
        scope_paths: list[str],
        source_dir: str,
        message: str,
        actor: Actor | None,
        credentials: GitCredentials,
    ) -> WriteResult:
        """Maak de repo onder ``scope_paths`` gelijk aan wat er in ``source_dir`` staat.

        Toegevoegd, gewijzigd en verwijderd ten opzichte van ``base_ref``, en per
        constructie niets buiten de scope: de commit kan geen pad bevatten dat niet onder
        een scope-pad valt.

        Heeft de tip een pad in de scope sinds ``base_ref`` ANDERS gewijzigd dan wij het
        schrijven, dan is dat een :class:`GitRepoConflictError`: de aanroeper doet zijn
        stap opnieuw. Dezelfde inhoud is geen conflict.
        """
        scope = _normalize_scope(scope_paths)
        desired = await asyncio.to_thread(_scan_directory, source_dir, scope)
        for path in desired:
            if not _within_scope(path, scope):
                raise ValueError(f"Pad '{path}' valt buiten de scope {scope}")

        async def build_changes(at: RepoAt) -> dict[str, FileChange | None]:
            present: dict[str, tuple[str, str]] = {}
            for prefix in scope:
                present.update(await at.entries(prefix))

            changes: dict[str, FileChange | None] = {}
            for path, change in desired.items():
                existing = present.get(path)
                if existing is None or existing[0] != change.mode or _blob_sha(change.content) != existing[1]:
                    changes[path] = change
            for path in present:
                if path not in desired:
                    changes[path] = None

            if at.ref != base_ref and changes:
                moved = {path for prefix in scope for path in await at.changed_paths(base_ref, prefix)}
                clashing = sorted(moved & set(changes))
                if clashing:
                    raise GitRepoConflictError(
                        f"{self.url}#{self.branch} is onder {scope} gewijzigd sinds {base_ref[:8]}: "
                        f"{', '.join(clashing)}. Opnieuw materialiseren en genereren."
                    )
            return changes

        return await self.commit(build_changes, message=message, actor=actor, credentials=credentials)


def _split_nul(stdout: str) -> list[str]:
    """Paden uit de -z-vorm van ls-tree en diff.

    Zonder -z C-quote git elk pad met niet-ASCII bytes (core.quotePath staat standaard
    aan), en dan is de sleutel een andere tekenreeks dan die van het bestand op schijf.
    """
    return [entry for entry in stdout.split("\0") if entry]


def _extract_tar(archive: str, target_dir: str) -> None:
    with tarfile.open(archive) as tar:
        tar.extractall(target_dir, filter="tar")


def _blob_sha(content: bytes) -> str:
    """De git-blob-sha van deze inhoud, zonder hem te schrijven."""
    header = f"blob {len(content)}\0".encode()
    return hashlib.sha1(header + content, usedforsecurity=False).hexdigest()


def _normalize_scope(scope_paths: list[str]) -> list[str]:
    """Scope-paden als nette, repo-relatieve mappaden zonder dubbele of lege waarden."""
    normalized: list[str] = []
    for raw in scope_paths:
        path = os.path.normpath(raw.strip()).strip("/")
        if not path or path == "." or path.startswith(".."):
            raise ValueError(f"Ongeldig scope-pad: {raw!r}")
        if path not in normalized:
            normalized.append(path)
    return normalized


def _within_scope(path: str, scope: list[str]) -> bool:
    return any(path == prefix or path.startswith(f"{prefix}/") for prefix in scope)


class GitRepoWorkspace:
    """De wegwerpmap van één taak op één repo, en de write terug.

    De generatie schrijft, globt en verwijdert gewoon in een map op schijf, precies zoals
    ze in een werkboom deed: de SOPS-skip, de prunes en de kustomization hebben de vorige
    stand op schijf nodig. Het verschil is dat die map alleen de SCOPE van deze taak bevat
    en dat niemand hem deelt, en dat de write erna één commit is die per constructie geen
    pad buiten die scope kan raken.
    """

    def __init__(
        self,
        repo: GitRepo,
        credentials: GitCredentials,
        scope_paths: list[str],
        *,
        project_name: str | None = None,
        name: str | None = None,
    ) -> None:
        self.repo = repo
        self.credentials = credentials
        self.scope_paths = _normalize_scope(scope_paths)
        self.project_name = project_name
        self.name = name
        self._working_dir = ""
        self._base_ref = ""
        #: Resultaat van de laatste write; ``mutate_and_push`` wist hem zodat het altijd bij die stap hoort.
        self.last_write: WriteResult | None = None

    def __repr__(self) -> str:
        return f"GitRepoWorkspace({self.name or self.project_name}, {self.repo}, {self.scope_paths})"

    async def get_working_dir(self) -> str:
        """De map met de scope van deze ronde erin, gematerialiseerd bij eerste gebruik."""
        if not self._base_ref:
            await self.refresh()
        return self._working_dir

    @property
    def base_ref(self) -> str:
        """De revisie waarvan de werkmap gemaakt is, of leeg als hij nog niet bestaat."""
        return self._base_ref

    async def refresh(self) -> str:
        """Vul de werkmap (opnieuw) met de scope op de laatste stand van de kopie."""
        if not self._working_dir:
            self._working_dir = tempfile.mkdtemp(prefix="git-scope-", dir=settings.TEMP_DIR)
        else:
            for name in os.listdir(self._working_dir):
                path = os.path.join(self._working_dir, name)
                shutil.rmtree(path, ignore_errors=True) if os.path.isdir(path) else os.unlink(path)

        self._base_ref = await self.repo.tip(self.credentials)
        await self.repo.materialize(self.scope_paths, self._base_ref, self._working_dir, self.credentials)
        logger.debug("Werkmap %s gevuld met %s op %s", self._working_dir, self.scope_paths, self._base_ref[:8])
        return self._base_ref

    async def commit_and_push(self, message: str) -> WriteResult:
        """Schrijf de stand van de werkmap terug als één commit op de scope."""
        working_dir = await self.get_working_dir()
        result = await self.repo.write_dir(
            base_ref=self._base_ref,
            scope_paths=self.scope_paths,
            source_dir=working_dir,
            message=message,
            actor=None,
            credentials=self.credentials,
        )
        self.last_write = result
        if result.committed:
            self._base_ref = result.ref
        return result

    async def mutate_and_push[T](
        self,
        message: str,
        mutate: Callable[[], Awaitable[T]],
        on_prepare: Callable[[], AbstractContextManager[None]] | None = None,
        on_push: Callable[[], AbstractContextManager[None]] | None = None,
    ) -> T:
        """Pas een stap toe op de laatste stand en publiceer hem, geserialiseerd per project.

        Per poging: de werkmap opnieuw vullen uit de kopie, ``mutate`` laten schrijven, en
        schrijven. Botst de write met een wijziging binnen dezelfde scope, dan gaat de hele
        stap opnieuw op de nieuwe stand, want alleen de aanroeper weet wat hij genereerde.

        ``on_prepare`` wraps filling the working copy (a clone when there is no copy yet) and
        ``on_push`` only the write, so a caller that reports progress can show those two
        network phases as their own step instead of as part of ``mutate``. Every new
        attempt opens them again.

        De lock per (repo, project) blijft: de kustomization van het project is het bestand
        waar taken van hetzelfde project om strijden (RC-265).
        """
        self.last_write = None
        async with project_write_lock(self.repo.url, self.project_name):
            started = time.monotonic()
            last_error: Exception | None = None
            for attempt in range(MAX_WRITE_ATTEMPTS):
                # De eerste poging neemt de werkmap zoals hij is. Een eerdere stap van
                # dezelfde taak kan er al iets in hebben gezet dat nog niet gecommit is,
                # zoals de PVC-hernoeming van ``handle_service_removal``, en die hoort in
                # DEZE write mee. Pas een echte botsing gooit de map weg en begint opnieuw.
                if attempt > 0 or not self._base_ref:
                    with on_prepare() if on_prepare else nullcontext():
                        await self.refresh()
                outcome = await mutate()
                try:
                    with on_push() if on_push else nullcontext():
                        await self.commit_and_push(message)
                except GitRepoConflictError as error:
                    last_error = error
                    logger.warning(
                        "Botsing binnen %s bij '%s' (%d/%d); werkmap opnieuw vullen en de stap herhalen",
                        self.scope_paths,
                        message,
                        attempt + 1,
                        MAX_WRITE_ATTEMPTS,
                    )
                    continue
                if self.last_write is not None and self.last_write.committed:
                    logger.info(
                        "'%s' op %s toegepast in %.1fs na %d poging(en)",
                        message,
                        self.repo,
                        time.monotonic() - started,
                        attempt + 1,
                    )
                else:
                    logger.debug("'%s' liet %s ongewijzigd", message, self.repo)
                return outcome

            raise GitRepoConflictError(
                f"Kon '{message}' na {MAX_WRITE_ATTEMPTS} pogingen niet toepassen op "
                f"{self.repo.url}#{self.repo.branch}: {last_error}"
            )

    async def remove_and_push(self, relative_path: str, message: str) -> bool:
        """Haal een pad binnen de scope weg en publiceer dat. False als het er niet stond.

        De BESLISSING valt binnen de lock, op de stand die de werkmap dan heeft: een
        andere taak kan deze map intussen herschreven of al verwijderd hebben.
        """
        if not _within_scope(os.path.normpath(relative_path).strip("/"), self.scope_paths):
            raise ValueError(f"Pad '{relative_path}' valt buiten de scope {self.scope_paths}")

        async def _remove() -> bool:
            full_path = os.path.join(await self.get_working_dir(), relative_path)
            if not os.path.exists(full_path) and not os.path.islink(full_path):
                return False
            if os.path.isdir(full_path) and not os.path.islink(full_path):
                await asyncio.to_thread(shutil.rmtree, full_path)
            else:
                os.unlink(full_path)
            return True

        return await self.mutate_and_push(message, _remove)

    async def path_unchanged_since(self, revision: str, path: str) -> bool:
        """Of ``path`` op ``revision`` gelijk is aan wat er nu in de repo staat.

        Zie :meth:`GitRepo.path_unchanged_since`; hier tegen de stand waar deze werkmap op
        staat, dus na een write de commit die net landde.
        """
        if not self._base_ref:
            return False
        return await self.repo.path_unchanged_since(revision, path, self._base_ref)

    async def close(self) -> None:
        """Gooi de werkmap weg. De beheerde kopie blijft staan."""
        if self._working_dir:
            shutil.rmtree(self._working_dir, ignore_errors=True)
            self._working_dir = ""
            self._base_ref = ""


def credentials_from_repo_config(repo_config: dict[str, Any]) -> GitCredentials:
    """De toegang uit een repo-config, met het wachtwoord uitgepakt.

    Hetzelfde uitpakken als :class:`~opi.connectors.git.GitConnector` deed, want een
    projectbestand bewaart het wachtwoord AGE-versleuteld.
    """
    password = repo_config.get("password")
    decrypted = decrypt_password_smart_auto_sync(password) if password else None
    if password and decrypted == password and password.startswith(("base64+age:", "age:")):
        raise ValueError("Failed to decrypt password - Age decryption unsuccessful")
    return GitCredentials(username=repo_config.get("username"), password=decrypted)


def workspace_from_repo_config(
    repo_config: dict[str, Any], scope_paths: list[str], *, project_name: str | None = None
) -> GitRepoWorkspace:
    """De werkmap van een taak op de repo uit deze config, beperkt tot ``scope_paths``."""
    return GitRepoWorkspace(
        get_git_repo(repo_config["url"], repo_config.get("branch") or "main"),
        credentials_from_repo_config(repo_config),
        scope_paths,
        project_name=project_name or repo_config.get("project_name"),
        name=repo_config.get("name"),
    )


def argo_repo_config() -> dict[str, Any]:
    """De repo-config van de argo-applications repo, uit de instellingen."""
    return {
        "url": settings.GIT_ARGO_APPLICATIONS_URL,
        "branch": settings.GIT_ARGO_APPLICATIONS_BRANCH,
        "username": settings.GIT_ARGO_APPLICATIONS_USERNAME,
        "password": settings.GIT_ARGO_APPLICATIONS_PASSWORD,
        "name": "argo",
    }


def deployments_repo_config() -> dict[str, Any]:
    """The repo config of the shared deployments repo, from the settings."""
    return {
        "url": settings.PROJECT_REPO_URL,
        "branch": settings.PROJECT_REPO_BRANCH,
        "username": settings.PROJECT_REPO_USERNAME,
        "password": settings.PROJECT_REPO_PASSWORD,
        "name": "deployments",
    }


async def sync_platform_repos() -> None:
    """Pull in outside changes in the deployments repo and the argo repo.

    For a refresh, which means "use what is in git now". Costs one ``ls-remote``
    per repo when nothing changed.
    """
    for config in (deployments_repo_config(), argo_repo_config()):
        repo = get_git_repo(config["url"], config["branch"])
        await repo.sync(credentials_from_repo_config(config))


async def warm_platform_repos() -> None:
    """Warm up bare copies of deployments and argo repos at startup.

    A clone costs minutes; without this the first writing task pays for it.
    A failure only logs: the first task clones it itself then.
    """
    for config in (deployments_repo_config(), argo_repo_config()):
        repo = get_git_repo(config["url"], config["branch"])
        try:
            await repo.tip(credentials_from_repo_config(config))
        # ValueError too: an undecryptable repo password comes from
        # credentials_from_repo_config, and must not cost the other repo its warm-up.
        except (RuntimeError, OSError, ValueError) as exc:
            logger.warning("Kon de git-kopie van %s niet vooraf klaarzetten: %s", repo, exc)


def argo_workspace(project_name: str, cluster: str) -> GitRepoWorkspace:
    """De werkmap op de argo-applications repo voor één project.

    Twee mappen: die van het project en die van zijn infrastructuur ernaast.
    """
    return workspace_from_repo_config(
        argo_repo_config(),
        [
            generate_argocd_project_folder_path(cluster, project_name),
            generate_infrastructure_argocd_folder_path(cluster, project_name),
        ],
        project_name=project_name,
    )
