# Persistent Git Cache for Faster Startups

## Status

**Planned** - Not yet implemented

## Problem Statement

The Operations Manager currently clones the project files repository from scratch on every pod restart, causing:

1. **Slow startups**: Full git clone takes 5-30 seconds depending on repo size
2. **Network overhead**: Repeated downloads of the same data
3. **Git server load**: Unnecessary clone operations
4. **Wasted resources**: Temp storage is cleared on pod restart

During development with frequent rebuilds, this overhead is particularly noticeable.

## Current Behavior

```python
# In git.py:118
self.__working_dir = tempfile.mkdtemp(prefix="git-repo-", dir=settings.TEMP_DIR)
self.should_cleanup = True
```

- Every pod restart creates a new temp directory
- Full `git clone` operation on startup
- Temp directory cleaned up when connector closes
- In-memory `ProjectRefreshState` with 30-second TTL (lost on restart)

## Solution Design

### Architecture Overview

Create a **generic, reusable git caching system** that works for ALL git operations:

```
┌─────────────────────────────────────────────────────────┐
│                   Application Code                      │
│  (project_manager, argo_manager, manifest_generator)    │
└───────────────────────┬─────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────────┐
│              GitConnector (unchanged API)               │
│        Uses cache automatically & transparently         │
└───────────────────────┬─────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────────┐
│                 GitCacheManager                         │
│  - Generates cache keys from URL+branch                 │
│  - Manages cache directory structure                    │
│  - Handles metadata persistence                         │
│  - Provides cache statistics                            │
└───────────────────────┬─────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────────┐
│           Persistent Storage (/var/cache/git)           │
│  projects-repo-main/                                    │
│  argo-apps-main/                                        │
│  manifests-repo-feature-branch/                         │
│  .cache_metadata.json                                   │
└─────────────────────────────────────────────────────────┘
```

**Key Design Principles:**

1. **Transparent**: Works automatically without changing existing code
2. **Universal**: Any GitConnector instance can use caching
3. **Safe**: Automatic cache key generation prevents collisions
4. **Smart**: TTL-based invalidation with automatic refresh
5. **Observable**: Built-in metrics and logging

### 1. Persistent Git Storage

Add a PersistentVolumeClaim to store ALL git repositories:

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: operations-manager-git-cache
  namespace: rig-system
spec:
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: 5Gi  # Increased to handle multiple repos
  storageClassName: standard
```

Mount in deployment:

```yaml
volumeMounts:
- name: git-cache
  mountPath: /var/cache/git

volumes:
- name: git-cache
  persistentVolumeClaim:
    claimName: operations-manager-git-cache
```

### 2. Automatic Cache Key Generation

Generate unique cache keys from repository URL and branch:

```python
def generate_cache_key(repo_url: str, branch: str) -> str:
    """
    Generate a safe, unique cache key from repo URL and branch.

    Examples:
      https://github.com/user/repo.git + main
        -> github-com-user-repo-main

      git@gitlab.com:group/project.git + feature/xyz
        -> gitlab-com-group-project-feature-xyz

      https://github.com/user/repo.git + main (path: /subdir)
        -> github-com-user-repo-main
    """
    import hashlib
    import re

    # Parse URL to get meaningful parts
    # Remove protocol and .git suffix
    cleaned = re.sub(r'^(https?://|git@|ssh://)', '', repo_url)
    cleaned = re.sub(r'\.git$', '', cleaned)
    cleaned = cleaned.replace(':', '-').replace('/', '-')

    # Sanitize branch name
    safe_branch = branch.replace('/', '-').replace('_', '-')

    # Combine and truncate if too long
    cache_key = f"{cleaned}-{safe_branch}"
    cache_key = re.sub(r'[^a-zA-Z0-9-]', '', cache_key)
    cache_key = cache_key.lower()

    # If still too long (>100 chars), add hash suffix
    if len(cache_key) > 100:
        url_hash = hashlib.md5(f"{repo_url}{branch}".encode()).hexdigest()[:8]
        cache_key = cache_key[:90] + '-' + url_hash

    return cache_key
```

### 3. Cache Metadata Structure

Store metadata for ALL cached repos in a single JSON file:

```json
{
  "github-com-user-projects-main": {
    "repo_url": "https://github.com/user/projects.git",
    "branch": "main",
    "last_fetched": "2026-01-14T15:30:00Z",
    "commit_sha": "a298892a166be031a8aab0f8e8dbd7b97b2a609a",
    "fetch_count": 42,
    "cache_hits": 156
  },
  "gitlab-com-group-argo-apps-main": {
    "repo_url": "git@gitlab.com:group/argo-apps.git",
    "branch": "main",
    "last_fetched": "2026-01-14T15:25:00Z",
    "commit_sha": "b123456...",
    "fetch_count": 18,
    "cache_hits": 73
  }
}
```

File location: `/var/cache/git/.cache_metadata.json`

Cache directory structure:
```
/var/cache/git/
├── .cache_metadata.json
├── github-com-user-projects-main/
│   └── .git/
├── gitlab-com-group-argo-apps-main/
│   └── .git/
└── github-com-company-manifests-feature-xyz/
    └── .git/
```

### 4. Smart Refresh Logic

```
┌──────────────────┐
│  Pod Starts      │
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│ Check if git     │
│ repo exists at   │◄────────┐
│ persistent path  │         │
└────────┬─────────┘         │
         │                   │
    ┌────┴────┐              │
    │ Exists? │              │
    └────┬────┘              │
         │                   │
    ┌────┴─────┐             │
    │  Yes/No  │             │
    └────┬─────┘             │
         │                   │
    NO──►├──YES               │
         │                   │
         ▼                   │
  ┌──────────────┐           │
  │  git clone   │           │
  └──────┬───────┘           │
         │                   │
         └──────────────────►│
                             │
         ┌───────────────────┘
         │
         ▼
  ┌──────────────────┐
  │ Check cache age  │
  │ (from metadata)  │
  └────────┬─────────┘
           │
      ┌────┴────┐
      │ Fresh?  │
      │ (<5min) │
      └────┬────┘
           │
      ┌────┴─────┐
      │  Yes/No  │
      └────┬─────┘
           │
      YES──┤──NO
           │
           ▼
    ┌──────────────┐
    │  git fetch   │
    │  git reset   │
    └──────┬───────┘
           │
           ▼
    ┌──────────────┐
    │ Update cache │
    │  metadata    │
    └──────────────┘
```

**Cache Validation Rules:**
- **< 5 minutes old**: Skip fetch entirely (use existing clone)
- **5+ minutes old**: Run `git fetch` + `git reset --hard origin/branch`
- **Missing/corrupt**: Full `git clone`

### 4. Implementation Changes

#### A. New Configuration Settings

Add to `config.py`:

```python
class Settings(BaseSettings):
    # ... existing settings ...

    # Git cache settings
    GIT_CACHE_DIR: str = "/var/cache/git"
    GIT_CACHE_TTL_SECONDS: int = 300  # 5 minutes
    GIT_CACHE_METADATA_FILE: str = ".cache_metadata.json"
```

#### B. Generic Cache Manager Module

Create `opi/utils/git_cache.py`:

```python
"""
Generic git cache management for persistent storage.

This module provides a reusable caching system for ALL git repositories
used in the Operations Manager. It automatically generates cache keys,
manages TTL-based invalidation, and provides metrics.
"""

import hashlib
import json
import logging
import os
import re
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def generate_cache_key(repo_url: str, branch: str) -> str:
    """
    Generate a safe, unique cache key from repo URL and branch.

    This ensures no collisions between different repos or branches.

    Args:
        repo_url: Git repository URL (any format)
        branch: Branch name

    Returns:
        Safe filesystem-compatible cache key

    Examples:
        >>> generate_cache_key("https://github.com/user/repo.git", "main")
        'github-com-user-repo-main'
        >>> generate_cache_key("git@gitlab.com:group/project.git", "feature/xyz")
        'gitlab-com-group-project-feature-xyz'
    """
    # Remove protocol and .git suffix
    cleaned = re.sub(r'^(https?://|git@|ssh://)', '', repo_url)
    cleaned = re.sub(r'\.git$', '', cleaned)
    cleaned = cleaned.replace(':', '-').replace('/', '-')

    # Sanitize branch name
    safe_branch = branch.replace('/', '-').replace('_', '-')

    # Combine and sanitize
    cache_key = f"{cleaned}-{safe_branch}"
    cache_key = re.sub(r'[^a-zA-Z0-9-]', '', cache_key)
    cache_key = cache_key.lower()

    # Truncate if too long, add hash for uniqueness
    if len(cache_key) > 100:
        url_hash = hashlib.md5(f"{repo_url}{branch}".encode()).hexdigest()[:8]
        cache_key = cache_key[:90] + '-' + url_hash

    return cache_key


class GitCacheManager:
    """
    Manages persistent git repository cache with TTL-based invalidation.

    This is a thread-safe, singleton cache manager that handles:
    - Automatic cache key generation from repo URL + branch
    - TTL-based cache invalidation
    - Metadata persistence across pod restarts
    - Cache statistics and metrics
    """

    def __init__(self, cache_dir: str, metadata_file: str = ".cache_metadata.json"):
        self.cache_dir = Path(cache_dir)
        self.metadata_file = self.cache_dir / metadata_file
        self._metadata: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        """Load metadata from file (thread-safe)."""
        with self._lock:
            if self.metadata_file.exists():
                try:
                    with open(self.metadata_file) as f:
                        self._metadata = json.load(f)
                    logger.info(f"Loaded git cache metadata: {len(self._metadata)} repositories")
                except Exception as e:
                    logger.warning(f"Failed to load cache metadata: {e}")
                    self._metadata = {}
            else:
                logger.info("No cache metadata found, starting with empty cache")
                self._metadata = {}

    def _save(self) -> None:
        """Save metadata to file (thread-safe)."""
        with self._lock:
            try:
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                with open(self.metadata_file, 'w') as f:
                    json.dump(self._metadata, f, indent=2, sort_keys=True)
                logger.debug("Saved git cache metadata")
            except Exception as e:
                logger.error(f"Failed to save cache metadata: {e}")

    def get_cache_path(self, repo_url: str, branch: str) -> str:
        """
        Get the persistent cache path for a repository.

        Args:
            repo_url: Git repository URL
            branch: Branch name

        Returns:
            Absolute path to cache directory
        """
        cache_key = generate_cache_key(repo_url, branch)
        return str(self.cache_dir / cache_key)

    def is_cache_valid(self, repo_url: str, branch: str, ttl_seconds: int) -> bool:
        """
        Check if cached repo is still valid based on TTL.

        Args:
            repo_url: Git repository URL
            branch: Branch name
            ttl_seconds: Time-to-live in seconds

        Returns:
            True if cache exists and is fresh, False otherwise
        """
        cache_key = generate_cache_key(repo_url, branch)

        with self._lock:
            repo_meta = self._metadata.get(cache_key)

        if not repo_meta:
            logger.debug(f"No cache metadata for {cache_key}")
            return False

        try:
            last_fetched_str = repo_meta.get("last_fetched")
            if not last_fetched_str:
                return False

            last_fetched = datetime.fromisoformat(last_fetched_str)
            age = datetime.now(UTC) - last_fetched
            age_seconds = age.total_seconds()

            is_valid = age_seconds < ttl_seconds

            logger.debug(
                f"Cache {cache_key}: age={age_seconds:.0f}s, "
                f"ttl={ttl_seconds}s, valid={is_valid}"
            )
            return is_valid

        except Exception as e:
            logger.warning(f"Failed to check cache validity for {cache_key}: {e}")
            return False

    def update_metadata(
        self,
        repo_url: str,
        branch: str,
        commit_sha: str,
        operation: str = "fetch"
    ) -> None:
        """
        Update metadata for a repository after fetch/clone.

        Args:
            repo_url: Git repository URL
            branch: Branch name
            commit_sha: Current commit SHA
            operation: Operation type ('fetch' or 'clone')
        """
        cache_key = generate_cache_key(repo_url, branch)

        with self._lock:
            if cache_key not in self._metadata:
                self._metadata[cache_key] = {
                    "repo_url": repo_url,
                    "branch": branch,
                    "created_at": datetime.now(UTC).isoformat(),
                    "fetch_count": 0,
                    "cache_hits": 0
                }

            metadata = self._metadata[cache_key]
            metadata["last_fetched"] = datetime.now(UTC).isoformat()
            metadata["commit_sha"] = commit_sha
            metadata["last_operation"] = operation

            if operation == "fetch":
                metadata["fetch_count"] = metadata.get("fetch_count", 0) + 1
            elif operation == "clone":
                metadata["fetch_count"] = 1  # Reset on clone

        self._save()
        logger.info(f"Updated cache metadata for {cache_key}: {commit_sha[:8]} ({operation})")

    def record_cache_hit(self, repo_url: str, branch: str) -> None:
        """Record a cache hit for metrics."""
        cache_key = generate_cache_key(repo_url, branch)

        with self._lock:
            if cache_key in self._metadata:
                self._metadata[cache_key]["cache_hits"] = (
                    self._metadata[cache_key].get("cache_hits", 0) + 1
                )
        # Don't save on every hit (performance), save periodically or on other operations

    def get_metadata(self, repo_url: str, branch: str) -> dict[str, Any] | None:
        """Get metadata for a repository."""
        cache_key = generate_cache_key(repo_url, branch)
        with self._lock:
            return self._metadata.get(cache_key)

    def get_all_metadata(self) -> dict[str, dict[str, Any]]:
        """Get metadata for all cached repositories."""
        with self._lock:
            return dict(self._metadata)

    def get_cache_stats(self) -> dict[str, Any]:
        """Get overall cache statistics."""
        with self._lock:
            total_repos = len(self._metadata)
            total_hits = sum(m.get("cache_hits", 0) for m in self._metadata.values())
            total_fetches = sum(m.get("fetch_count", 0) for m in self._metadata.values())

            return {
                "total_repositories": total_repos,
                "total_cache_hits": total_hits,
                "total_fetches": total_fetches,
                "cache_hit_ratio": total_hits / (total_hits + total_fetches) if (total_hits + total_fetches) > 0 else 0
            }

    def clear_cache(self, repo_url: str | None = None, branch: str | None = None) -> bool:
        """
        Clear cache for specific repo or all repos.

        Args:
            repo_url: Optional URL to clear specific repo
            branch: Optional branch (requires repo_url)

        Returns:
            True if successful
        """
        if repo_url:
            cache_key = generate_cache_key(repo_url, branch or "main")
            cache_path = self.cache_dir / cache_key

            try:
                if cache_path.exists():
                    import shutil
                    shutil.rmtree(cache_path)
                    logger.info(f"Cleared cache: {cache_key}")

                with self._lock:
                    if cache_key in self._metadata:
                        del self._metadata[cache_key]
                self._save()
                return True
            except Exception as e:
                logger.error(f"Failed to clear cache {cache_key}: {e}")
                return False
        else:
            # Clear all cache
            try:
                import shutil
                if self.cache_dir.exists():
                    for item in self.cache_dir.iterdir():
                        if item.name != self.metadata_file.name:
                            if item.is_dir():
                                shutil.rmtree(item)
                            else:
                                item.unlink()

                with self._lock:
                    self._metadata = {}
                self._save()
                logger.info("Cleared all git cache")
                return True
            except Exception as e:
                logger.error(f"Failed to clear all cache: {e}")
                return False


# Global singleton instance
_cache_manager: GitCacheManager | None = None
_cache_lock = threading.Lock()


def get_git_cache_manager() -> GitCacheManager:
    """Get the global git cache manager instance (thread-safe singleton)."""
    global _cache_manager

    if _cache_manager is None:
        with _cache_lock:
            # Double-check after acquiring lock
            if _cache_manager is None:
                from opi.core.config import settings
                _cache_manager = GitCacheManager(
                    settings.GIT_CACHE_DIR,
                    settings.GIT_CACHE_METADATA_FILE
                )
                logger.info(f"Initialized global git cache manager at {settings.GIT_CACHE_DIR}")

    return _cache_manager
```

#### C. Modify GitConnector (Automatic Caching)

Update `GitConnector.__init__()` in `git.py` to use caching automatically:

```python
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
    use_cache: bool = True,  # NEW: Enable caching by default (opt-out)
):
    # ... existing init code ...

    # Set up working directory with automatic caching
    if working_dir:
        # Explicit working directory provided, use it
        self.__working_dir = working_dir
        self.should_cleanup = False
        self.use_cache = False
        logger.debug(f"Using provided working directory: {working_dir}")
    elif use_cache and os.path.exists(settings.GIT_CACHE_DIR):
        # Use persistent cache (default behavior)
        from opi.utils.git_cache import get_git_cache_manager

        cache_manager = get_git_cache_manager()
        self.__working_dir = cache_manager.get_cache_path(repo_url, self.branch)
        self.should_cleanup = False
        self.use_cache = True
        self.cache_manager = cache_manager

        logger.info(
            f"Using persistent git cache for {repo_url} ({self.branch}): "
            f"{self.__working_dir}"
        )
    else:
        # Fallback to temp directory (cache disabled or not available)
        self.__working_dir = tempfile.mkdtemp(prefix="git-repo-", dir=settings.TEMP_DIR)
        self.should_cleanup = True
        self.use_cache = False
        logger.debug(f"Created temporary working directory: {self.__working_dir}")
```

**Key Changes:**
- **`use_cache=True`** by default → caching automatic for all repos
- **No "name" parameter needed** → cache key generated from `repo_url + branch`
- **Transparent** → existing code works without changes
- **Opt-out** → set `use_cache=False` to disable

Update `ensure_repo_cloned()` to use the smart cache logic:

```python
async def ensure_repo_cloned(self) -> None:
    """
    Ensure the repository is cloned, using cache if available.

    This method automatically:
    - Uses cached repo if fresh (< TTL)
    - Fetches updates if cache expired
    - Does full clone if no cache exists
    - Updates cache metadata after operations
    """
    if self._repo_cloned:
        return

    repo_exists = os.path.exists(os.path.join(self.__working_dir, ".git"))

    if self.use_cache and repo_exists:
        # Cache exists, check if it's fresh
        is_valid = self.cache_manager.is_cache_valid(
            self.repo_url,
            self.branch,
            settings.GIT_CACHE_TTL_SECONDS
        )

        if is_valid:
            # Cache is fresh, use it directly
            logger.info(f"✓ Using cached git repository: {self.repo_url} ({self.branch})")
            self.cache_manager.record_cache_hit(self.repo_url, self.branch)
            self._repo_cloned = True
            return
        else:
            # Cache expired, fetch updates
            logger.info(f"⟳ Cache expired, fetching updates: {self.repo_url} ({self.branch})")
            try:
                await self._fetch_and_reset()

                # Update cache metadata
                commit_sha = await self.get_current_commit()
                self.cache_manager.update_metadata(
                    self.repo_url,
                    self.branch,
                    commit_sha,
                    operation="fetch"
                )

                self._repo_cloned = True
                return

            except Exception as e:
                logger.warning(f"Failed to fetch cached repo, will re-clone: {e}")
                # Remove corrupted cache and fall through to full clone
                shutil.rmtree(self.__working_dir, ignore_errors=True)

    # Full clone (no cache or corrupted cache)
    logger.info(f"↓ Cloning git repository: {self.repo_url} ({self.branch})")
    await self._clone_repo()

    # Update cache metadata if using cache
    if self.use_cache:
        commit_sha = await self.get_current_commit()
        self.cache_manager.update_metadata(
            self.repo_url,
            self.branch,
            commit_sha,
            operation="clone"
        )

    self._repo_cloned = True
```

Add fetch and reset method:

```python
async def _fetch_and_reset(self) -> None:
    """Fetch updates and reset to origin/branch."""
    logger.debug(f"Fetching updates for {self.branch}")

    # Configure git if needed
    await self._configure_git_user()

    # Fetch
    cmd = f"cd {self.__working_dir} && git fetch origin {self.branch}"
    stdout, stderr, code = await self._run_git_command(cmd)

    if code != 0:
        raise RuntimeError(f"git fetch failed: {stderr}")

    # Reset to origin/branch
    cmd = f"cd {self.__working_dir} && git reset --hard origin/{self.branch}"
    stdout, stderr, code = await self._run_git_command(cmd)

    if code != 0:
        raise RuntimeError(f"git reset failed: {stderr}")

    logger.info(f"Successfully updated cached repo from origin/{self.branch}")
```

#### D. No Changes Needed to Existing Code!

**That's the beauty of this design** - existing code works automatically:

```python
# This already uses caching automatically!
async def create_git_connector_for_project_files(project_name: str) -> GitConnector:
    projects_repo_config = {
        "url": settings.GIT_PROJECTS_SERVER_URL,
        "branch": settings.GIT_PROJECTS_SERVER_BRANCH,
        "path": settings.GIT_PROJECTS_SERVER_REPO_PATH,
        "password": settings.GIT_PROJECTS_SERVER_PASSWORD,
        "username": settings.GIT_PROJECTS_SERVER_USERNAME,
        "project_name": project_name,
        "name": "projects",
        # No need to add use_cache - it's True by default!
    }
    return await create_git_connector_from_repo_config(projects_repo_config)

# This also uses caching automatically!
async def create_git_connector_for_argocd(project_name: str) -> GitConnector:
    gitops_repo_config = {
        "url": settings.GIT_ARGO_APPLICATIONS_URL,
        "branch": settings.GIT_ARGO_APPLICATIONS_BRANCH,
        "password": settings.GIT_ARGO_APPLICATIONS_PASSWORD,
        "username": settings.GIT_ARGO_APPLICATIONS_USERNAME,
        "project_name": project_name,
        "name": "argo",
        # Caching works automatically!
    }
    return await create_git_connector_from_repo_config(gitops_repo_config)

# Even ad-hoc git operations benefit!
connector = GitConnector(
    repo_url="https://github.com/user/manifests.git",
    branch="feature/xyz",
    password="token"
)
# ^ This will automatically use cache too!
```

**To disable caching for specific repo (rare):**

```python
connector = GitConnector(
    repo_url="https://example.com/volatile-repo.git",
    branch="main",
    use_cache=False  # Explicitly disable
)
```

## Deployment Changes

### 1. Add PVC to Kustomization

File: `bootstrap/rig-system/kustomize/operations-manager/base/pvc.yaml`

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: operations-manager-git-cache
  namespace: rig-system
spec:
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: 5Gi  # Enough for multiple repos + branches
  storageClassName: standard
```

Add to `kustomization.yaml`:

```yaml
resources:
  - deployment.yaml
  - service.yaml
  - pvc.yaml  # ADD THIS
```

### 2. Update Deployment

Add volume mount to `deployment.yaml`:

```yaml
spec:
  template:
    spec:
      containers:
      - name: operations-manager
        volumeMounts:
        - name: git-cache
          mountPath: /var/cache/git
        env:
        - name: GIT_CACHE_DIR
          value: "/var/cache/git"
        - name: GIT_CACHE_TTL_SECONDS
          value: "300"  # 5 minutes
      volumes:
      - name: git-cache
        persistentVolumeClaim:
          claimName: operations-manager-git-cache
```

## Usage Examples

### Automatic Caching (Zero Code Changes)

```python
# All these automatically use caching:

# 1. Project files repo
connector = await create_git_connector_for_project_files("my-project")
# Cache key: github-com-org-projects-main

# 2. ArgoCD applications repo
connector = await create_git_connector_for_argocd("my-project")
# Cache key: gitlab-com-group-argo-apps-main

# 3. Manifest generation with feature branches
connector = GitConnector(
    repo_url="https://github.com/company/manifests.git",
    branch="feature/new-dashboard"
)
# Cache key: github-com-company-manifests-feature-new-dashboard

# 4. Multiple repos, multiple branches - all cached separately
conn1 = GitConnector("https://github.com/user/repo1.git", branch="main")
conn2 = GitConnector("https://github.com/user/repo1.git", branch="develop")
conn3 = GitConnector("https://github.com/user/repo2.git", branch="main")
# Each gets its own cache directory - no collisions!
```

### Cache Management API

```python
from opi.utils.git_cache import get_git_cache_manager

cache = get_git_cache_manager()

# Get cache statistics
stats = cache.get_cache_stats()
# {
#   "total_repositories": 5,
#   "total_cache_hits": 247,
#   "total_fetches": 18,
#   "cache_hit_ratio": 0.932
# }

# Get metadata for specific repo
metadata = cache.get_metadata(
    "https://github.com/user/repo.git",
    "main"
)
# {
#   "repo_url": "https://github.com/user/repo.git",
#   "branch": "main",
#   "last_fetched": "2026-01-14T16:30:00Z",
#   "commit_sha": "a298892...",
#   "fetch_count": 5,
#   "cache_hits": 42
# }

# Clear specific repo cache
cache.clear_cache(
    repo_url="https://github.com/user/old-repo.git",
    branch="main"
)

# Clear all cache (useful for debugging)
cache.clear_cache()
```

### Optional: Add Cache Stats Endpoint

Add to `opi/api/router.py`:

```python
@router.get("/cache/stats")
async def get_cache_stats():
    """Get git cache statistics."""
    from opi.utils.git_cache import get_git_cache_manager

    cache = get_git_cache_manager()
    stats = cache.get_cache_stats()
    all_metadata = cache.get_all_metadata()

    return {
        "statistics": stats,
        "repositories": [
            {
                "cache_key": key,
                "repo_url": meta.get("repo_url"),
                "branch": meta.get("branch"),
                "last_fetched": meta.get("last_fetched"),
                "commit_sha": meta.get("commit_sha", "")[:8],
                "fetch_count": meta.get("fetch_count", 0),
                "cache_hits": meta.get("cache_hits", 0),
            }
            for key, meta in all_metadata.items()
        ]
    }
```

## Benefits

### Performance Improvements

**Before (full clone every restart):**
- First startup: 5-30 seconds
- Subsequent restarts: 5-30 seconds each

**After (with cache):**
- First startup: 5-30 seconds (full clone)
- Cache hit (< 5 min): ~0.1 seconds (metadata check)
- Cache expired: 1-3 seconds (git fetch + reset)

**Typical development workflow:**
- Rebuild & restart operations-manager every 2-3 minutes
- With cache: startup overhead reduced from 10-30s to ~0.1s
- **Total time saved**: 10-30 seconds per restart

### Resource Savings

- **Network bandwidth**: ~95% reduction (only fetch deltas)
- **Git server load**: Reduced from full clones to lightweight fetches
- **Storage**: 1GB PVC vs ephemeral temp storage

## Configuration Options

All configurable via environment variables:

```yaml
# In deployment.yaml or .env.local
GIT_CACHE_DIR: /var/cache/git              # Cache directory
GIT_CACHE_TTL_SECONDS: 300                 # 5 minutes default
GIT_CACHE_METADATA_FILE: .cache_metadata.json
```

**Tuning Guidelines:**

- **Development**: `GIT_CACHE_TTL_SECONDS=300` (5 minutes) - faster iteration
- **Production**: `GIT_CACHE_TTL_SECONDS=60` (1 minute) - fresher data
- **CI/CD**: `GIT_CACHE_TTL_SECONDS=0` - disable cache for clean builds

## Cache Invalidation

Cache is automatically invalidated when:

1. **TTL expires** (default 5 minutes)
2. **Corruption detected** (invalid .git directory)
3. **Manual invalidation**: Delete PVC contents

Manual invalidation:

```bash
# Delete cache
kubectl exec -n rig-system deployment/operations-manager -- rm -rf /var/cache/git/*

# Restart to rebuild cache
kubectl rollout restart -n rig-system deployment/operations-manager
```

## Monitoring

Add metrics to track cache effectiveness:

```python
# In opi/utils/git_cache.py
from prometheus_client import Counter, Histogram

git_cache_hits = Counter('git_cache_hits_total', 'Total cache hits', ['repo'])
git_cache_misses = Counter('git_cache_misses_total', 'Total cache misses', ['repo'])
git_fetch_duration = Histogram('git_fetch_duration_seconds', 'Git fetch duration', ['operation'])
```

Log messages:

```
INFO - Using cached git repository: projects (0.1s)
INFO - Cache expired, fetching updates for: projects (2.3s)
INFO - Full clone required for: projects (15.7s)
```

## Rollback Plan

If issues occur:

1. **Disable cache globally**: Remove `/var/cache/git` volume mount from deployment
2. **Or disable per-connector**: Pass `use_cache=False` to GitConnector
3. **Revert deployment**: Remove volume mount and PVC
4. **Delete PVC**: `kubectl delete pvc operations-manager-git-cache -n rig-system`

**No data loss** - git caches are read-only copies. Original data is always in Git remote.

## Key Advantages of Generic Design

### 1. Universal Application

✅ **Works everywhere automatically**
- Project files repository
- ArgoCD applications repository
- Manifest generation repos
- Any future git operations

### 2. Zero Maintenance Overhead

✅ **No manual cache key management**
- Automatic key generation from URL + branch
- No collisions between repos/branches
- No "name" parameter to remember

### 3. Feature Branch Support

✅ **Each branch gets its own cache**
```python
# These create separate caches:
GitConnector("https://github.com/user/repo.git", branch="main")
GitConnector("https://github.com/user/repo.git", branch="feature/xyz")
GitConnector("https://github.com/user/repo.git", branch="develop")
```

### 4. Transparent to Application Code

✅ **No code changes needed**
- Existing GitConnector calls work unchanged
- Factory functions work unchanged
- Tests work unchanged

### 5. Observable and Manageable

✅ **Built-in observability**
- Cache hit/miss metrics
- Per-repo statistics
- Total cache effectiveness
- API endpoint for monitoring

### 6. Safe and Reliable

✅ **Robust error handling**
- Corrupted cache? Falls back to clone
- Missing cache? Works like before
- Lock-free (thread-safe singleton)
- No race conditions

### 7. Easy to Disable

✅ **Opt-out when needed**
```python
# Disable for specific volatile repo
GitConnector(url="...", use_cache=False)

# Or disable globally in config
GIT_CACHE_DIR = ""  # Cache disabled if dir doesn't exist
```

## Future Enhancements

1. **Shared cache**: Multiple pods sharing the same cache (requires ReadWriteMany PVC)
2. **Cache warming**: Pre-clone repos during image build
3. **Webhook invalidation**: Invalidate cache on git push events
4. **Per-repo TTL**: Different TTL for project files vs argo applications
5. **Cache compression**: Compress old caches to save space
6. **Smart prefetching**: Pre-fetch likely branches based on usage patterns

## Related Features

- [Auto Database Provisioning](./auto-database-provisioning.md) - Also benefits from faster startups
- GitOps Deployment - Cache applies to both project files and argo application repos

---

**Last Updated**: 2026-01-14
**Status**: Planned / Not Implemented
**Priority**: High (significant development experience improvement)
**Estimated Implementation**: 2-4 hours
