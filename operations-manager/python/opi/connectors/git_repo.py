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
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from opi.connectors.git import (
    EXEC_MODE,
    FILE_MODE,
    SYMLINK_MODE,
    FileChange,
    GitPushConflictError,
    build_tree_commit,
    credential_env,
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

#: Serialiseert het bijwerken van één kopie, en houdt hem tegen de opruiming vast.
_repo_locks: dict[str, asyncio.Lock] = {}

#: Hoeveel taken er op dit moment uit een kopie lezen of erin schrijven. Houdt hem tegen
#: de opruiming vast, ook buiten :meth:`GitRepo.ensure`, waar de lock niet vastzit.
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
    kopie. De toegangscontrole per taak is de ``ls-remote`` met de credentials van de
    aanroeper in :meth:`GitRepo.resolve`.
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
        return await self.repo.changed_paths(since, self.ref, prefix, self._credentials)


BuildChanges = Callable[[RepoAt], Awaitable[dict[str, FileChange | None]]]


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
        """De SHA van onze branch op de remote, met de credentials van de AANROEPER.

        Dit is de toegangscontrole, hetzelfde patroon als de ArgoCD repo-server
        (``newClientResolveRevision`` voor ``checkoutRevision``): eerst resolven met de
        eigen toegang, dan pas de gedeelde kopie. Een ingetrokken of verkeerd token faalt
        hier, zodat niemand via een kopie leest die een ander project vulde.
        """
        stdout, stderr, code = await run_git(
            ["ls-remote", self.url, f"refs/heads/{self.branch}"],
            cwd=settings.TEMP_DIR,
            env=credentials.env(self.url),
        )
        if code != 0:
            raise RuntimeError(f"Kon {self.url}#{self.branch} niet opvragen: {' '.join(stderr.split())}")
        first = stdout.strip().split("\n", 1)[0]
        return first.split("\t", 1)[0].strip() or None

    async def tip(self, credentials: GitCredentials) -> str:
        """De remote tip, met de objecten ervan lokaal aanwezig."""
        revision = await self.resolve(credentials)
        if revision is None:
            raise RuntimeError(f"{self.url}#{self.branch} heeft geen branch {self.branch}")
        await self.ensure(revision, credentials)
        return revision

    async def ensure(self, revision: str, credentials: GitCredentials) -> None:
        """Zorg dat ``revision`` in de kopie staat, en fetch alleen als dat moet."""
        async with _repo_lock(self._key):
            with rollout_phase(PHASE_GIT_PREPARE):
                await self._ensure_locked(revision, credentials)

    async def _ensure_locked(self, revision: str, credentials: GitCredentials) -> None:
        if not await self._is_repo():
            await self._create(credentials)
        elif await self._has(revision):
            os.utime(self.path)
            return

        if not await self._fetch(credentials) or not await self._has(revision):
            # Een halve of beschadigde kopie (een achtergebleven index.lock, een
            # afgebroken fetch) herstelt zich door hem weg te gooien en opnieuw op te
            # bouwen; dat mag nooit een taak kosten.
            logger.warning("Git-kopie %s is onbruikbaar; opnieuw opbouwen", self.path)
            shutil.rmtree(self.path, ignore_errors=True)
            await self._create(credentials)
            if not await self._has(revision):
                raise RuntimeError(
                    f"Revisie {revision} staat na een verse kopie van {self.url} nog niet in {self.path}"
                )

    async def _is_repo(self) -> bool:
        if not os.path.isdir(self.path):
            return False
        _, _, code = await run_git(["rev-parse", "--git-dir"], cwd=self.path)
        return code == 0

    async def _create(self, credentials: GitCredentials) -> None:
        """Een bare blobless kloon MET historie: een diff per bestand heeft historie nodig."""
        os.makedirs(GIT_REPOS_DIR, exist_ok=True)
        _prune_repos(self.path)
        shutil.rmtree(self.path, ignore_errors=True)
        os.makedirs(self.path, exist_ok=True)
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
        )
        if code != 0:
            shutil.rmtree(self.path, ignore_errors=True)
            raise RuntimeError(f"Kon {self.url}#{self.branch} niet klonen: {' '.join(stderr.split())}")
        # De kopie committeert nooit in een werkboom, dus een gc die objecten opruimt waar
        # een lopende write naar kijkt is pure kans op schade zonder winst.
        await run_git(["config", "gc.auto", "0"], cwd=self.path)
        logger.info("Bare git-kopie van %s#%s klaargezet in %s", self.url, self.branch, self.path)

    async def _fetch(self, credentials: GitCredentials) -> bool:
        _, stderr, code = await run_git(
            ["fetch", "--force", "origin", f"+refs/heads/{self.branch}:refs/heads/{self.branch}"],
            cwd=self.path,
            env=credentials.env(self.url),
        )
        if code != 0:
            logger.warning("Fetch in %s mislukte: %s", self.path, " ".join(stderr.split()))
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

    async def _has(self, revision: str) -> bool:
        _, _, code = await run_git(["cat-file", "-e", f"{revision}^{{commit}}"], cwd=self.path)
        return code == 0

    # ------------------------------------------------------------------
    # lezen
    # ------------------------------------------------------------------

    async def read_file(self, path: str, ref: str, credentials: GitCredentials | None = None) -> bytes | None:
        """De inhoud van één pad op ``ref``, of None als het er niet staat."""
        async with self._in_use():
            stdout, _, code = await run_git_bytes(
                ["cat-file", "blob", f"{ref}:{path}"],
                cwd=self.path,
                env=(credentials or GitCredentials()).env(self.url),
            )
        return stdout if code == 0 else None

    async def list_tree(self, prefix: str = "", ref: str = "HEAD") -> list[str]:
        """Alle bestandspaden onder ``prefix`` op ``ref``. Geen blobs nodig."""
        args = ["ls-tree", "-r", "--name-only", "-z", ref]
        if prefix:
            args += ["--", prefix]
        async with self._in_use():
            stdout, _, code = await run_git(args, cwd=self.path)
        if code != 0:
            return []
        return _split_nul(stdout)

    async def entries(self, prefix: str = "", ref: str = "HEAD") -> dict[str, tuple[str, str]]:
        """Pad -> (modus, blob-sha) onder ``prefix`` op ``ref``."""
        args = ["ls-tree", "-r", "-z", ref]
        if prefix:
            args += ["--", prefix]
        async with self._in_use():
            stdout, _, code = await run_git(args, cwd=self.path)
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

    async def changed_paths(
        self, old_ref: str, new_ref: str, prefix: str = "", credentials: GitCredentials | None = None
    ) -> list[str]:
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
            stdout, stderr, code = await run_git(
                args, cwd=self.path, env=(credentials or GitCredentials()).env(self.url)
            )
        if code != 0:
            raise RuntimeError(f"git diff {old_ref[:8]}..{new_ref[:8]} op {self.url} faalde (exit {code}): {stderr}")
        return _split_nul(stdout)

    async def path_unchanged_since(
        self, revision: str, path: str, ref: str, credentials: GitCredentials | None = None
    ) -> bool:
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
                env=(credentials or GitCredentials()).env(self.url),
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
            present = [path for path in paths if await self._path_exists(ref, path, env)]
            if not present:
                return []

            os.makedirs(target_dir, exist_ok=True)
            handle, archive = tempfile.mkstemp(prefix="git-archive-", suffix=".tar", dir=settings.TEMP_DIR)
            os.close(handle)
            try:
                _, stderr, code = await run_git(
                    ["archive", "--format=tar", "-o", archive, ref, "--", *present], cwd=self.path, env=env
                )
                if code != 0:
                    raise RuntimeError(
                        f"Kon {present} op {ref} niet uitpakken uit {self.url}: {' '.join(stderr.split())}"
                    )
                await asyncio.to_thread(_extract_tar, archive, target_dir)
            finally:
                if os.path.exists(archive):
                    os.unlink(archive)
        return present

    async def _path_exists(self, ref: str, path: str, env: dict[str, str]) -> bool:
        _, _, code = await run_git(["cat-file", "-e", f"{ref}:{path}"], cwd=self.path, env=env)
        return code == 0

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
        ``GitProjectStore.mutate``, en hier generiek.

        De commit wordt als los object gepusht naar ``refs/heads/<branch>``, dus de remote
        doet zelf de compare-and-swap en er is geen lokale ref om na een mislukte push
        terug te zetten.
        """
        async with self._in_use():
            last_error: Exception | None = None
            for attempt in range(MAX_WRITE_ATTEMPTS):
                parent = await self.tip(credentials)
                changes = await build_changes(RepoAt(self, parent, credentials))
                if not changes:
                    logger.debug("Niets te committen naar %s: %s", self, message)
                    return WriteResult(ref=parent, committed=False)

                _guard_changes(changes, self.url)
                commit = await build_tree_commit(
                    cwd=self.path, changes=changes, message=message, parent=parent, actor=actor
                )
                if commit is None:
                    return WriteResult(ref=parent, committed=False)

                try:
                    await self._push(commit, credentials)
                except GitPushConflictError as error:
                    last_error = error
                    logger.warning(
                        "Push naar %s afgewezen (%d/%d); opnieuw lezen en toepassen: %s",
                        self,
                        attempt + 1,
                        MAX_WRITE_ATTEMPTS,
                        message,
                    )
                    continue

                logger.info("Commit %s naar %s geland voor %d pad(en): %s", commit[:8], self, len(changes), message)
                return WriteResult(ref=commit, committed=True, paths=sorted(changes))

            raise GitPushConflictError(
                f"Kon na {MAX_WRITE_ATTEMPTS} pogingen niet naar {self.url}#{self.branch} pushen: {last_error}"
            )

    async def _push(self, commit: str, credentials: GitCredentials) -> None:
        """Push één commit-object naar onze branch, geserialiseerd per (repo, branch)."""
        async with push_lock_for(self.url, self.branch):
            with rollout_phase(PHASE_GIT_PUSH):
                _, stderr, code = await run_git(
                    ["push", "origin", f"{commit}:refs/heads/{self.branch}"],
                    cwd=self.path,
                    env=credentials.env(self.url),
                )
            if code != 0:
                lowered = stderr.lower()
                if any(
                    marker in lowered
                    for marker in ("non-fast-forward", "fetch first", "failed to push some refs", "incorrect old value")
                ):
                    raise GitPushConflictError(
                        f"Push naar {self.url}#{self.branch} afgewezen, de remote is verder: {' '.join(stderr.split())}"
                    )
                raise RuntimeError(f"Push naar {self.url}#{self.branch} mislukte: {' '.join(stderr.split())}")

            # De eigen ref meeverzetten, zodat de volgende lezer deze commit zonder fetch
            # vindt. Hij is nu per definitie de remote tip.
            await run_git(["update-ref", f"refs/heads/{self.branch}", commit], cwd=self.path)

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
        """Vul de werkmap (opnieuw) met de scope op de laatste stand van de remote."""
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

    async def mutate_and_push[T](self, message: str, mutate: Callable[[], Awaitable[T]]) -> T:
        """Pas een stap toe op de laatste stand en publiceer hem, geserialiseerd per project.

        Per poging: de werkmap opnieuw vullen uit de remote, ``mutate`` laten schrijven, en
        schrijven. Botst de write met een wijziging binnen dezelfde scope, dan gaat de hele
        stap opnieuw op de nieuwe stand, want alleen de aanroeper weet wat hij genereerde.

        De lock per (repo, project) blijft: de kustomization van het project is het bestand
        waar taken van hetzelfde project om strijden (RC-265).
        """
        self.last_write = None
        async with project_write_lock(self.repo.url, self.project_name):
            last_error: Exception | None = None
            for attempt in range(MAX_WRITE_ATTEMPTS):
                # De eerste poging neemt de werkmap zoals hij is. Een eerdere stap van
                # dezelfde taak kan er al iets in hebben gezet dat nog niet gecommit is,
                # zoals de PVC-hernoeming van ``handle_service_removal``, en die hoort in
                # DEZE write mee. Pas een echte botsing gooit de map weg en begint opnieuw.
                if attempt > 0 or not self._base_ref:
                    await self.refresh()
                outcome = await mutate()
                try:
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
        return await self.repo.path_unchanged_since(revision, path, self._base_ref, self.credentials)

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
