i# Redis ACL Persistence

## Problem

Redis was configured with `--requirepass` for authentication and `--appendonly yes` for data persistence. While key/value data survived pod restarts, ACL users created at runtime via `ACL SETUSER` were stored only in memory. Any pod restart (crash, scaling, node drain, infrastructure sync) would wipe all per-project ACL users, breaking Redis connectivity for all projects until they were refreshed.

## Decision

Persist Redis ACL users to disk using the `--aclfile` flag, and call `ACL SAVE` after every user creation or deletion.

## Implementation

### Infrastructure (`infrastructure/bootstrap/infrastructure/redis/controller/base/deployment.yaml`)

- **Removed** `--requirepass $(REDIS_PASSWORD)` from the Redis startup command
- **Added** `--aclfile /data/users.acl` — tells Redis to load and persist ACL users from/to this file on the PVC
- **Added** an init container (`init-aclfile`) that seeds the ACL file with the `default` user and admin password on first run (when the file doesn't exist or is empty). This is necessary because Redis requires the ACL file to exist at startup, and an empty file would leave the `default` user without a password.

### Operations Manager (`opi/manager/redis_manager.py`)

- **Added** `ACL SAVE` after every `ACL SETUSER` in `_create_acl_user` — flushes new users to disk immediately
- **Added** `ACL SAVE` after every `ACL DELUSER` in `_delete_acl_user` — flushes deletions to disk immediately

## How it works

1. On first deployment (or after PVC wipe), the init container creates `/data/users.acl` with:
   ```
   user default on ><admin-password> ~* &* +@all
   ```
2. Redis starts with `--aclfile /data/users.acl` and loads all users from the file
3. When OPI creates or deletes a per-project ACL user, it issues `ACL SAVE` afterward to flush changes to disk
4. On pod restart, Redis reloads all users from the ACL file — no users are lost

## Compatibility

- Applications using `AUTH <password>` (without username) continue to work — Redis authenticates against the `default` user
- Applications using `AUTH default <password>` continue to work identically
- Per-project ACL users (e.g., `staging-zdvm-9hx`) now survive restarts

## Edge cases

- **Admin password rotation**: If the `redis-admin-credentials` secret changes after the ACL file was already seeded, the init container will not overwrite it (it only seeds when the file is empty). The default user's password in Redis must be updated manually via `ACL SETUSER default on ><new-password> ~* &* +@all` followed by `ACL SAVE`, or by deleting the ACL file and restarting the pod.
