"""
Git connector for polling repository changes using GitPython.
"""

import asyncio
import logging
import os
import re
import shutil
import tempfile
from collections.abc import Callable, Coroutine
from typing import Any
from urllib.parse import urlparse

from ruamel.yaml import YAML

from opi.core.config import settings
from opi.utils.age import decrypt_password_smart_auto_sync

logger = logging.getLogger(__name__)


def _obfuscate_git_command(cmd_str: str) -> str:
    """
    Obfuscate sensitive information in Git commands for logging.

    Args:
        cmd_str: The command string to obfuscate

    Returns:
        Command string with sensitive information replaced by asterisks
    """
    # Pattern to match URLs with credentials (https://user:password@host or https://token@host)
    # This handles various formats including GitHub personal access tokens (ghp_*)
    url_pattern = r"https://([^:/@\s]+):([^@\s]+)@"

    # Replace with obfuscated version
    obfuscated = re.sub(url_pattern, r"https://\1:***@", cmd_str)

    # Also handle the case where there's just a token without username (https://token@host)
    token_pattern = r"https://([^@\s]*ghp_[^@\s]+)@"
    obfuscated = re.sub(token_pattern, r"https://***@", obfuscated)

    return obfuscated


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
    ):
        """
        Initialize the Git connector.

        Args:
            repo_url: URL for the Git repository (git://, ssh://, https://, git@host:path)
            repo_path: Path within the repository (e.g., "/subdir/project")
            working_dir: Optional working directory for the local clone
            branch: Branch name to monitor (without refs/heads/ prefix)
            ssh_key_path: Optional path to SSH private key for authentication
            password: Optional password for authentication (HTTPS, may be encrypted)
            username: Optional username for authentication (HTTPS)
            project_name: Optional project name for better error context reporting
        """
        logger.debug(f"Initializing GitConnector {name} for project {project_name} from url {repo_url}")

        # Set authentication properties
        self.ssh_key_path = ssh_key_path
        self.username = username
        self.project_name = project_name
        self.name = name

        # Decrypt password immediately in constructor if provided
        if password:
            logger.debug("Decrypting password during initialization")
            self._decrypted_password = decrypt_password_smart_auto_sync(password)

            # Verify password was actually decrypted (not returned as original encrypted value)
            if self._decrypted_password == password and (
                password.startswith("base64+age:") or password.startswith("age:")
            ):
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
        # TODO: rethink cleanup logic!
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
        self._git_user_configured = False  # Track if git user has been configured

        # Initialize URL parsing immediately (synchronous)
        self.url_config = self._parse_git_url(self.repo_url)

        logger.debug("GitConnector initialization completed")

    async def __aenter__(self):
        await self.ensure_repo_cloned()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()
        return False

    @property
    def password(self) -> str | None:
        """Get the decrypted password."""
        return self._decrypted_password

    @property
    def repo_url_with_path(self) -> str:
        """Get the repository URL with credentials embedded for HTTPS URLs."""
        return self._construct_repo_url_with_credentials()

    def _parse_git_url(self, url: str) -> dict[str, Any]:
        """
        Parse a Git URL and return connection details.

        Supports:
        - git://host:port/path (Git daemon, no auth)
        - ssh://user@host:port/path (SSH with auth)
        - git@host:path (SSH shorthand)
        - https://host/path (HTTPS)

        Args:
            url: Git URL to parse

        Returns:
            Dict with keys: scheme, host, port, user, path, needs_auth
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
        """
        Normalize repository path to handle different configurations.

        Args:
            repo_path: Raw repository path from configuration

        Returns:
            Normalized repository path
        """
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

    def _construct_repo_url_with_credentials(self) -> str:
        """
        Construct the repository URL with embedded credentials for HTTPS URLs.

        Returns:
            Repository URL with credentials and path
        """
        base_url = self.repo_url

        # For HTTPS URLs, embed credentials if provided
        if self.url_config and self.url_config.get("scheme") == "https" and (self.username or self._decrypted_password):
            from urllib.parse import urlparse, urlunparse

            parsed = urlparse(self.repo_url)

            # Extract existing username from URL if present
            existing_username = parsed.username

            # Use provided credentials or fall back to existing ones
            username = self.username or existing_username
            # Use decrypted password if available, otherwise don't embed credentials
            password = self._decrypted_password

            # Only embed credentials if both username and password are valid strings
            if username and password and isinstance(username, str) and isinstance(password, str):
                # Construct new netloc with credentials
                netloc = f"{username}:{password}@{parsed.hostname}"
                if parsed.port:
                    netloc += f":{parsed.port}"

                # Reconstruct URL with credentials
                base_url = urlunparse(
                    (parsed.scheme, netloc, parsed.path, parsed.params, parsed.query, parsed.fragment)
                )

        # Return the base URL without the repo path
        # The repo path is used after cloning to navigate to subdirectories
        return base_url

    @property
    def ssh_port(self) -> int:
        """Get the SSH port from the parsed URL config."""
        if self.url_config and self.url_config.get("scheme") in ["ssh"]:
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
        config_name_cmd = ["config", "user.name", "Operations Manager"]
        stdout, stderr, code = await self._run_git_command(config_name_cmd, cwd=self.__working_dir)
        if code != 0:
            logger.warning(f"Failed to configure git user name: {stderr}")

        config_email_cmd = ["config", "user.email", "operations-manager@example.com"]
        stdout, stderr, code = await self._run_git_command(config_email_cmd, cwd=self.__working_dir)
        if code != 0:
            logger.warning(f"Failed to configure git user email: {stderr}")

        self._git_user_configured = True
        logger.debug("Git user identity configured successfully")

    async def _run_git_command(
        self, args: list[str], env: dict[str, str] | None = None, cwd: str | None = None
    ) -> tuple[str, str, int]:
        """
        Run a Git command directly with subprocess.

        This is a lightweight alternative to cloning the full repository.

        Args:
            args: List of Git command arguments
            env: Optional environment variables
            cwd: Optional working directory

        Returns:
            Tuple of (stdout, stderr, return_code)
        """
        cmd = ["git"] + args
        cmd_str = " ".join(cmd)
        working_dir = cwd or self.__working_dir
        logger.debug(f"Running Git command: {_obfuscate_git_command(cmd_str)} in {working_dir}")

        # Set up environment
        cmd_env = os.environ.copy()
        if env:
            cmd_env.update(env)

        # Configure SSH if using SSH URL and credentials are provided
        if self.url_config and self.url_config.get("needs_auth") and self.url_config.get("scheme") in ["ssh"]:
            if self.ssh_key_path:
                ssh_cmd = f"ssh -i {self.ssh_key_path}"

                # Add port if not default
                port = self.url_config.get("port", 22)
                if port != 22:
                    ssh_cmd += f" -p {port}"

                # Add options for StrictHostKeyChecking
                ssh_cmd += " -o StrictHostKeyChecking=no"

                logger.debug(f"Using SSH command: {ssh_cmd}")
                cmd_env["GIT_SSH_COMMAND"] = ssh_cmd

        # Create process
        process = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=cmd_env, cwd=working_dir
        )

        # Wait for command to complete
        stdout, stderr = await process.communicate()
        stdout_str = stdout.decode("utf-8").strip()
        stderr_str = stderr.decode("utf-8").strip()

        if process.returncode != 0:
            # Include git server context in error message
            server_info = self._get_server_context()
            logger.debug(f"Git command failed with code {process.returncode} for {server_info}: {stderr_str}")
        else:
            logger.debug("Git command succeeded")

        return stdout_str, stderr_str, process.returncode or 0

    def _check_git_command_result(self, code: int, stderr: str, operation: str) -> None:
        """
        Check git command result and raise RuntimeError if failed.

        Args:
            code: Return code from git command
            stderr: Standard error output
            operation: Description of the operation (e.g., "stage all changes")

        Raises:
            RuntimeError: If git command failed
        """
        if code != 0:
            server_info = self._get_server_context()
            error_msg = f"Failed to {operation} on {server_info}: {stderr}"
            logger.error(error_msg)
            raise RuntimeError(error_msg)

    def _get_server_context(self) -> str:
        """
        Get contextual information about the git server for error reporting.

        Returns:
            String describing the git server context (host, repository, user, project)
        """
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
        except Exception:
            # If anything goes wrong, return a simple fallback
            project_part = f" for project {self.project_name}" if self.project_name else ""
            return f"git server [URL: {_obfuscate_git_command(self.repo_url)}]{project_part}"

    # Dictionary to cache Git references for each repository URL
    _ref_cache: dict[str, dict[str, str]] = {}

    async def get_remote_refs(self) -> dict[str, str]:
        """
        Get all remote references from the Git repository.

        Returns:
            Dictionary mapping reference names to commit hashes
        """
        logger.debug(f"Fetching remote refs from: {self.repo_url_with_path}")

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
        """
        Get the latest commit hash for the branch.

        Returns:
            Latest commit hash
        """
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
        """
        Get the current commit hash from the local clone.

        Returns:
            Current local commit hash
        """
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

        try:
            # Try to detect the remote default branch first
            remote_default_branch = await self._get_remote_default_branch()
            target_branch = self.branch

            # If our configured branch doesn't exist but there's a remote default, try that first
            if remote_default_branch != self.branch:
                logger.debug(
                    f"Detected remote default branch '{remote_default_branch}', configured branch is '{self.branch}'"
                )

            # Strategy 1: Try cloning with the configured branch
            clone_cmd = [
                "clone",
                "--single-branch",
                "--branch",
                target_branch,
                "--depth",
                "1",
                self.repo_url_with_path,
                ".",  # Clone to current working directory
            ]

            stdout, stderr, code = await self._run_git_command(clone_cmd, cwd=self.__working_dir)

            if code != 0:
                logger.debug(f"Clone with branch '{target_branch}' failed: {stderr}")

                # Strategy 2: If configured branch fails and differs from remote default, try remote default
                if remote_default_branch != target_branch:
                    logger.debug(f"Trying clone with remote default branch: {remote_default_branch}")

                    clone_cmd_default = [
                        "clone",
                        "--single-branch",
                        "--branch",
                        remote_default_branch,
                        "--depth",
                        "1",
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

                clone_cmd_no_branch = ["clone", "--depth", "1", self.repo_url_with_path, "."]

                stdout, stderr, code = await self._run_git_command(clone_cmd_no_branch, cwd=self.__working_dir)

                if code != 0:
                    # Strategy 4: Try cloning without depth restriction (for completely empty repos)
                    logger.debug("Trying clone without depth restriction for empty repository")

                    clone_cmd_no_depth = ["clone", self.repo_url_with_path, "."]

                    stdout, stderr, code = await self._run_git_command(clone_cmd_no_depth, cwd=self.__working_dir)

                    if code != 0:
                        error_msg = f"Failed to clone repository with all strategies. Last error: {stderr}"
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
        try:
            # Use git ls-remote to get the default branch without cloning
            ls_remote_cmd = ["ls-remote", "--symref", self.repo_url_with_path, "HEAD"]
            stdout, stderr, code = await self._run_git_command(ls_remote_cmd)

            if code == 0 and stdout:
                # Parse output to find the default branch
                # Format: "ref: refs/heads/main	HEAD"
                for line in stdout.strip().split("\n"):
                    if line.startswith("ref: refs/heads/"):
                        default_branch = line.split("refs/heads/")[1].split("\t")[0]
                        logger.debug(f"Detected remote default branch: {default_branch}")
                        return default_branch

            logger.debug(f"Could not detect remote default branch, using configured: {self.branch}")
            return self.branch

        except Exception as e:
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
        except Exception as e:
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

    async def file_changed_between_commits(self, file_path: str, old_commit: str, new_commit: str) -> bool:
        """
        Check if a specific file was changed between commits using git diff.

        Args:
            file_path: Path to the file to check
            old_commit: Old commit hash
            new_commit: New commit hash

        Returns:
            True if the file changed, False otherwise
        """
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

        if file_path.startswith("/"):
            file_path = file_path[1:]

        if self.repo_path.endswith("/"):
            return f"{self.repo_path}{file_path}"
        else:
            return f"{self.repo_path}/{file_path}"

    def get_absolute_file_path(self, file_path: str) -> str:
        """
        Get the absolute path to a file in the local repository.

        Args:
            file_path: Path to the file relative to the repository root

        Returns:
            Absolute path to the file
        """
        relative_path = self._get_full_path(file_path)
        return os.path.join(self.__working_dir, relative_path)

    async def read_file_content(self, file_path: str) -> str:
        """
        Read content of a file directly from the local repository.

        Args:
            file_path: Path to the file in the repository

        Returns:
            Content of the file as a string
        """
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
        """
        Parse YAML content into a Python dictionary.

        Args:
            content: YAML content as a string

        Returns:
            Parsed YAML as a dictionary
        """
        logger.debug(f"Parsing YAML content of length {len(content)}")
        try:
            yaml = YAML()
            result = yaml.load(content)
            logger.debug("YAML parsing successful")
            return result
        except Exception as e:
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

    async def get_working_dir(self):
        await self.ensure_repo_cloned()
        return self.__working_dir

    async def add_file(self, file_path: str, content: str, overwrite: bool = True) -> None:
        """
        Add a file to the git staging area with specified content.

        Args:
            file_path: Path to the file relative to the repository root
            content: Content to write to the file
            overwrite: If True, overwrite existing file; if False, fail if file exists
        """
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
        """
        Delete a file and stage the deletion.

        Args:
            file_path: Path to the file relative to the repository root
        """
        logger.debug(f"Deleting file: {file_path}")

        # Ensure the repository is cloned
        await self.ensure_repo_cloned()

        # Use git rm to remove and stage the deletion
        rm_cmd = ["rm", self._get_full_path(file_path)]
        stdout, stderr, code = await self._run_git_command(rm_cmd, cwd=self.__working_dir)
        self._check_git_command_result(code, stderr, f"delete file {file_path}")

        logger.debug(f"Successfully deleted and staged file {file_path}")

    async def delete_folder(self, folder_path: str) -> None:
        """
        Delete a folder and all its contents, staging all deletions.

        Args:
            folder_path: Path to the folder relative to the repository root
        """
        logger.debug(f"Deleting folder: {folder_path}")

        # Ensure the repository is cloned
        await self.ensure_repo_cloned()

        # Use git rm -r to remove folder and stage all deletions
        rm_cmd = ["rm", "-r", self._get_full_path(folder_path)]
        stdout, stderr, code = await self._run_git_command(rm_cmd, cwd=self.__working_dir)
        self._check_git_command_result(code, stderr, f"delete folder {folder_path}")

        logger.debug(f"Successfully deleted and staged folder {folder_path}")

    async def read_file(self, file_path: str) -> str:
        """
        Read a file from the repository.

        Args:
            file_path: Path to the file relative to the repository root

        Returns:
            Content of the file as a string
        """
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

    async def commit_and_push(self, message: str) -> None:
        """
        Commit all changes in the working directory and push to remote repository.
        This stages all changes (including new files, modifications, and deletions) before committing.
        This is always done at the end when no other git operations are needed.

        Args:
            message: Commit message
        """
        logger.debug(f"Committing and pushing all changes: {message}")

        # Ensure the repository is cloned
        await self.ensure_repo_cloned()

        # Stage all changes in the working directory (new, modified, and deleted files)
        add_cmd = ["add", "-A"]
        stdout, stderr, code = await self._run_git_command(add_cmd, cwd=self.__working_dir)
        logger.debug("All changes staged successfully")

        # Commit and push the changes
        await self.commit_changes(message)
        await self.push_changes()

        logger.info(f"Successfully committed and pushed all changes: {message}")

    async def write_file_without_commit(self, file_path: str, content: str) -> None:
        """
        Create or update a file in the Git repository without committing changes.

        DEPRECATED: Use add_file() for atomic operations instead.
        This method is kept for backward compatibility.

        Args:
            file_path: Path to the file relative to the repository root
            content: Content to write to the file
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
        """
        Create or update a file in the Git repository and optionally commit and push the changes.

        Args:
            file_path: Path to the file relative to the repository root
            content: Content to write to the file
            commit_message: Optional commit message
        """
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
        """
        Check if a file exists in the Git repository.

        Args:
            file_path: Path to the file relative to the repository root

        Returns:
            True if file exists, False otherwise
        """
        await self.ensure_repo_cloned()
        abs_path = self.get_absolute_file_path(file_path)
        return os.path.exists(abs_path)

    async def check_overwrite_project_file(self, file_path: str) -> None:
        """
        Check if a project file can be overwritten based on settings.

        Raises an exception if the file exists and overwriting is not allowed.
        Does nothing if overwriting is allowed or file doesn't exist.

        Args:
            file_path: Path to the project file relative to the repository root

        Raises:
            RuntimeError: If file exists and ALLOW_PROJECTFILES_OVERWRITE is False
        """
        from opi.core.config import settings

        if await self.file_exists(file_path):
            if not settings.ALLOW_PROJECTFILES_OVERWRITE:
                # Extract project name from file path (e.g., "projects/myproject.yaml" -> "myproject")
                project_name = os.path.splitext(os.path.basename(file_path))[0]
                raise RuntimeError(
                    f"Project '{project_name}' already exists. Set ALLOW_PROJECTFILES_OVERWRITE=True to allow overwriting existing projects."
                )
            logger.warning(f"Overwriting existing project file: {file_path}")

    async def has_changes(self) -> bool:
        """
        Check if there are any uncommitted changes in the repository.

        Returns:
            True if there are changes to commit, False otherwise
        """
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
        """
        Add files to the git staging area.

        Args:
            files_or_paths: List of file paths or directories to add

        Returns:
            True if files were added successfully, False otherwise
        """
        await self.ensure_repo_cloned()

        for file_path in files_or_paths:
            add_cmd = ["add", file_path]
            stdout, stderr, code = await self._run_git_command(add_cmd, cwd=self.__working_dir)

            if code != 0:
                logger.error(f"Failed to add {file_path} to git: {stderr}")
                return False

        logger.debug(f"Successfully added {len(files_or_paths)} files to git staging")
        return True

    async def commit_changes(self, message: str) -> None:
        """
        Commit all changes in the working directory.

        Args:
            message: Commit message

        Raises:
            RuntimeError: If staging or commit fails
        """
        await self.ensure_repo_cloned()

        # Stage all changes in the working directory (new, modified, and deleted files)
        add_cmd = ["add", "-A"]
        stdout, stderr, code = await self._run_git_command(add_cmd, cwd=self.__working_dir)
        logger.debug("All changes staged successfully")

        await self._configure_git_user()

        commit_cmd = ["commit", "--no-verify", "-m", message]
        stdout, stderr, code = await self._run_git_command(commit_cmd, cwd=self.__working_dir)

        if code != 0:
            if "nothing to commit" in stdout or "nothing to commit" in stderr:
                logger.debug("No changes to commit")
                return
            else:
                server_info = self._get_server_context()
                error_msg = f"Failed to commit changes to {server_info}: {stderr}"
                logger.error(error_msg)
                raise RuntimeError(error_msg)

        logger.debug(f"Successfully committed changes: {message}")

    # TODO: update push changes to handle rebase, and if rebase fails, commit and push to temporary branch
    async def push_changes(self, branch: str | None = None) -> None:
        """
        Push committed changes to remote repository.

        Args:
            branch: Branch to push to (defaults to configured branch)

        Raises:
            RuntimeError: If push fails
        """
        await self.ensure_repo_cloned()

        target_branch = branch or self.branch
        push_cmd = ["push", "origin", target_branch]
        stdout, stderr, code = await self._run_git_command(push_cmd, cwd=self.__working_dir)
        self._check_git_command_result(code, stderr, f"push changes to {target_branch}")

        logger.debug(f"Successfully pushed changes to {target_branch}")

    async def commit_and_push_changes(
        self, message: str, files_or_paths: list[str] | None = None, branch: str | None = None
    ) -> bool:
        """
        Add, commit, and push changes in one operation.

        Args:
            message: Commit message
            files_or_paths: Optional list of files to add (defaults to ["."] for all changes)
            branch: Branch to push to (defaults to configured branch)

        Returns:
            True if all operations were successful, False otherwise
        """
        await self.ensure_repo_cloned()

        # Check if there are changes first
        if not await self.has_changes():
            logger.info("No changes to commit and push")
            return True

        # Add files
        files_to_add = files_or_paths or ["."]
        if not await self.add_files(files_to_add):
            return False

        # Commit and push changes
        await self.commit_changes(message)
        await self.push_changes(branch)

        logger.info(f"Successfully committed and pushed changes: {message}")
        return True

    async def get_previous_file_content(self, file_path: str, commits_back: int = 1) -> str | None:
        """
        Get the content of a file from a previous commit.

        Args:
            file_path: Path to the file within the repository
            commits_back: Number of commits to go back (default: 1 for HEAD~1)

        Returns:
            File content as string, or None if file doesn't exist in previous commit
        """
        try:
            # Ensure repository is cloned
            await self.ensure_repo_cloned()

            # Check if there are enough commits
            stdout, stderr, returncode = await self._run_git_command(
                ["rev-list", "--count", "HEAD"], cwd=self.__working_dir
            )

            if returncode != 0:
                logger.debug(f"Failed to get commit count: {stderr}")
                return None

            commit_count = int(stdout.strip())
            if commit_count <= commits_back:
                logger.debug(f"Not enough commits to go back {commits_back} - only {commit_count} commit(s)")
                return None

            # Get the file content from the specified previous commit
            commit_ref = f"HEAD~{commits_back}"
            clean_file_path = file_path.lstrip("/")

            stdout, stderr, returncode = await self._run_git_command(
                ["show", f"{commit_ref}:{clean_file_path}"], cwd=self.__working_dir
            )

            if returncode == 0:
                logger.debug(f"Successfully retrieved previous version of {file_path} from {commit_ref}")
                return stdout
            else:
                logger.debug(f"File {file_path} does not exist in {commit_ref}: {stderr}")
                return None

        except Exception as e:
            logger.warning(f"Error retrieving previous file content for {file_path}: {e}")
            return None

    async def close(self) -> None:
        """Clean up resources."""
        # TODO: rethink cleanup logic.. for now, we always remove the working directory on close
        # if self.should_cleanup and self.working_dir and os.path.exists(self.working_dir):
        if await self.has_changes():
            logger.warning(
                f"Repository {self.name} for project {self.project_name} has uncommitted changes but is being closed."
            )
        if self.__working_dir and os.path.exists(self.__working_dir):
            logger.debug(
                f"Closing GitConnector {self.name} for project {self.project_name}; removing temporary directory: {self.__working_dir}"
            )
            shutil.rmtree(self.__working_dir, ignore_errors=True)

    @staticmethod
    async def create_repository(
        server_host: str, repo_name: str, ssh_key_path: str | None = None, ssh_port: int = 22, ssh_user: str = "git"
    ) -> bool:
        """
        Creates a new bare Git repository on the remote server via SSH.
        This is a static method that can be called without creating a GitConnector instance.

        Args:
            server_host: Hostname or IP of the Git server
            repo_name: Name of the repository to create (without .git extension)
            ssh_key_path: Path to the SSH private key file for authentication
            ssh_port: SSH port number for the server (default: 22)
            ssh_user: SSH username for authentication (default: git)

        Returns:
            True if repository was created successfully, False otherwise
        """
        logger.info(f"Creating new Git repository '{repo_name}' on server {server_host}")

        # Ensure repo_name doesn't have .git extension already
        if repo_name.endswith(".git"):
            repo_name = repo_name[:-4]

        repo_path = f"/srv/git/{repo_name}.git"
        logger.debug(f"Repository path on server: {repo_path}")

        # Build SSH command with appropriate options
        ssh_cmd_base = ["ssh"]

        # Add port if not default
        if ssh_port != 22:
            ssh_cmd_base.extend(["-p", str(ssh_port)])

        # Add SSH key if provided
        if ssh_key_path:
            ssh_cmd_base.extend(["-i", ssh_key_path])

        # Add options to disable host checking
        ssh_cmd_base.extend(["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null"])

        # Add user@host
        ssh_target = f"{ssh_user}@{server_host}"

        # First check if repository already exists
        check_cmd = ssh_cmd_base + [ssh_target, f"test -d {repo_path}"]
        logger.debug(f"Checking if repository exists: {' '.join(check_cmd)}")

        try:
            process = await asyncio.create_subprocess_exec(
                *check_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )

            stdout, stderr = await process.communicate()

            if process.returncode == 0:
                logger.info(f"Repository {repo_name}.git already exists, skipping creation")
                return True
            else:
                logger.debug("Repository does not exist, proceeding with creation")

        except Exception as e:
            logger.warning(f"Error checking repository existence: {e}, proceeding with creation")

        # Commands to execute on the remote server (only if repo doesn't exist)
        commands = [f"mkdir -p {repo_path}", f"git-init -b main --bare {repo_path}"]

        # Execute each command
        for cmd in commands:
            ssh_cmd = ssh_cmd_base + [ssh_target, cmd]
            logger.debug(f"Executing SSH command: {' '.join(ssh_cmd)}")

            try:
                # Run the SSH command
                process = await asyncio.create_subprocess_exec(
                    *ssh_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                )

                stdout, stderr = await process.communicate()
                stdout_str = stdout.decode("utf-8").strip()
                stderr_str = stderr.decode("utf-8").strip()

                if process.returncode != 0:
                    # Special handling for "already exists" errors
                    if "already exists" in stderr_str.lower():
                        logger.info(f"Repository {repo_name}.git already exists (detected during creation)")
                        return True
                    else:
                        logger.error(f"Command '{cmd}' failed with exit code {process.returncode}: {stderr_str}")
                        return False

                if stdout_str:
                    logger.debug(f"Command output: {stdout_str}")

            except Exception as e:
                logger.error(f"Error executing SSH command: {e}")
                return False

        logger.info(f"Successfully created Git repository: {repo_name}.git")
        return True


async def poll_for_changes(
    connector: GitConnector,
    file_path: str,
    interval: int = 10,
    callback: Callable[[str, dict], Coroutine[Any, Any, None]] | None = None,
    stop_event: asyncio.Event | None = None,
) -> None:
    """
    Poll the Git repository for changes to a specific file.

    Args:
        connector: GitConnector instance
        file_path: Path to the file to monitor
        interval: Polling interval in seconds
        callback: Optional async callback function to call when changes are detected
            Callback signature: async fn(file_path, current_content)
        stop_event: Optional asyncio Event to signal stopping the polling
    """
    logger.info(f"Starting to poll for changes to {file_path} every {interval} seconds")
    logger.debug(f"Monitoring branch: {connector.branch}, Repo URL: {connector.repo_url_with_path}")

    # 1. On startup: Clone the repository and do initial load
    try:
        # Clone the repository
        logger.info("Cloning repository and checking out the branch")
        await connector.ensure_repo_cloned()
        logger.debug("Repository successfully cloned")

        # Get the local commit hash from the checked out repository
        local_hash = await connector.get_local_commit_hash()
        logger.info(f"Current commit hash: {local_hash}")

        # Process the file initially if callback is provided
        if callback:
            # Read the file directly from the filesystem
            content = await connector.read_file_content(file_path)

            # Parse YAML content
            parsed_content = await connector.parse_yaml_content(content)

            # Call the callback with the initial content
            logger.info("Calling callback with initial file content")
            await callback(file_path, parsed_content)
    except Exception as e:
        logger.error(f"Failed to initialize repository monitoring: {e}")
        logger.debug(f"Initialization error details: {e!s}", exc_info=True)
        # If we can't initialize, we can't continue
        return

    # Main polling loop
    try:
        while True:
            if stop_event and stop_event.is_set():
                logger.info("Stop event received, ending polling")
                break

            try:
                # 2. Check for remote changes using ls-remote
                remote_hash = await connector.get_latest_commit_hash()

                # If remote has changes
                if remote_hash != local_hash:
                    logger.info(f"Remote hash changed: {remote_hash} (was {local_hash})")

                    # First fetch the changes to make both commits available locally
                    logger.debug("Fetching changes before diff")
                    await connector._fetch_latest()

                    # Check if our file changed
                    file_changed = await connector.file_changed_between_commits(file_path, local_hash, remote_hash)

                    if file_changed:
                        logger.info(f"File {file_path} changed between commits")

                        # Pull the changes to update the working directory
                        logger.debug("Pulling changes to update working directory")
                        await connector._pull_latest()

                        # Process the file and notify callback
                        if callback:
                            # Read the updated file directly from the filesystem
                            content = await connector.read_file_content(file_path)

                            # Parse YAML content
                            parsed_content = await connector.parse_yaml_content(content)

                            # Call the callback with the updated content
                            logger.info("Calling callback with updated file content")
                            await callback(file_path, parsed_content)
                    else:
                        logger.debug(f"File {file_path} did not change despite commit changes")

                    # Update local hash
                    local_hash = remote_hash
                else:
                    logger.debug("No remote changes detected")

            except Exception as e:
                logger.error(f"Error during polling cycle: {e}")
                logger.debug(f"Polling error details: {e!s}", exc_info=True)

            # Sleep for the interval
            await asyncio.sleep(interval)
    finally:
        # Clean up resources
        logger.debug("Polling loop ended, cleaning up resources")
        await connector.close()


async def start_file_monitoring(
    repo_url: str,
    file_path: str,
    branch: str = "main",
    interval: int = 10,
    repo_path: str | None = None,
    working_dir: str | None = None,
    callback: Callable[[str, dict], Coroutine[Any, Any, None]] | None = None,
    stop_event: asyncio.Event | None = None,
    ssh_key_path: str | None = None,
    password: str | None = None,
    username: str | None = None,
) -> None:
    """
    Start monitoring a YAML file for changes.

    Args:
        repo_url: URL for the Git repository (git://, ssh://, https://, git@host:path)
        file_path: Path to the YAML file to monitor
        branch: Branch to monitor
        interval: Polling interval in seconds
        repo_path: Path within the repository (e.g., "/subdir/project")
        working_dir: Optional working directory for the local clone
        callback: Optional async callback function called when changes are detected
        stop_event: Optional asyncio Event to signal stopping the polling
        ssh_key_path: Optional path to SSH private key for authentication
        password: Optional password for authentication (may be encrypted)
        username: Optional username for authentication
    """
    logger.debug(f"Starting file monitoring for {file_path} in {repo_url} (branch: {branch})")
    logger.debug(f"Repository path: {repo_path}, Poll interval: {interval}s")

    connector = GitConnector(repo_url, repo_path, working_dir, branch, ssh_key_path, password, username)
    await poll_for_changes(connector, file_path, interval, callback, stop_event)


# FastAPI Integration Functions


async def start_monitoring_task(
    repo_url: str,
    file_path: str,
    branch: str = "main",
    interval: int = 10,
    repo_path: str | None = None,
    working_dir: str | None = None,
    callback: Callable[[str, dict], Coroutine[Any, Any, None]] | None = None,
    ssh_key_path: str | None = None,
    password: str | None = None,
    username: str | None = None,
) -> asyncio.Task:
    """
    Create and start a monitoring task that can be used with FastAPI.

    Args:
        repo_url: URL for the Git repository (git://, ssh://, https://, git@host:path)
        file_path: Path to the YAML file to monitor
        branch: Branch to monitor
        interval: Polling interval in seconds
        repo_path: Path within the repository (e.g., "/subdir/project")
        working_dir: Optional working directory for the local clone
        callback: Optional async callback function called when changes are detected
        ssh_key_path: Optional path to SSH private key for authentication
        password: Optional password for authentication
        username: Optional username for authentication

    Returns:
        asyncio.Task that can be stored and cancelled later
    """
    logger.debug(f"Creating monitoring task for {file_path} in {repo_url}")
    stop_event = asyncio.Event()

    async def _monitoring_wrapper():
        try:
            logger.debug("Starting monitoring wrapper")
            await start_file_monitoring(
                repo_url,
                file_path,
                branch,
                interval,
                repo_path,
                working_dir,
                callback,
                stop_event,
                ssh_key_path,
                password,
                username,
            )
        except asyncio.CancelledError:
            logger.debug("Monitoring task cancel requested")
            stop_event.set()
            logger.info("Monitoring task cancelled")
            raise
        except Exception as e:
            logger.error(f"Error in monitoring task: {e}")
            raise

    task = asyncio.create_task(_monitoring_wrapper())
    logger.debug(f"Monitoring task created: {task.get_name()}")
    return task


async def create_git_repository(
    server_host: str, repo_name: str, ssh_key_path: str | None = None, ssh_port: int = 22, ssh_user: str = "git"
) -> bool:
    """
    Creates a new bare Git repository on the remote server via SSH.

    This is a standalone function that creates a Git repository without requiring a GitConnector instance.
    It uses the static method GitConnector.create_repository internally.

    Args:
        server_host: Hostname or IP of the Git server
        repo_name: Name of the repository to create (without .git extension)
        ssh_key_path: Path to the SSH private key file for authentication
        ssh_port: SSH port number for the server (default: 22)
        ssh_user: SSH username for authentication (default: git)

    Returns:
        True if repository was created successfully, False otherwise
    """
    logger.info(f"Creating new Git repository '{repo_name}' via standalone function")

    # Use the static method directly
    return await GitConnector.create_repository(
        server_host=server_host, repo_name=repo_name, ssh_key_path=ssh_key_path, ssh_port=ssh_port, ssh_user=ssh_user
    )


# TODO: replace factory method with direct calls to the GitConnector?
async def create_git_connector_from_repo_config(repo_config: dict[str, Any]) -> GitConnector:
    connector = GitConnector(
        repo_url=repo_config["url"],
        repo_path=repo_config.get("path"),
        branch=repo_config["branch"],
        username=repo_config.get("username"),
        password=repo_config.get("password"),
        ssh_key_path=repo_config.get("ssh_key_path"),
        project_name=repo_config.get("project_name"),
        name=repo_config.get("name"),
    )
    return connector


async def create_git_connector_for_argocd(project_name: str) -> GitConnector:
    """
    Create a GitConnector for the GitOps (Argo) repository.

    Returns:
        Configured GitConnector instance
    """
    gitops_repo_config = {
        "url": settings.GIT_ARGO_APPLICATIONS_URL,
        "branch": settings.GIT_ARGO_APPLICATIONS_BRANCH,
        "password": settings.GIT_ARGO_APPLICATIONS_PASSWORD,
        "username": settings.GIT_ARGO_APPLICATIONS_USERNAME,
        "project_name": project_name,
        "name": "argo",
    }

    # Only add SSH key if it's not empty (for SSH URLs)
    # TODO: this is probably obsolete?
    if settings.GIT_ARGO_APPLICATIONS_KEY and settings.GIT_ARGO_APPLICATIONS_KEY.strip():
        gitops_repo_config["ssh_key_path"] = settings.GIT_ARGO_APPLICATIONS_KEY

    return await create_git_connector_from_repo_config(gitops_repo_config)


async def create_git_connector_for_project_files(project_name: str) -> GitConnector:
    projects_repo_config = {
        "url": settings.GIT_PROJECTS_SERVER_URL,
        "branch": settings.GIT_PROJECTS_SERVER_BRANCH,
        "path": settings.GIT_PROJECTS_SERVER_REPO_PATH,
        "password": settings.GIT_PROJECTS_SERVER_PASSWORD,
        "username": settings.GIT_PROJECTS_SERVER_USERNAME,
        "project_name": project_name,
        "name": "projects",
    }
    return await create_git_connector_from_repo_config(projects_repo_config)
