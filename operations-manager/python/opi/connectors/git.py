"""The one place this process spawns git: commands, credentials, commits and pushes."""

import asyncio
import base64
import logging
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, Self
from urllib.parse import ParseResult, urlparse, urlunparse

from opi.core.actor import Actor, get_actor
from opi.core.config import settings
from opi.core.metrics import PHASE_GIT_PREPARE, PHASE_GIT_PUSH, rollout_phase
from opi.core.subprocess_env import child_env
from opi.generation.manifests import CMP_ENV_NAME, CMP_ENV_PLAINTEXT_FILENAME
from opi.utils.age import decrypt_password_smart_auto_sync
from opi.utils.git_signing import get_git_signing_identity
from opi.utils.sops import TO_SOPS_SUFFIX, sops_filenames
from opi.utils.yaml_util import YAMLError, load_yaml_safe

if TYPE_CHECKING:
    from types import TracebackType

logger = logging.getLogger(__name__)

#: Shared process-wide, one per (repo, branch). See features/git-push-serialization.md.
_push_locks: dict[str, asyncio.Lock] = {}


#: Lock per (repo, project) to serialize writes to the project directory in argo-applications.
_project_write_locks: dict[str, asyncio.Lock] = {}


#: The default branch per repo, as ``ls-remote --symref`` reported it once. The answer does
#: not change within one process's life, and asking costs a network round trip.
_remote_default_branches: dict[str, str] = {}


def repo_lock_key(repo_url: str) -> str:
    """The repo URL without credentials: a remote's identity, for locks and caches."""
    sanitized = re.sub(r"//[^/@]*@", "//", repo_url)  # https://user:pass@host -> https://host
    return re.sub(r"^[^@/]+@", "", sanitized)  # git@host:path -> host:path


def push_lock_for(repo_url: str, branch: str) -> asyncio.Lock:
    """Return the shared push lock for a (repo, branch), creating it once."""
    return _push_locks.setdefault(f"{repo_lock_key(repo_url)}#{branch}", asyncio.Lock())


def project_write_lock(repo_url: str, project_name: str | None) -> asyncio.Lock:
    """The lock that serialises writes to this repo's project directory."""
    return _project_write_locks.setdefault(f"{repo_lock_key(repo_url)}#{project_name or ''}", asyncio.Lock())


# Identity used for every commit this connector creates. Kept as constants so the
# working-tree path (git config) and the plumbing path (commit-tree env) cannot drift.
GIT_COMMIT_AUTHOR_NAME = "Operations Manager"
GIT_COMMIT_AUTHOR_EMAIL = "operations-manager@example.com"
# Set explicitly on both paths: a global gpg.format=ssh would otherwise read the gpg
# fingerprint as an ssh key.
GIT_SIGNING_FORMAT = "openpgp"

#: Timeout for git commands that go out over the network (push, fetch, ls-remote).
#: Without it, curl's connect timeout (300s) is the only limit.
GIT_PUSH_TIMEOUT_SECONDS = 60

#: Own limit for a clone: a blobless bare clone may take a while, but not forever.
#: The platform repos are cloned at startup, so no live action waits for one.
GIT_CLONE_TIMEOUT_SECONDS = 300

#: How long a network step to the remote may be retried before the task fails.
GIT_RETRY_BUDGET_SECONDS = 15

COMMIT_ACTOR_TRAILER = "Actor"
"""Git trailer naming who started the commit. Author and committer stay OPI; a trailer so
git can read it out (``git log --format=%(trailers:key=Actor,valueonly)``).
"""

_TRAILER_LINE = re.compile(r"^[A-Za-z][A-Za-z0-9-]*: ")


def _with_actor_trailer(message: str, actor: Actor | None = None) -> str:
    """Add the actor trailer to a commit message; without ``actor`` the one from the context."""
    body = message.rstrip("\n")
    # A newline in the value would make it a second trailer. From outside only the address a
    # federation peer sends along arrives here, and this keeps that on one line.
    trailer = f"{COMMIT_ACTOR_TRAILER}: {' '.join(str(actor or get_actor()).split())}"
    if any(line.startswith(f"{COMMIT_ACTOR_TRAILER}: ") for line in body.splitlines()):
        return body

    # A trailer belongs in the last paragraph, and the subject line is not a paragraph to
    # add to: only under an existing trailer block does the line join directly.
    paragraphs = body.split("\n\n")
    last = paragraphs[-1].splitlines()
    if len(paragraphs) > 1 and last and all(_TRAILER_LINE.match(line) for line in last):
        return f"{body}\n{trailer}"
    return f"{body}\n\n{trailer}"


#: A fixed path, so the host key the first SSH connection records survives the next ones and
#: a change in between stands out.
SSH_KNOWN_HOSTS_PATH = os.path.join(settings.TEMP_DIR, "git-known-hosts")


#: userinfo in an http(s) URL: user:password@host, and the token-only form. The second
#: excludes ":" so it cannot run over the first one's result.
_URL_USERINFO_PATTERN = re.compile(r"(https?://)([^:/@\s]+):[^@\s]+@")
_URL_TOKEN_PATTERN = re.compile(r"(https?://)[^:/@\s]+@")


def _obfuscate_git_command(cmd_str: str) -> str:
    """Mask credentials in a git command for the log.

    A second layer: credentials go through the environment (see ``credential_env``), so a URL
    should hold no userinfo, but a URL out of a project file can still carry it.
    """
    obfuscated = _URL_USERINFO_PATTERN.sub(r"\1\2:***@", cmd_str)
    return _URL_TOKEN_PATTERN.sub(r"\1***@", obfuscated)


def _authority(parsed: ParseResult) -> str:
    """Host and port of a parsed URL, without userinfo.

    Out of netloc and not out of parsed.hostname, because that strips the brackets off an
    IPv6 literal and turns it into an unusable address (::1:3000).
    """
    return parsed.netloc.rsplit("@", 1)[-1]


def strip_url_credentials(url: str) -> str:
    """An http(s) URL without its userinfo. Any other form comes back unchanged."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return url

    authority = _authority(parsed)
    return urlunparse((parsed.scheme, authority, parsed.path, parsed.params, parsed.query, parsed.fragment))


def _cmp_env_paths_added_or_modified(status_output: str) -> list[str]:
    """Paths from ``git status --porcelain`` where a plain-text ``.cmp-env`` appears or changes.

    A deletion is exactly what is wanted: that is how the files still there disappear.
    """
    touched: list[str] = []
    for line in status_output.splitlines():
        if len(line) < 4:
            continue
        codes, path = line[:2], line[3:]
        # A rename names the old name first and then the new one; the latter lands in the commit.
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        path = path.strip('"')
        if os.path.basename(path) != CMP_ENV_PLAINTEXT_FILENAME:
            continue
        if set(codes) <= {"D", " "}:
            continue
        touched.append(path)
    return sorted(touched)


def credential_env(repo_url: str, username: str | None, password: str | None) -> dict[str, str]:
    """Git config in the environment that binds the Basic auth header to one base URL.

    Through the environment instead of through the URL, so the password lands in neither a
    process argument, nor .git/config, nor a log line (BIO2 5.17, 8.12). The key names scheme,
    host and port of the repo URL, so git sends the header only there and not along after a
    redirect to another host.
    """
    parsed = urlparse(repo_url)
    if parsed.scheme not in ("http", "https"):
        return {}

    username = username or parsed.username
    password = password or parsed.password
    if not username or not password:
        return {}

    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": f"http.{parsed.scheme}://{_authority(parsed)}/.extraHeader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {token}",
    }


async def run_git(
    args: list[str],
    *,
    cwd: str,
    env: dict[str, str] | None = None,
    stdin: str | bytes | None = None,
    timeout: float | None = None,
) -> tuple[str, str, int]:
    """Run one git command and return (stdout, stderr, exit code), stdout as text.

    The single place where this process spawns git: it installs the child environment,
    refuses an interactive credential prompt, points gpg at the imported signing key and
    masks credentials in the log line.

    With ``timeout`` the command is killed and raises :class:`GitCommandTimeoutError` when
    it does not finish in time. Without it the command waits indefinitely.
    """
    stdout, stderr, code = await _spawn_git(args, cwd=cwd, env=env, stdin=stdin, timeout=timeout)
    return stdout.decode("utf-8").strip(), stderr, code


async def run_git_bytes(
    args: list[str],
    *,
    cwd: str,
    env: dict[str, str] | None = None,
    stdin: str | bytes | None = None,
) -> tuple[bytes, str, int]:
    """As :func:`run_git`, but with stdout unchanged, for blob content."""
    return await _spawn_git(args, cwd=cwd, env=env, stdin=stdin)


async def _spawn_git(
    args: list[str],
    *,
    cwd: str,
    env: dict[str, str] | None = None,
    stdin: str | bytes | None = None,
    timeout: float | None = None,
) -> tuple[bytes, str, int]:
    from opi.core.metrics import track_subprocess_memory

    cmd = ["git", *args]
    logger.debug(f"Running Git command: {_obfuscate_git_command(' '.join(cmd))} in {cwd}")

    cmd_env = child_env()
    # No interactive password prompt: a missing credential has to fail, not wait for a
    # terminal that is not there.
    cmd_env["GIT_TERMINAL_PROMPT"] = "0"
    signing = get_git_signing_identity()
    if signing:
        # git hands its own environment to gpg, so this is how gpg finds the imported key.
        cmd_env["GNUPGHOME"] = signing.gnupg_home
    if env:
        cmd_env.update(env)

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE if stdin is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=cmd_env,
        cwd=cwd,
    )
    payload = stdin.encode("utf-8") if isinstance(stdin, str) else stdin
    try:
        async with track_subprocess_memory("git"):
            stdout, stderr = await asyncio.wait_for(process.communicate(payload), timeout=timeout)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise GitCommandTimeoutError(
            f"{_obfuscate_git_command(' '.join(cmd))} got no response within {timeout}s"
        ) from None
    return stdout, stderr.decode("utf-8").strip(), process.returncode or 0


#: The three file modes a tree can hold: a plain file, an executable file and a symlink,
#: whose "content" is its target path.
FILE_MODE = "100644"
EXEC_MODE = "100755"
SYMLINK_MODE = "120000"


@dataclass(frozen=True)
class FileChange:
    """The new state of a path in a commit: content and mode."""

    content: bytes
    mode: str = FILE_MODE


def _reject_unrepresentable_paths(changes: dict[str, FileChange | None]) -> None:
    """Refuse a path that would split the ``--index-info`` stream of :func:`build_tree_commit`.

    That stream is one line per path, so a newline IN a path makes it a second index line,
    naming its own mode, blob and path. A caller that may only write inside its own scope
    (``GitRepo.write_dir``) could put a path outside it that way: the scope check looks at the
    key, git at the lines. Such a name does not come out of generation but out of a copied
    helm or helmfile tree, which belongs to the user.
    """
    broken = sorted(path for path in changes if "\n" in path or "\r" in path)
    if broken:
        shown = ", ".join(repr(path) for path in broken)
        raise ValueError(f"Pad met een regeleinde kan niet gecommit worden: {shown}")


async def build_tree_commit(
    *,
    cwd: str,
    changes: dict[str, FileChange | None],
    message: str,
    parent: str | None,
    actor: Actor | None = None,
    env: dict[str, str] | None = None,
) -> str | None:
    """Build a commit out of git objects, with an index of its own and without a worktree.

    Every call gets its OWN index (``GIT_INDEX_FILE``), so two concurrent writers never see
    each other's staging and a failure leaves nothing behind. By construction the commit
    cannot hold a path the caller did not name, unlike an ``add -A``.

    ``cwd`` is the repository, a worktree or a bare one. ``changes`` maps a path in the repo
    to its new state, or to None to delete it. ``parent`` None makes a root commit.

    Returns the new commit sha, or None when the tree equals the parent's.
    """
    index_fd, index_path = tempfile.mkstemp(prefix="git-index-", dir=settings.TEMP_DIR)
    os.close(index_fd)
    os.unlink(index_path)  # git wants to create it itself
    command_env = {**(env or {}), "GIT_INDEX_FILE": index_path}

    def check(code: int, stderr: str, operation: str) -> None:
        if code != 0:
            raise RuntimeError(f"Failed to {operation} in {cwd}: {stderr}")

    try:
        if parent:
            _, stderr, code = await run_git(["read-tree", parent], cwd=cwd, env=command_env)
            check(code, stderr, f"read-tree {parent}")

        # Every path in ONE update-index: --index-info also works in a bare repo, where
        # --force-remove refuses ("must be run in a work tree"), and it is one subprocess
        # instead of one per path.
        _reject_unrepresentable_paths(changes)
        index_lines: list[str] = []
        for file_path, change in changes.items():
            if change is None:
                index_lines.append(f"0 {'0' * 40}\t{file_path}")
                continue

            blob, stderr, code = await run_git(["hash-object", "-w", "--stdin"], cwd=cwd, stdin=change.content)
            check(code, stderr, f"hash-object for {file_path}")
            index_lines.append(f"{change.mode} {blob.strip()}\t{file_path}")

        if index_lines:
            _, stderr, code = await run_git(
                ["update-index", "--add", "--index-info"],
                cwd=cwd,
                env=command_env,
                stdin="\n".join(index_lines) + "\n",
            )
            check(code, stderr, f"update-index for {len(index_lines)} path(s)")

        # --missing-ok: the entries we did not touch come from the parent's tree, so their
        # blobs are on the remote, and a blobless copy need not have them. Without it,
        # write-tree checks each blob of every rewritten folder and fetches the missing ones.
        tree, stderr, code = await run_git(["write-tree", "--missing-ok"], cwd=cwd, env=command_env)
        check(code, stderr, "write-tree")
        tree = tree.strip()

        if parent:
            parent_tree, _, code = await run_git(["rev-parse", f"{parent}^{{tree}}"], cwd=cwd, env=env)
            if code == 0 and parent_tree.strip() == tree:
                logger.debug("build_tree_commit: tree unchanged, nothing to commit")
                return None

        # commit-tree reads the identity from the environment first, and this container
        # can have GIT_AUTHOR_NAME set to an empty string, which git rejects. Pass it
        # explicitly so the commit never depends on ambient environment.
        identity = {
            **(env or {}),
            "GIT_AUTHOR_NAME": GIT_COMMIT_AUTHOR_NAME,
            "GIT_AUTHOR_EMAIL": GIT_COMMIT_AUTHOR_EMAIL,
            "GIT_COMMITTER_NAME": GIT_COMMIT_AUTHOR_NAME,
            "GIT_COMMITTER_EMAIL": GIT_COMMIT_AUTHOR_EMAIL,
        }
        commit_args = ["commit-tree", tree]
        signing = get_git_signing_identity()
        if signing:
            commit_args = ["-c", f"gpg.format={GIT_SIGNING_FORMAT}", *commit_args, f"-S{signing.fingerprint}"]
        if parent:
            commit_args += ["-p", parent]
        commit_args += ["-m", _with_actor_trailer(message, actor)]
        commit, stderr, code = await run_git(commit_args, cwd=cwd, env=identity)
        check(code, stderr, "commit-tree")

        logger.debug(f"Built commit {commit.strip()} for {len(changes)} path(s) without a working tree")
        return commit.strip()
    finally:
        if os.path.exists(index_path):
            os.unlink(index_path)


class GitCommandTimeoutError(RuntimeError):
    """A git command got no response within its timeout and was killed.

    Says "got no response", not "not pushed": a push that is killed after the timeout may
    still have landed server-side. That is safe, because the next rollout reads the remote
    again and the commits are idempotent.
    """


class GitRemoteUnavailableError(RuntimeError):
    """The git server could not be reached: DNS, the connection itself, or a 5xx.

    Retried (see ``GitRepo._with_retry``), because the call never arrived at the server or
    was cut off halfway. ``detail`` is git's stderr, so the message after the last attempt
    can still name it.
    """

    def __init__(self, message: str, detail: str = "") -> None:
        super().__init__(message)
        self.detail = detail


class GitPushRejectedError(RuntimeError):
    """The git server refused the push for its own reason: a hook, a protected branch.

    Not the same as :class:`GitPushConflictError`: nothing moved, so reading again and
    pushing again yields the same refusal.
    """


class GitPushConflictError(RuntimeError):
    """A push could not be landed because the remote moved and the local commit
    could not be rebased on top of it (conflict, or retries exhausted).

    Callers that own the semantic change (e.g. "remove deployment X") can catch
    this, reset to the remote via ``reset_to_remote()``, re-apply the change to the
    fresh state, and retry - instead of surfacing a fatal "manual intervention" error.
    """


#: Markers that mean the remote moved under us, so the write must be redone on its tip.
#: "cannot lock ref" is one: the server holds the ref at a commit we did not build on.
_COLLISION_MARKERS = (
    "non-fast-forward",
    "fetch first",
    "incorrect old value",
    "cannot lock ref",
)

#: A push the server refused itself, with the reason in brackets behind it.
_REJECTION_REASON = re.compile(r"!\s+\[[a-z ]*rejected\][^(\n]*\(([^)]*)\)")

#: Userinfo in a URL inside free text. The task service's ``redact_text`` does not know
#: this shape, and ``strip_url_credentials`` only works on a bare URL.
_URL_USERINFO = re.compile(r"(?i)(https?://)[^\s/@'\"]+@")


def git_stderr_detail(stderr: str) -> str:
    """Git's stderr as a single line, with no credentials in a URL left in it."""
    return _URL_USERINFO.sub(r"\1", " ".join(stderr.split()))


def is_git_push_collision(stderr: str) -> bool:
    """Whether the server refused the push because the ref moved under us.

    "failed to push some refs" does not count: git prints that line on EVERY refusal, a
    hook or a protected branch included. "cannot lock ref" does: the server has the ref at
    another commit than the one we built on, which is exactly a collision.
    """
    lowered = stderr.lower()
    return any(marker in lowered for marker in _COLLISION_MARKERS)


def git_push_rejection_reason(stderr: str) -> str:
    """The reason the server gave for a refusal, or the whole stderr if it gave none."""
    match = _REJECTION_REASON.search(stderr)
    return git_stderr_detail(match.group(1) if match else stderr)


class GitConnector:
    """Connector for interacting with Git repositories using GitPython."""

    def __init__(
        self,
        repo_url: str,
        repo_path: str | None = None,
        working_dir: str | None = None,
        branch: str = "main",
        ssh_key_path: str | None = None,
        password: str | None = None,
        username: str | None = None,
        project_name: str | None = None,
        name: str | None = None,
        full_history: bool = False,
    ) -> None:
        """``repo_url`` takes git://, ssh://, https:// and the git@host:path shorthand.

        ``repo_path`` is the path WITHIN the repository, ``branch`` a name without the
        refs/heads/ prefix, and ``password`` may be AGE-encrypted. ``project_name`` only
        enriches the error messages.
        """
        logger.debug(f"Initializing GitConnector {name} for project {project_name} from url {repo_url}")

        # Set authentication properties
        self.ssh_key_path = ssh_key_path
        self.username = username
        self.project_name = project_name
        self.name = name
        # When True, clone with full commit history (blobless partial clone) so file-scoped
        # diffs work (removed-component detection). Default keeps the cheap --depth 1 clone.
        self._full_history = full_history

        # Decrypt password immediately in constructor if provided
        if password:
            logger.debug("Decrypting password during initialization")
            self._decrypted_password = decrypt_password_smart_auto_sync(password)

            # Verify password was actually decrypted (not returned as original encrypted value)
            if self._decrypted_password == password and (password.startswith(("base64+age:", "age:"))):
                logger.error("Password decryption failed, still contains encryption prefix")
                raise ValueError("Failed to decrypt password - Age decryption unsuccessful")

            logger.debug("Password decryption completed during initialization")
        else:
            self._decrypted_password = None

        # Store basic configuration
        self.repo_url = repo_url
        self.repo_path = self._normalize_repo_path(repo_path)

        # Store the branch name without refs/heads/ prefix for consistency
        if branch.startswith("refs/heads/"):
            self.branch = branch[len("refs/heads/") :]
        else:
            self.branch = branch

        logger.debug(f"Using branch: {self.branch}")

        # Set up working directory
        if working_dir:
            self.__working_dir = working_dir
            self.should_cleanup = False
            logger.debug(f"Using provided working directory: {working_dir}")
        else:
            self.__working_dir = tempfile.mkdtemp(prefix="git-repo-", dir=settings.TEMP_DIR)
            self.should_cleanup = True
            logger.debug(f"Created temporary working directory: {self.__working_dir}")

        self._repo_cloned = False
        self._fetched_in_session = False  # Track if we've fetched in this session
        self._closed = False
        self._git_user_configured = False  # Track if git user has been configured

        # Initialize URL parsing immediately (synchronous)
        self.url_config = self._parse_git_url(self.repo_url)
        self._clean_repo_url = strip_url_credentials(self.repo_url)

        logger.debug("GitConnector initialization completed")

    async def __aenter__(self) -> Self:
        await self.ensure_repo_cloned()
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc_val: BaseException | None, exc_tb: TracebackType | None
    ) -> bool:
        await self.close()
        return False

    @property
    def password(self) -> str | None:
        """Get the decrypted password."""
        return self._decrypted_password

    @property
    def repo_url_with_path(self) -> str:
        """The repo URL without credentials, as it goes to git as an argument.

        Authentication goes through the environment; see ``_credential_env``.
        """
        return self._clean_repo_url

    def _parse_git_url(self, url: str) -> dict[str, Any]:
        """The connection details of a git URL as {scheme, host, port, user, path, needs_auth}.

        Supports git://host:port/path (the git daemon, no auth), ssh://user@host:port/path,
        the git@host:path shorthand and https://host/path.
        """
        parsed = urlparse(url)

        if parsed.scheme == "git":
            # Git daemon protocol - no authentication
            return {
                "scheme": "git",
                "host": parsed.hostname,
                "port": parsed.port or 9418,  # Default git daemon port
                "user": None,
                "path": parsed.path,
                "needs_auth": False,
            }
        elif parsed.scheme == "ssh":
            # ssh:// format
            return {
                "scheme": "ssh",
                "host": parsed.hostname,
                "port": parsed.port or 22,
                "user": parsed.username or "git",
                "path": parsed.path,
                "needs_auth": True,
            }
        elif parsed.scheme in ["http", "https"]:
            # HTTP/HTTPS protocol
            return {
                "scheme": parsed.scheme,
                "host": parsed.hostname,
                "port": parsed.port or (443 if parsed.scheme == "https" else 80),
                "user": parsed.username,
                "path": parsed.path,
                "needs_auth": bool(parsed.username or self.password),
            }
        elif not parsed.scheme and "@" in url and ":" in url:
            # SSH shorthand format: git@host:path
            user_host, path = url.split(":", 1)
            if "@" in user_host:
                user, host = user_host.split("@", 1)
            else:
                user, host = None, user_host

            return {"scheme": "ssh", "host": host, "port": 22, "user": user or "git", "path": path, "needs_auth": True}
        else:
            raise ValueError(f"Unsupported Git URL format: {url}")

    def _normalize_repo_path(self, repo_path: str | None) -> str:
        """A repository path from the configuration as a clean, repo-relative path."""
        if not repo_path:
            # None or empty string means root directory
            return "/"

        # Strip whitespace
        repo_path = repo_path.strip()

        if repo_path in ("", "."):
            # Empty string or "." means current/root directory
            return "/"

        # Ensure path starts with "/" for consistency
        if not repo_path.startswith("/"):
            repo_path = f"/{repo_path}"

        # Ensure path doesn't end with "/" unless it's just "/"
        if repo_path != "/" and repo_path.endswith("/"):
            repo_path = repo_path.rstrip("/")

        logger.debug(f"Normalized repo path: '{repo_path}'")
        return repo_path

    def _credential_env(self) -> dict[str, str]:
        """The git config in the environment that authenticates this connector to its remote."""
        return credential_env(self.repo_url, self.username, self._decrypted_password)

    @property
    def ssh_port(self) -> int:
        """Get the SSH port from the parsed URL config."""
        if self.url_config and self.url_config.get("scheme") == "ssh":
            return self.url_config.get("port", 22)
        return 22

    async def _configure_git_user(self) -> None:
        """
        Configure git user identity for commits.
        This method sets up the git user.name and user.email configuration
        required for making commits.
        Only runs once per GitConnector instance.
        """
        if self._git_user_configured:
            logger.debug("Git user already configured, skipping")
            return

        # Configure git user identity for commits
        config_name_cmd = ["config", "user.name", GIT_COMMIT_AUTHOR_NAME]
        stdout, stderr, code = await self._run_git_command(config_name_cmd, cwd=self.__working_dir)
        if code != 0:
            logger.warning(f"Failed to configure git user name: {stderr}")

        config_email_cmd = ["config", "user.email", GIT_COMMIT_AUTHOR_EMAIL]
        stdout, stderr, code = await self._run_git_command(config_email_cmd, cwd=self.__working_dir)
        if code != 0:
            logger.warning(f"Failed to configure git user email: {stderr}")

        signing = get_git_signing_identity()
        if signing:
            # commit.gpgsign also covers the commits a rebase after a push conflict makes
            # again. commit-tree does not read it; that one gets -S in build_commit.
            for config_key, config_value in (
                ("gpg.format", GIT_SIGNING_FORMAT),
                ("user.signingkey", signing.fingerprint),
                ("commit.gpgsign", "true"),
            ):
                stdout, stderr, code = await self._run_git_command(
                    ["config", config_key, config_value], cwd=self.__working_dir
                )
                if code != 0:
                    logger.warning(f"Failed to configure git {config_key}: {stderr}")

        self._git_user_configured = True
        logger.debug("Git user identity configured successfully")

    async def _run_git_command(
        self,
        args: list[str],
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        stdin: str | None = None,
        timeout: float | None = None,
    ) -> tuple[str, str, int]:
        """Run a git command in this connector's working directory, with its credentials.

        Returns (stdout, stderr, exit code). ``env`` is applied last, ``stdin`` is piped to
        the command (``hash-object --stdin`` uses it), and without ``timeout`` the command
        waits indefinitely.

        Raises:
            GitCommandTimeoutError: ``timeout`` passed before the command finished.
        """
        working_dir = cwd or self.__working_dir
        cmd_env = {**self._credential_env(), **self._ssh_env(), **(env or {})}
        try:
            stdout, stderr, code = await run_git(args, cwd=working_dir, env=cmd_env, stdin=stdin, timeout=timeout)
        except GitCommandTimeoutError as error:
            raise self._timeout_error(args, timeout) from error

        if code != 0:
            # Include git server context in error message
            logger.debug(f"Git command failed with code {code} for {self._get_server_context()}: {stderr}")
        else:
            logger.debug("Git command succeeded")

        return stdout, stderr, code

    def _timeout_error(self, args: list[str], timeout: float | None) -> GitCommandTimeoutError:
        """The timeout named with the operation and the server this connector talks to.

        Obfuscated and context-based, so no credentials reach the message a task shows.
        """
        return GitCommandTimeoutError(
            f"git {_obfuscate_git_command(' '.join(args))} on {self._get_server_context()} "
            f"got no response within {timeout}s"
        )

    def _ssh_env(self) -> dict[str, str]:
        """GIT_SSH_COMMAND for an ssh remote with a key, empty for everything else."""
        if not (
            self.url_config
            and self.url_config.get("needs_auth")
            and self.url_config.get("scheme") == "ssh"
            and self.ssh_key_path
        ):
            return {}

        ssh_cmd = f"ssh -i {self.ssh_key_path}"
        port = self.url_config.get("port", 22)
        if port != 22:
            ssh_cmd += f" -p {port}"
        # accept-new trusts an unknown host once and records it, so a changed host key gives
        # an error afterwards instead of silently going on.
        ssh_cmd += f" -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile={SSH_KNOWN_HOSTS_PATH}"
        return {"GIT_SSH_COMMAND": ssh_cmd}

    def _check_git_command_result(self, code: int, stderr: str, operation: str) -> None:
        """Raise a ``RuntimeError`` naming ``operation`` when the command failed."""
        if code != 0:
            server_info = self._get_server_context()
            error_msg = f"Failed to {operation} on {server_info}: {stderr}"
            logger.error(error_msg)
            raise RuntimeError(error_msg)

    def _get_server_context(self) -> str:
        """Host, repository, user and project of this remote, as one line for an error."""
        try:
            context_parts = []

            # Add project context if available
            if self.project_name:
                context_parts.append(f"project={self.project_name}")

            if self.url_config:
                host = self.url_config.get("host", "unknown-host")
                port = self.url_config.get("port")
                scheme = self.url_config.get("scheme", "unknown")

                # Build server description
                server_desc = f"{scheme}://{host}"
                if port and ((scheme == "ssh" and port != 22) or (scheme == "https" and port != 443)):
                    server_desc += f":{port}"
                context_parts.append(f"server={server_desc}")

                if self.username:
                    context_parts.append(f"user={self.username}")

                # Add obfuscated repo path for context
                repo_path = getattr(self, "repo_path", "")
                if repo_path and repo_path != "/":
                    context_parts.append(f"path={repo_path}")

                return f"git [{', '.join(context_parts)}]"
            else:
                # Fallback for unparsed URLs
                obfuscated_url = _obfuscate_git_command(self.repo_url)
                context_parts.append(f"server={obfuscated_url}")
                return f"git [{', '.join(context_parts)}]"
        except AttributeError, KeyError, TypeError, ValueError:
            # If anything goes wrong, return a simple fallback
            project_part = f" for project {self.project_name}" if self.project_name else ""
            return f"git server [URL: {_obfuscate_git_command(self.repo_url)}]{project_part}"

    # Dictionary to cache Git references for each repository URL
    _ref_cache: ClassVar[dict[str, dict[str, str]]] = {}

    async def get_remote_refs(self) -> dict[str, str]:
        """Every remote reference, as name -> commit hash."""
        logger.debug(f"Fetching remote refs from: {_obfuscate_git_command(self.repo_url_with_path)}")

        # Use git ls-remote to get all references
        cmd = ["ls-remote", self.repo_url_with_path]
        stdout, stderr, code = await self._run_git_command(cmd)

        if code != 0:
            server_info = self._get_server_context()
            error_msg = f"Failed to fetch refs from {server_info}: {stderr}"
            logger.error(error_msg)
            raise RuntimeError(error_msg)

        refs = {}
        for line in stdout.splitlines():
            if not line.strip():
                continue

            parts = line.split(maxsplit=1)
            if len(parts) == 2:
                commit_hash, ref_name = parts
                refs[ref_name] = commit_hash
                logger.debug(f"Found ref: {ref_name} -> {commit_hash}")

        # Cache the refs for this repository
        GitConnector._ref_cache[self.repo_url] = refs

        logger.info(f"Found {len(refs)} references")
        return refs

    async def get_latest_commit_hash(self) -> str:
        """The branch's latest commit hash on the remote."""
        logger.debug(f"Getting latest commit hash for branch: {self.branch}")

        # Get all remote refs
        refs = await self.get_remote_refs()

        # Only check refs that start with refs/heads/
        branch_refs = {ref: hash_val for ref, hash_val in refs.items() if ref.startswith("refs/heads/")}
        logger.debug(f"Found {len(branch_refs)} branch references")

        # Look for the specified branch using the branch name from config
        branch_ref = f"refs/heads/{self.branch}"

        if branch_ref in branch_refs:
            commit_hash = branch_refs[branch_ref]
            logger.debug(f"Latest commit hash for {branch_ref}: {commit_hash}")
            return commit_hash

        # Check if HEAD is available and pointing to our branch
        if "HEAD" in refs:
            head_hash = refs["HEAD"]
            logger.debug(f"HEAD hash: {head_hash}")

            # Check if any branch matches the HEAD hash
            for ref, hash_val in branch_refs.items():
                if hash_val == head_hash:
                    logger.debug(f"HEAD is pointing to branch: {ref}")
                    if ref == branch_ref:
                        return head_hash

            # If we can't find a better match, return HEAD
            logger.warning(f"Branch {self.branch} not found, using HEAD")
            return head_hash

        # If we can't find the branch or HEAD, raise an error
        error_msg = f"No commit hash found for branch {self.branch}"
        logger.error(error_msg)
        raise RuntimeError(error_msg)

    async def get_local_commit_hash(self) -> str:
        """The commit hash the local clone is on."""
        logger.debug("Getting local commit hash")

        # Use git rev-parse HEAD to get the current commit hash
        cmd = ["rev-parse", "HEAD"]
        stdout, stderr, code = await self._run_git_command(cmd)

        if code != 0:
            error_msg = f"Failed to get local commit hash: {stderr}"
            logger.error(error_msg)
            raise RuntimeError(error_msg)

        commit_hash = stdout.strip()
        logger.debug(f"Local commit hash: {commit_hash}")
        return commit_hash

    async def ensure_repo_cloned(self) -> None:
        """
        Ensure the repository is cloned for operations that require local access.
        This handles both existing repositories and empty repositories that need branch creation.
        Uses intelligent branch detection to avoid clone failures.
        """
        if self._repo_cloned:
            # Repo already cloned, fetch updates only once per session
            if not self._fetched_in_session:
                await self._fetch_latest()
                self._fetched_in_session = True
            return

        logger.debug(f"Cloning repository to {self.__working_dir}")

        with rollout_phase(PHASE_GIT_PREPARE):
            await self._clone_into_working_dir()

    async def _clone_into_working_dir(self) -> None:
        """Clone the repository into the (empty) working directory, trying strategies in turn."""
        try:
            # Try to detect the remote default branch first
            remote_default_branch = await self._get_remote_default_branch()
            target_branch = self.branch

            # If our configured branch doesn't exist but there's a remote default, try that first
            if remote_default_branch != self.branch:
                logger.debug(
                    f"Detected remote default branch '{remote_default_branch}', configured branch is '{self.branch}'"
                )

            # Blobless partial clone (--filter=blob:none) when the caller needs history for
            # file-scoped diffs (removed-component detection): full commit+tree history,
            # lazy blobs. Falls back to a full clone (strategy 4) if the server lacks
            # partial-clone support (e.g. older Forgejo).
            history_args = ["--filter=blob:none"] if self._full_history else ["--depth", "1"]
            logger.info(
                "Cloning repo %r as %s",
                self.name or self.repo_url,
                "full history (--filter=blob:none)" if self._full_history else "shallow (--depth 1)",
            )

            # Strategy 1: Try cloning with the configured branch
            clone_cmd = [
                "clone",
                "--single-branch",
                "--branch",
                target_branch,
                *history_args,
                self.repo_url_with_path,
                ".",  # Clone to current working directory
            ]

            stdout, stderr, code = await self._run_git_command(clone_cmd, cwd=self.__working_dir)

            if code != 0:
                # Strategy 1 is the clean attempt (correct branch, empty dir), so its
                # error is the real reason a clone can't happen (DNS, auth, host, ...).
                # The later fallbacks clone into the now non-empty working dir and only
                # emit "Cloning into '.'...", so keep strategy 1's message (collapsed to
                # one line) for the final report instead of the last fallback's noise.
                primary_error = " ".join(stderr.split()) or f"git clone exited with code {code}"
                logger.debug(f"Clone with branch '{target_branch}' failed: {stderr}")

                # Strategy 2: If configured branch fails and differs from remote default, try remote default
                if remote_default_branch != target_branch:
                    logger.debug(f"Trying clone with remote default branch: {remote_default_branch}")

                    clone_cmd_default = [
                        "clone",
                        "--single-branch",
                        "--branch",
                        remote_default_branch,
                        *history_args,
                        self.repo_url_with_path,
                        ".",
                    ]

                    stdout, stderr, code = await self._run_git_command(clone_cmd_default, cwd=self.__working_dir)

                    if code == 0:
                        # Successfully cloned with remote default, now ensure correct branch
                        await self._ensure_correct_branch()
                        self._repo_cloned = True
                        logger.debug(
                            f"Repository cloned successfully using remote default branch: {remote_default_branch}"
                        )
                        return

                # Strategy 3: If specific branch cloning fails, try cloning without branch specification
                logger.debug("Trying clone without branch specification")

                clone_cmd_no_branch = ["clone", *history_args, self.repo_url_with_path, "."]

                stdout, stderr, code = await self._run_git_command(clone_cmd_no_branch, cwd=self.__working_dir)

                if code != 0:
                    # Strategy 4: Try cloning without depth restriction (for completely empty repos)
                    logger.debug("Trying clone without depth restriction for empty repository")

                    clone_cmd_no_depth = ["clone", self.repo_url_with_path, "."]

                    stdout, stderr, code = await self._run_git_command(clone_cmd_no_depth, cwd=self.__working_dir)

                    if code != 0:
                        error_msg = f"Failed to clone repository with all strategies: {primary_error}"
                        logger.error(error_msg)
                        raise RuntimeError(error_msg)

                # Repository cloned with default branch, now handle branch creation/switching
                await self._ensure_correct_branch()

            # Mark repository as cloned
            self._repo_cloned = True
            self._fetched_in_session = True  # Mark as fetched since we just cloned
            logger.debug(f"Repository cloned successfully on branch: {self.branch}")
        except Exception as e:
            logger.error(f"Failed to clone repository: {e}")
            raise

    async def _get_remote_default_branch(self) -> str:
        """
        Get the default branch of the remote repository.
        Returns the remote default branch name or falls back to configured branch.
        """
        memo = _remote_default_branches.get(repo_lock_key(self.repo_url))
        if memo is not None:
            logger.debug("Default branch van %s al bekend: %s", self._clean_repo_url, memo)
            return memo

        try:
            # Use git ls-remote to get the default branch without cloning
            ls_remote_cmd = ["ls-remote", "--symref", self.repo_url_with_path, "HEAD"]
            stdout, stderr, code = await self._run_git_command(ls_remote_cmd)

            if code == 0 and stdout:
                # Each line is a ref line: the symref prefix, the ref name, a tab, HEAD.
                for line in stdout.strip().split("\n"):
                    if line.startswith("ref: refs/heads/"):
                        default_branch = line.split("refs/heads/")[1].split("\t")[0]
                        logger.debug(f"Detected remote default branch: {default_branch}")
                        _remote_default_branches[repo_lock_key(self.repo_url)] = default_branch
                        return default_branch

            logger.debug(f"Could not detect remote default branch, using configured: {self.branch}")
            return self.branch

        except (OSError, ValueError, GitCommandTimeoutError, GitRemoteUnavailableError) as e:
            logger.debug(f"Error detecting remote default branch: {e}, using configured: {self.branch}")
            return self.branch

    async def _ensure_correct_branch(self) -> None:
        """
        Ensure we're on the correct branch, creating it if necessary.
        This handles empty repositories or repositories without the desired branch.
        """
        logger.debug(f"Ensuring correct branch: {self.branch}")

        try:
            # Check if the desired branch exists locally
            list_cmd = ["branch", "--list", self.branch]
            stdout, stderr, code = await self._run_git_command(list_cmd, cwd=self.__working_dir)

            branch_exists_locally = code == 0 and self.branch in stdout

            if branch_exists_locally:
                # Branch exists, just switch to it
                checkout_cmd = ["checkout", self.branch]
                stdout, stderr, code = await self._run_git_command(checkout_cmd, cwd=self.__working_dir)
                if code != 0:
                    logger.warning(f"Failed to checkout existing branch {self.branch}: {stderr}")
            else:
                # Check if we have any commits (repository might be empty)
                log_cmd = ["log", "--oneline", "-1"]
                stdout, stderr, code = await self._run_git_command(log_cmd, cwd=self.__working_dir)

                if code != 0:
                    # Repository is empty, create initial commit and branch
                    logger.debug("Repository is empty, creating initial setup")

                    # Create an initial empty commit
                    await self._configure_git_user()
                    commit_cmd = ["commit", "--allow-empty", "--no-verify", "-m", "Initial commit"]
                    stdout, stderr, code = await self._run_git_command(commit_cmd, cwd=self.__working_dir)
                    if code != 0:
                        logger.warning(f"Failed to create initial commit: {stderr}")

                    # Rename the current branch to the desired branch name
                    branch_cmd = ["branch", "-m", self.branch]
                    stdout, stderr, code = await self._run_git_command(branch_cmd, cwd=self.__working_dir)
                    if code != 0:
                        logger.warning(f"Failed to rename branch to {self.branch}: {stderr}")

                    # Push the branch to establish it on the remote
                    push_cmd = ["push", "-u", "origin", self.branch]
                    stdout, stderr, code = await self._run_git_command(push_cmd, cwd=self.__working_dir)
                    if code != 0:
                        logger.warning(f"Failed to push initial branch {self.branch}: {stderr}")
                    else:
                        logger.debug(f"Successfully pushed initial branch {self.branch} to remote")
                else:
                    # Repository has commits but no desired branch, create new branch
                    checkout_cmd = ["checkout", "-b", self.branch]
                    stdout, stderr, code = await self._run_git_command(checkout_cmd, cwd=self.__working_dir)
                    if code != 0:
                        logger.warning(f"Failed to create new branch {self.branch}: {stderr}")

            logger.debug(f"Successfully ensured branch: {self.branch}")
        except (OSError, ValueError, GitCommandTimeoutError, GitRemoteUnavailableError) as e:
            logger.warning(f"Error ensuring correct branch: {e}")
            # Don't raise here, as the repository might still be usable

    async def _fetch_latest(self) -> None:
        """Fetch the latest changes from the repository."""
        if not self._repo_cloned:
            await self.ensure_repo_cloned()
            return

        logger.debug(f"Fetching latest changes for branch: {self.branch}")

        try:
            # Use git fetch command directly
            fetch_cmd = ["fetch", "origin", self.branch]
            stdout, stderr, code = await self._run_git_command(fetch_cmd, cwd=self.__working_dir)
            self._check_git_command_result(code, stderr, "fetch latest changes")

            logger.debug("Latest changes fetched successfully")
            self._fetched_in_session = True
        except Exception as e:
            server_info = self._get_server_context()
            logger.error(f"Failed to fetch latest changes from {server_info}: {e}")
            raise

    async def _pull_latest(self) -> None:
        """Pull the latest changes from the repository."""
        if not self._repo_cloned:
            await self.ensure_repo_cloned()
            return

        logger.debug(f"Pulling latest changes for branch: {self.branch}")

        try:
            # Use git pull command directly
            pull_cmd = ["pull", "origin", self.branch]
            stdout, stderr, code = await self._run_git_command(pull_cmd, cwd=self.__working_dir)
            self._check_git_command_result(code, stderr, "pull latest changes")

            logger.debug("Latest changes pulled successfully")
        except Exception as e:
            server_info = self._get_server_context()
            logger.error(f"Failed to pull latest changes from {server_info}: {e}")
            raise

    async def _rebase_on_remote(self, branch: str | None = None) -> bool:
        """Rebase the local commits onto the remote branch; False on a conflict.

        The way out of a non-fast-forward push rejection.

        Raises:
            RuntimeError: the rebase failed for any reason other than a conflict.
        """
        target_branch = branch or self.branch
        logger.info(f"Rebasing local commits on origin/{target_branch}")

        try:
            # The commits the rebase makes again have to be signed too, and that identity
            # lives in this working directory's git config.
            await self._configure_git_user()

            # First fetch the latest from remote
            fetch_cmd = ["fetch", "origin", target_branch]
            stdout, stderr, code = await self._run_git_command(
                fetch_cmd, cwd=self.__working_dir, timeout=GIT_PUSH_TIMEOUT_SECONDS
            )
            self._check_git_command_result(code, stderr, f"fetch origin/{target_branch}")

            # Rebase on the remote branch
            rebase_cmd = ["rebase", f"origin/{target_branch}"]
            stdout, stderr, code = await self._run_git_command(rebase_cmd, cwd=self.__working_dir)

            if code != 0:
                # Check if this is a conflict
                if "CONFLICT" in stderr or "conflict" in stderr.lower():
                    # A conflict here is a recoverable, expected outcome: we abort cleanly
                    # and the caller (mutate_and_commit_project) resets to the remote and
                    # re-applies. Log at WARNING, not ERROR - the caller decides if the
                    # overall operation ultimately failed.
                    logger.warning(f"Rebase conflict, aborting to re-apply on latest remote: {stderr}")
                    # Abort the rebase to leave repo in clean state
                    abort_cmd = ["rebase", "--abort"]
                    await self._run_git_command(abort_cmd, cwd=self.__working_dir)
                    return False
                else:
                    self._check_git_command_result(code, stderr, f"rebase on origin/{target_branch}")

            logger.info(f"Successfully rebased on origin/{target_branch}")
            return True

        except Exception as e:
            server_info = self._get_server_context()
            logger.error(f"Failed to rebase on {server_info}: {e}")
            raise

    async def reset_to_remote(self, branch: str | None = None) -> None:
        """Discard local commits/changes and hard-reset the working tree to origin/<branch>.

        Used to recover a clean, up-to-date base after a push conflict so a semantic
        change can be re-applied on top of whatever landed on the remote meanwhile.
        """
        await self.ensure_repo_cloned()
        target_branch = branch or self.branch

        fetch_cmd = ["fetch", "origin", target_branch]
        _, stderr, code = await self._run_git_command(fetch_cmd, cwd=self.__working_dir)
        self._check_git_command_result(code, stderr, f"fetch origin/{target_branch}")

        reset_cmd = ["reset", "--hard", f"origin/{target_branch}"]
        _, stderr, code = await self._run_git_command(reset_cmd, cwd=self.__working_dir)
        self._check_git_command_result(code, stderr, f"reset --hard origin/{target_branch}")

        # Drop any untracked leftovers so the tree exactly matches origin.
        await self._run_git_command(["clean", "-fd"], cwd=self.__working_dir)
        logger.info(f"Reset working tree to origin/{target_branch}")

    async def file_changed_between_commits(self, file_path: str, old_commit: str, new_commit: str) -> bool:
        """Whether one file changed between two commits, through ``git diff``."""
        logger.debug(f"Checking if {file_path} changed between {old_commit} and {new_commit}")

        # Combine repo path and file path
        full_path = self._get_full_path(file_path)

        # Use git diff --quiet to check if the file changed between commits
        # Exit code 0 means no changes, 1 means file changed
        diff_cmd = [
            "diff",
            "--quiet",  # Don't produce output, just exit with status code
            old_commit,
            new_commit,
            "--",
            full_path,
        ]

        _, stderr, code = await self._run_git_command(diff_cmd)

        if code == 0:
            # Exit code 0 means no changes
            logger.debug(f"File {full_path} was not changed between commits")
            return False
        elif code == 1:
            # Exit code 1 means changes were detected
            logger.debug(f"File {full_path} was changed between commits")
            return True
        else:
            # Any other exit code indicates an error
            logger.error(f"Failed to check if file changed: {stderr}")
            return False

    def _get_full_path(self, file_path: str) -> str:
        """Combine repo path and file path."""
        if not self.repo_path or self.repo_path == "/":
            return file_path.lstrip("/")

        file_path = file_path.removeprefix("/")

        if self.repo_path.endswith("/"):
            return f"{self.repo_path}{file_path}"
        else:
            return f"{self.repo_path}/{file_path}"

    def get_absolute_file_path(self, file_path: str) -> str:
        """The absolute path of a repo-relative path in the local clone."""
        relative_path = self._get_full_path(file_path)
        return os.path.join(self.__working_dir, relative_path)

    async def read_file_content(self, file_path: str) -> str:
        """The content of a file in the local clone, as text."""
        logger.debug(f"Reading file content for {file_path}")

        # Ensure the repository is cloned
        await self.ensure_repo_cloned()

        # Get the absolute path to the file
        abs_path = self.get_absolute_file_path(file_path)

        try:
            with open(abs_path) as f:
                content = f.read()

            logger.debug(f"Read file content, length: {len(content)}")
            return content
        except Exception as e:
            error_msg = f"Failed to read file {abs_path}: {e}"
            logger.error(error_msg)
            raise

    async def parse_yaml_content(self, content: str) -> dict:
        """Parse YAML text into a dictionary."""
        logger.debug(f"Parsing YAML content of length {len(content)}")
        try:
            result = load_yaml_safe(content)
            logger.debug("YAML parsing successful")
            return result
        except YAMLError as e:
            error_msg = f"Error parsing YAML: {e}"
            logger.error(error_msg)
            raise

    async def clone(self) -> None:
        """
        Clone the repository. This is an atomic operation that ensures the repository is ready for file operations.
        """
        logger.debug("Cloning repository (atomic operation)")
        await self.ensure_repo_cloned()
        logger.debug("Repository cloned successfully")

    async def get_working_dir(self) -> str:
        await self.ensure_repo_cloned()
        return self.__working_dir

    async def add_file(self, file_path: str, content: str, overwrite: bool = True) -> None:
        """Write a repo-relative path and stage it; without ``overwrite`` an existing file fails."""
        logger.debug(f"Adding file to staging: {file_path} (overwrite={overwrite})")

        # Ensure the repository is cloned
        await self.ensure_repo_cloned()

        # Get the absolute path to the file
        abs_path = self.get_absolute_file_path(file_path)

        # Check if file exists when overwrite is False
        if not overwrite and os.path.exists(abs_path):
            raise FileExistsError(f"File {file_path} already exists and overwrite=False")

        # Create directory if it doesn't exist
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)

        # Write the content to the file
        try:
            with open(abs_path, "w") as f:
                f.write(content)
            logger.debug(f"File content written to: {abs_path}")
        except Exception as e:
            error_msg = f"Failed to write file {abs_path}: {e}"
            logger.error(error_msg)
            raise

        # Stage the file
        add_cmd = ["add", self._get_full_path(file_path)]
        stdout, stderr, code = await self._run_git_command(add_cmd, cwd=self.__working_dir)
        self._check_git_command_result(code, stderr, f"stage file {file_path}")

        logger.debug(f"Successfully added file {file_path} to staging area")

    async def delete_file(self, file_path: str) -> None:
        """Delete a repo-relative path and stage the deletion."""
        logger.debug(f"Deleting file: {file_path}")

        # Ensure the repository is cloned
        await self.ensure_repo_cloned()

        # Use git rm -f to remove and stage the deletion (force needed because
        # earlier steps in a workflow may have modified the file)
        rm_cmd = ["rm", "-f", self._get_full_path(file_path)]
        stdout, stderr, code = await self._run_git_command(rm_cmd, cwd=self.__working_dir)
        self._check_git_command_result(code, stderr, f"delete file {file_path}")

        logger.debug(f"Successfully deleted and staged file {file_path}")

    async def read_file(self, file_path: str) -> str:
        """The content of a repo-relative path, as text."""
        logger.debug(f"Reading file: {file_path}")

        # Ensure the repository is cloned
        await self.ensure_repo_cloned()

        # Get the absolute path to the file
        abs_path = self.get_absolute_file_path(file_path)

        try:
            with open(abs_path) as f:
                content = f.read()
            logger.debug(f"Read file content, length: {len(content)}")
            return content
        except Exception as e:
            error_msg = f"Failed to read file {abs_path}: {e}"
            logger.error(error_msg)
            raise

    async def _abort_if_plaintext_secrets_present(self) -> None:
        """
        Refuse to stage/commit plain-text secrets: *.to-sops.yaml or a new .cmp-env.

        A .to-sops.yaml file holds a secret in plain text; it must have been
        replaced by its encrypted .sops.yaml counterpart before any git
        operation. This is defense in depth: even if an upstream caller forgets
        to fail closed on a SOPS error, no code path can leak plaintext secrets
        into the GitOps repository through this connector.

        A ``.cmp-env`` holds the env-vars of a helmfile deployment in plain text
        (RC-333). Those now travel as ``cmp-env.sops.yaml``, so a newly added or
        modified ``.cmp-env`` is refused as well. Removing one is allowed, and an
        untouched one does not block: the working tree is the whole repository and
        the files other projects have not been reprocessed yet must not hold up a
        commit.

        Raises:
            RuntimeError: If one or more plain-text secret files are found.
        """
        working_dir = self.__working_dir
        leftover: list[str] = []
        for root, dirs, files in os.walk(working_dir):
            # Prune, do not skip: without this os.walk descends into .git and reads out
            # every object directory, on the event loop, for every commit.
            dirs[:] = [d for d in dirs if d != ".git"]
            leftover.extend(
                os.path.relpath(os.path.join(root, name), working_dir)
                for name in files
                if name.endswith(TO_SOPS_SUFFIX)
            )

        if leftover:
            leftover.sort()
            server_info = self._get_server_context()
            raise RuntimeError(
                f"Geweigerd om te committen naar {server_info}: "
                f"{len(leftover)} onversleuteld(e) .to-sops.yaml bestand(en) aangetroffen "
                f"in de werkboom: {', '.join(leftover)}. "
                "Dit zou secrets in platte tekst naar git committen; operatie afgebroken."
            )

        await self._abort_if_cmp_env_touched()

    async def _abort_if_cmp_env_touched(self) -> None:
        """Refuse when this commit would add or change a plain-text ``.cmp-env``."""
        # --untracked-files=all: without this git names only the new DIRECTORY, and a
        # .cmp-env in a new deployment directory slips out from under it.
        status_cmd = ["status", "--porcelain", "--untracked-files=all"]
        stdout, stderr, code = await self._run_git_command(status_cmd, cwd=self.__working_dir)
        if code != 0:
            raise RuntimeError(
                f"Kon de status van de werkboom niet opvragen voor de controle op "
                f"{CMP_ENV_PLAINTEXT_FILENAME}: {stderr}. Operatie afgebroken."
            )

        touched = _cmp_env_paths_added_or_modified(stdout)
        if touched:
            server_info = self._get_server_context()
            raise RuntimeError(
                f"Geweigerd om te committen naar {server_info}: "
                f"{len(touched)} toegevoegd(e) of gewijzigd(e) {CMP_ENV_PLAINTEXT_FILENAME}-bestand(en) "
                f"aangetroffen in de werkboom: {', '.join(touched)}. "
                f"De env-vars van een helmfile-deployment horen versleuteld in "
                f"{sops_filenames(CMP_ENV_NAME).encrypted}; operatie afgebroken."
            )

    async def commit_and_push(self, message: str) -> None:
        """Stage everything in the working directory, commit it and push.

        The last git step of a round: it stages new, changed AND deleted files.
        """
        logger.debug(f"Committing and pushing all changes: {message}")

        # Ensure the repository is cloned
        await self.ensure_repo_cloned()

        # Defense in depth: never commit plaintext secrets.
        await self._abort_if_plaintext_secrets_present()

        # Stage all changes in the working directory (new, modified, and deleted files)
        add_cmd = ["add", "-A"]
        stdout, stderr, code = await self._run_git_command(add_cmd, cwd=self.__working_dir)
        logger.debug("All changes staged successfully")

        # Commit and push the changes
        await self.commit_changes(message)
        await self.push_changes()

        logger.info(f"Successfully committed and pushed all changes: {message}")

    async def write_file_without_commit(self, file_path: str, content: str) -> None:
        """Write a repo-relative path without staging or committing it.

        DEPRECATED: use ``add_file`` instead, which stages in the same step.
        """
        logger.debug(f"Writing file without commit: {file_path} (DEPRECATED - use add_file)")

        # Ensure the repository is cloned
        await self.ensure_repo_cloned()

        # Get the absolute path to the file
        abs_path = self.get_absolute_file_path(file_path)

        # Create directory if it doesn't exist
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)

        # Write the content to the file
        try:
            with open(abs_path, "w") as f:
                f.write(content)
            logger.debug(f"File content written to: {abs_path}")
        except Exception as e:
            error_msg = f"Failed to write file {abs_path}: {e}"
            logger.error(error_msg)
            raise

        logger.debug(f"Successfully wrote file {file_path} (not committed)")

    async def create_or_update_file(
        self, file_path: str, content: str, do_commit_and_push: bool, commit_message: str | None = None
    ) -> None:
        """Write a repo-relative path, and commit and push it when ``commit_message`` is given."""
        logger.debug(f"Creating or updating file: {file_path}")

        # Ensure the repository is cloned
        await self.ensure_repo_cloned()

        # Get the absolute path to the file
        abs_path = self.get_absolute_file_path(file_path)

        # Create directory if it doesn't exist
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)

        # Write the content to the file
        try:
            with open(abs_path, "w") as f:
                f.write(content)
            logger.debug(f"File content written to: {abs_path}")
        except Exception as e:
            error_msg = f"Failed to write file {abs_path}: {e}"
            logger.error(error_msg)
            raise

        # Add the file to git
        add_cmd = ["add", self._get_full_path(file_path)]
        stdout, stderr, code = await self._run_git_command(add_cmd, cwd=self.__working_dir)

        if code != 0:
            server_info = self._get_server_context()
            error_msg = f"Failed to add file to git on {server_info}: {stderr}"
            logger.error(error_msg)
            raise RuntimeError(error_msg)

        if do_commit_and_push:
            # Generate commit message if not provided
            if commit_message is None:
                commit_message = f"Update {file_path}"

            # Commit and push the changes
            await self.commit_changes(commit_message)
            await self.push_changes()

            logger.info(f"Successfully created/updated file {file_path} and pushed to remote")

    async def file_exists(self, file_path: str) -> bool:
        """Whether a repo-relative path exists in the local clone."""
        await self.ensure_repo_cloned()
        abs_path = self.get_absolute_file_path(file_path)
        return os.path.exists(abs_path)

    async def has_changes(self) -> bool:
        """Whether the working directory holds anything uncommitted."""
        await self.ensure_repo_cloned()

        # Use git status --porcelain to check for changes
        status_cmd = ["status", "--porcelain"]
        stdout, stderr, code = await self._run_git_command(status_cmd, cwd=self.__working_dir)

        if code != 0:
            logger.error(f"Failed to check git status: {stderr}")
            return False

        has_changes = bool(stdout.strip())
        logger.debug(f"Repository has changes: {has_changes}")
        return has_changes

    async def add_files(self, files_or_paths: list[str]) -> bool:
        """Stage these files and directories; False when one of them could not be added."""
        await self.ensure_repo_cloned()

        for file_path in files_or_paths:
            add_cmd = ["add", file_path]
            stdout, stderr, code = await self._run_git_command(add_cmd, cwd=self.__working_dir)

            if code != 0:
                logger.error(f"Failed to add {file_path} to git: {stderr}")
                return False

        logger.debug(f"Successfully added {len(files_or_paths)} files to git staging")
        return True

    async def path_unchanged_since(self, revision: str, path: str) -> bool:
        """Whether ``path`` at ``revision`` is byte for byte what it is in HEAD now.

        This proves that someone else (ArgoCD) already applied exactly these files, even when
        their revision is older than ours: on a repo several projects share, the tip always
        runs ahead of the last comparison of ONE application, and then "is our commit an
        ancestor" says nothing about our own directory.

        A revision we do not have locally, and anything else that goes wrong, gives False:
        nothing is proven then and the caller does its full work.
        """
        if not revision or not path:
            return False
        _, _, code = await self._run_git_command(
            ["diff", "--quiet", revision, "HEAD", "--", path], cwd=self.__working_dir
        )
        return code == 0

    async def _has_commits_to_push(self, target_branch: str) -> bool:
        """Whether anything local is missing from the remote-tracking ref.

        A push that brings nothing still costs a full connection and a negotiation. Only an
        EMPTY answer is proof: when the remote is ahead of us, this list still counts our own
        commits and we push as always.
        """
        stdout, _, code = await self._run_git_command(
            ["rev-list", "--count", f"refs/remotes/origin/{target_branch}..HEAD"], cwd=self.__working_dir
        )
        if code != 0:
            # No remote-tracking ref (a new branch), so there is nothing to prove.
            return True
        return stdout.strip() != "0"

    async def commit_changes(self, message: str) -> bool:
        """Commit everything in the working directory; False when there was nothing to commit.

        A caller that knows it changed nothing can skip work only a change needs (see
        ``wait_for_project_level_application``).

        Raises:
            RuntimeError: staging or the commit itself failed.
        """
        await self.ensure_repo_cloned()

        # Defense in depth: never commit plaintext secrets.
        await self._abort_if_plaintext_secrets_present()

        # Stage all changes in the working directory (new, modified, and deleted files)
        add_cmd = ["add", "-A"]
        stdout, stderr, code = await self._run_git_command(add_cmd, cwd=self.__working_dir)
        logger.debug("All changes staged successfully")

        await self._configure_git_user()

        commit_cmd = ["commit", "--no-verify", "-m", _with_actor_trailer(message)]
        stdout, stderr, code = await self._run_git_command(commit_cmd, cwd=self.__working_dir)

        if code != 0:
            if "nothing to commit" in stdout or "nothing to commit" in stderr:
                logger.debug("No changes to commit")
                return False
            else:
                server_info = self._get_server_context()
                error_msg = f"Failed to commit changes to {server_info}: {stderr}"
                logger.error(error_msg)
                raise RuntimeError(error_msg)

        logger.debug(f"Successfully committed changes: {message}")
        return True

    async def push_changes(self, branch: str | None = None, max_retries: int = 5, allow_rebase: bool = True) -> None:
        """Push the committed changes, fetching and rebasing once a push is non-fast-forward.

        ``allow_rebase=False`` raises GitPushConflictError on a non-fast-forward instead:
        the internal rebase text-merges the remote's version with ours and pushes the
        *merged* result, which nobody validated, and two edits that are each valid can merge
        into an invalid project file. It also makes the caller's post-push state diverge from
        git, because the caller still holds its pre-merge version. ProjectStore therefore
        pushes with allow_rebase=False, re-reads, re-applies and re-validates.

        Raises:
            RuntimeError: the push failed after every retry, or the rebase hit a conflict.
        """
        await self.ensure_repo_cloned()

        target_branch = branch or self.branch

        if not await self._has_commits_to_push(target_branch):
            logger.debug("Niets te pushen naar %s: lokaal staat niets voor op de remote", target_branch)
            return

        # Serialize the whole attempt (fetch+rebase+push) against every other pusher
        # to this ref in the process, so a waiter rebases onto the winner and pushes
        # without a concurrent competitor invalidating it. See _push_locks.
        async with push_lock_for(self.repo_url, target_branch):
            with rollout_phase(PHASE_GIT_PUSH):
                await self._push_attempts(target_branch, max_retries, allow_rebase)

    async def _push_attempts(self, target_branch: str, max_retries: int, allow_rebase: bool) -> None:
        """The attempts themselves; see :meth:`push_changes`. Runs under the push lock."""
        for attempt in range(max_retries):
            push_cmd = ["push", "origin", target_branch]
            # A timeout is not a non-fast-forward, so GitCommandTimeoutError leaves this
            # loop at once: no rebase, no retry, and the push lock is held for at most one
            # timeout.
            stdout, stderr, code = await self._run_git_command(
                push_cmd, cwd=self.__working_dir, timeout=GIT_PUSH_TIMEOUT_SECONDS
            )

            if code == 0:
                logger.debug(f"Successfully pushed changes to {target_branch}")
                return

            if not is_git_push_collision(stderr):
                # The server refused the push for its own reason (a hook, a protected
                # branch): rebasing onto its tip changes nothing, so it would refuse the
                # retry just as well. Anything else is not a push verdict at all.
                if "rejected" in stderr.lower():
                    server_info = self._get_server_context()
                    raise GitPushRejectedError(
                        f"De git-server weigerde de push naar {target_branch} op {server_info}: "
                        f"{git_push_rejection_reason(stderr)}."
                    )
                self._check_git_command_result(code, stderr, f"push changes to {target_branch}")

            if not allow_rebase:
                # Hand the divergence to the caller instead of resolving it with a blind
                # text merge. The caller re-reads, re-applies and re-validates.
                server_info = self._get_server_context()
                raise GitPushConflictError(
                    f"Cannot push to {target_branch} on {server_info}: remote has moved "
                    f"(non-fast-forward). Caller must re-read and re-apply."
                )

            # Non-fast-forward error - try to rebase and retry
            if attempt < max_retries - 1:
                logger.warning(
                    f"Push rejected (non-fast-forward), attempting rebase and retry "
                    f"(attempt {attempt + 1}/{max_retries})"
                )

                rebase_success = await self._rebase_on_remote(target_branch)
                if not rebase_success:
                    server_info = self._get_server_context()
                    raise GitPushConflictError(
                        f"Cannot push to {target_branch} on {server_info}: "
                        f"Remote has conflicting changes that cannot be automatically merged."
                    )

                logger.info("Rebase successful, retrying push...")
            else:
                # Last attempt failed
                server_info = self._get_server_context()
                raise GitPushConflictError(
                    f"Failed to push changes to {target_branch} on {server_info} after {max_retries} attempts: {stderr}"
                )

    async def get_previous_file_content(self, file_path: str, commits_back: int = 1) -> str | None:
        """
        Get the content of a file from a previous commit that changed THAT file.

        File-scoped, not branch-scoped: uses ``git log -- <file>`` to find the commit that
        previously changed this specific file, so it is correct regardless of how many
        unrelated commits (other projects) landed in between. Branch-scoped ``HEAD~N`` was
        wrong for the multi-project projects repo (an intervening commit for another project
        made ``HEAD~1`` not the previous version of this file).

        Requires the clone to carry history (GitConnector(full_history=True), i.e. a blobless
        ``--filter=blob:none`` clone). On a ``--depth 1`` clone ``git log`` sees only one
        commit and this returns None.

        This reconstructs from git what the mutation already knew. Passing the pre-change
        project state from the mutation (add/delete/update component, wizard save) into
        generation would be robuster; tracked in #161. This git-diff path stays while
        changes still arrive via raw git.

        ``commits_back`` counts file-touching commits, so 1 is the previous version. None
        when there is no such previous version.
        """
        try:
            await self.ensure_repo_cloned()
            clean_file_path = file_path.lstrip("/")

            # The last (commits_back + 1) commits that TOUCHED this file, newest first.
            stdout, stderr, returncode = await self._run_git_command(
                ["log", f"-n{commits_back + 1}", "--format=%H", "--", clean_file_path],
                cwd=self.__working_dir,
            )
            if returncode != 0:
                logger.debug(f"git log failed for {file_path}: {stderr}")
                return None

            hashes = [h for h in stdout.strip().splitlines() if h]
            if len(hashes) <= commits_back:
                logger.debug(
                    f"No commit {commits_back} back that changed {file_path} (only {len(hashes)} in file history)"
                )
                return None
            prev_commit = hashes[commits_back]

            stdout, stderr, returncode = await self._run_git_command(
                ["show", f"{prev_commit}:{clean_file_path}"], cwd=self.__working_dir
            )
            if returncode == 0:
                logger.debug(f"Retrieved previous version of {file_path} from {prev_commit}")
                return stdout
            logger.debug(f"File {file_path} does not exist in {prev_commit}: {stderr}")
            return None

        except (OSError, ValueError, GitCommandTimeoutError, GitRemoteUnavailableError) as e:
            logger.warning(f"Error retrieving previous file content for {file_path}: {e}")
            return None

    async def show_file_at(self, ref: str, file_path: str) -> str | None:
        """Return the content of a file as of ``ref``, or None if absent there.

        Local-only (``git show``), so it is cheap on a warm clone that carries
        history. Used by ProjectStore.read_at to reconstruct a past version.
        """
        await self.ensure_repo_cloned()
        clean_file_path = self._get_full_path(file_path)

        stdout, stderr, code = await self._run_git_command(["show", f"{ref}:{clean_file_path}"])
        if code == 0:
            return stdout
        logger.debug(f"File {clean_file_path} not present at {ref}: {stderr}")
        return None

    async def get_blob_sha(self, file_path: str, ref: str = "HEAD") -> str | None:
        """Return the git blob SHA of ``file_path`` at ``ref``, or None if absent.

        The blob names the file's exact content, so it identifies a version of ONE
        project file rather than of the whole repository: unrelated commits for other
        projects leave it unchanged. Unlike a plain hash it is also a git object, so
        ``read_blob`` can hand the very content back later.
        """
        await self.ensure_repo_cloned()
        clean_file_path = self._get_full_path(file_path)

        stdout, stderr, code = await self._run_git_command(["rev-parse", f"{ref}:{clean_file_path}"])
        if code == 0:
            return stdout.strip()
        logger.debug(f"No blob for {clean_file_path} at {ref}: {stderr}")
        return None

    async def read_blob(self, blob_sha: str) -> str | None:
        """Return the content of a blob object, or None if it is not in this clone.

        Blobs stay reachable through the history of the branch, so a version read
        earlier can still be reconstructed after later commits landed on top.
        """
        await self.ensure_repo_cloned()

        stdout, stderr, code = await self._run_git_command(["cat-file", "blob", blob_sha])
        if code == 0:
            return stdout
        logger.debug(f"Blob {blob_sha} not available: {stderr}")
        return None

    async def list_file_revisions(self, file_path: str, limit: int = 50) -> list[dict[str, str]]:
        """Return the commits that touched ``file_path``, newest first.

        File-scoped (``git log -- <file>``), so unrelated commits for other
        projects in the shared repo are not reported as revisions of this file.
        Each entry has: ref, author, timestamp (ISO 8601), message.
        """
        await self.ensure_repo_cloned()
        clean_file_path = self._get_full_path(file_path)

        # Unit separator between fields, record separator between commits, so a
        # multi-line commit message cannot be misparsed as another revision.
        stdout, stderr, code = await self._run_git_command(
            ["log", f"-n{limit}", "--format=%H%x1f%an%x1f%aI%x1f%s%x1e", "--", clean_file_path]
        )
        if code != 0:
            logger.debug(f"git log failed for {clean_file_path}: {stderr}")
            return []

        revisions: list[dict[str, str]] = []
        for record in stdout.split("\x1e"):
            record = record.strip()
            if not record:
                continue
            parts = record.split("\x1f")
            if len(parts) != 4:
                continue
            revisions.append({"ref": parts[0], "author": parts[1], "timestamp": parts[2], "message": parts[3]})
        return revisions

    async def last_modified_per_file(self, subdir: str) -> dict[str, str]:
        """When each file under ``subdir`` was last touched, as ISO 8601, newest wins.

        One ``git log`` pass over the whole history rather than one per file: an
        overview page asks this for every project it lists, and a per-file walk would
        make the page cost a subprocess per row.

        Merge commits list no files under ``--name-only`` and are therefore skipped,
        which is what we want -- a merge is not an edit of the file. Keys are
        repo-relative paths; a file that only ever appeared in a merge is absent, so
        callers must treat a missing key as "unknown" and not as "never changed".
        """
        await self.ensure_repo_cloned()
        prefix = self._get_full_path(subdir).strip("/")

        # Record separator before each commit's timestamp, so the file list that
        # follows can be attributed to it without counting lines.
        stdout, stderr, code = await self._run_git_command(
            ["log", "--pretty=format:%x1e%aI", "--name-only", "--", prefix]
        )
        if code != 0:
            logger.debug(f"git log failed for {prefix}: {stderr}")
            return {}

        last_modified: dict[str, str] = {}
        for record in stdout.split("\x1e"):
            lines = [line.strip() for line in record.splitlines() if line.strip()]
            if not lines:
                continue
            timestamp, paths = lines[0], lines[1:]
            for path in paths:
                # Newest first, so the first sighting of a path is its last change.
                last_modified.setdefault(path, timestamp)
        return last_modified

    async def list_changed_files(self, old_commit: str, new_commit: str) -> list[str]:
        """Return repo-relative paths changed between two commits.

        Drives the incremental reconcile: only the files that actually changed
        are re-read, instead of re-reading every project file.
        """
        await self.ensure_repo_cloned()

        stdout, stderr, code = await self._run_git_command(["diff", "--name-only", old_commit, new_commit])
        if code != 0:
            logger.warning(f"git diff --name-only {old_commit}..{new_commit} failed: {stderr}")
            return []

        return [line.strip() for line in stdout.splitlines() if line.strip()]

    async def build_commit(
        self,
        changes: dict[str, str | None],
        message: str,
        parent: str | None = None,
        actor: Actor | None = None,
    ) -> str | None:
        """Build a commit object directly, without touching the working tree or shared index.

        Thin wrapper around :func:`build_tree_commit`: it resolves the paths against this
        connector's ``repo_path`` and defaults the parent to HEAD. The plumbing itself is
        shared with :class:`opi.connectors.git_repo.GitRepo`.

        This deliberately replaces the ``write file`` + ``git add -A`` + ``commit`` path for
        project files. ``git add -A`` stages *everything* in the working tree, which on a
        shared warm copy means one writer's commit can carry another writer's half-written
        file, or a pending deletion, to the remote.

        ``changes`` maps a repo-relative path to its new content, or to None to delete it.
        ``parent`` defaults to the current HEAD, and None without a HEAD makes a root commit.
        ``actor`` goes into the trailer; without it the one from the context.

        Returns the new commit sha, or None when the resulting tree is identical to the
        parent's. The branch ref is NOT moved: call ``set_branch_ref``.
        """
        await self.ensure_repo_cloned()

        if parent is None:
            stdout, _, code = await self._run_git_command(["rev-parse", "HEAD"])
            parent = stdout.strip() if code == 0 else None

        return await build_tree_commit(
            cwd=await self.get_working_dir(),
            changes={
                self._get_full_path(path): None if content is None else FileChange(content.encode("utf-8"))
                for path, content in changes.items()
            },
            message=message,
            parent=parent,
            actor=actor,
            env=self._credential_env(),
        )

    async def set_branch_ref(self, commit: str, branch: str | None = None) -> None:
        """Point the local branch at ``commit``.

        Separated from ``build_commit`` on purpose: the caller moves the ref only when it
        intends to publish, and rolling back after a failed push is just moving it back --
        no working tree to clean up.
        """
        target_branch = branch or self.branch
        _, stderr, code = await self._run_git_command(
            ["update-ref", f"refs/heads/{target_branch}", commit], cwd=self.__working_dir
        )
        self._check_git_command_result(code, stderr, f"update-ref refs/heads/{target_branch}")

    async def sync_worktree_to_head(self) -> None:
        """Make the working copy match HEAD again after a plumbing commit.

        Writes bypass the working tree, so after moving the ref the checkout is stale.
        Keeping it in step preserves the invariant that the warm copy mirrors HEAD, which
        the read paths and any leftover working-tree reader rely on.
        """
        _, stderr, code = await self._run_git_command(["reset", "--hard", "HEAD"], cwd=self.__working_dir)
        self._check_git_command_result(code, stderr, "reset --hard HEAD")

    async def get_remote_commit_hash(self, branch: str | None = None) -> str | None:
        """The remote branch tip, without fetching. None when it cannot be determined.

        ``ls-remote`` asks the server for one ref and transfers no objects, so it is
        the cheap way to answer "has anything changed?" before paying for a fetch
        plus a working-tree reset. Measured at roughly 60ms against the sandbox,
        against seconds for the full reconcile it guards.
        """
        target_branch = branch or self.branch

        stdout, stderr, code = await self._run_git_command(["ls-remote", "origin", f"refs/heads/{target_branch}"])
        if code != 0:
            logger.warning(f"Could not read remote hash for {target_branch}: {stderr}")
            return None

        line = stdout.strip()
        if not line:
            return None
        return line.split()[0]

    async def count_unpushed_commits(self, branch: str | None = None) -> int:
        """How many commits the local branch has that origin/<branch> does not.

        Local-only (no fetch), because the case this exists for is a local commit
        that was built and ref'd but never acknowledged by a push -- for instance
        when a rollback could not complete. Returns 0 when the remote-tracking ref
        is unknown, so a fresh clone does not read as diverged.
        """
        target_branch = branch or self.branch

        stdout, stderr, code = await self._run_git_command(
            ["rev-list", "--count", f"origin/{target_branch}..HEAD"], cwd=self.__working_dir
        )
        if code != 0:
            logger.debug(f"Could not count unpushed commits for {target_branch}: {stderr}")
            return 0
        try:
            return int(stdout.strip())
        except ValueError:
            return 0

    async def close(self) -> None:
        """Clean up resources. Safe to call multiple times."""
        if self._closed:
            return
        self._closed = True
        # The working directory always goes at close, even when should_cleanup is False.
        # has_changes() clones again and would throw its clone error over the task's error.
        if self._repo_cloned and await self.has_changes():
            logger.warning(
                f"Repository {self.name} for project {self.project_name} has uncommitted changes but is being closed."
            )
        if self.__working_dir and os.path.exists(self.__working_dir):
            logger.debug(
                f"Closing GitConnector {self.name} for project {self.project_name}; removing temporary directory: {self.__working_dir}"
            )
            shutil.rmtree(self.__working_dir, ignore_errors=True)


async def create_git_connector_from_repo_config(
    repo_config: dict[str, Any], full_history: bool = False, working_dir: str | None = None
) -> GitConnector:
    return GitConnector(
        repo_url=repo_config["url"],
        repo_path=repo_config.get("path"),
        branch=repo_config["branch"],
        username=repo_config.get("username"),
        password=repo_config.get("password"),
        ssh_key_path=repo_config.get("ssh_key_path"),
        project_name=repo_config.get("project_name"),
        name=repo_config.get("name"),
        full_history=full_history,
        working_dir=working_dir,
    )


async def create_git_connector_for_project_files(
    project_name: str, full_history: bool = True, working_dir: str | None = None
) -> GitConnector:
    # full_history defaults to True: ANY project-files connector can end up feeding
    # analyze_project_changes (the file-scoped diff that detects removed components/
    # deployments), and a --depth 1 clone has no previous commit so that diff silently
    # finds nothing and prune never runs. The projects repo is shared across all
    # projects, so the "previous" commit for a file is rarely HEAD~1; we need real
    # history. Blobless (--filter=blob:none) keeps commits+trees, lazy-fetches blobs.
    projects_repo_config = {
        "url": settings.GIT_PROJECTS_SERVER_URL,
        "branch": settings.GIT_PROJECTS_SERVER_BRANCH,
        "path": settings.GIT_PROJECTS_SERVER_REPO_PATH,
        "password": settings.GIT_PROJECTS_SERVER_PASSWORD,
        "username": settings.GIT_PROJECTS_SERVER_USERNAME,
        "project_name": project_name,
        "name": "projects",
    }
    return await create_git_connector_from_repo_config(
        projects_repo_config, full_history=full_history, working_dir=working_dir
    )
