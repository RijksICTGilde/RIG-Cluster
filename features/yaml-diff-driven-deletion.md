# YAML Diff-Driven Deletion & Resource Cleanup

## What It Is

When a user removes a deployment or a service from their project YAML file, the system automatically detects the change and cleans up the associated infrastructure resources. Persistent data resources (databases, MinIO buckets) are protected by a two-phase deletion process with a configurable grace period, allowing accident recovery via git revert.

## How It Works

### Change Detection

The existing `ProjectFileHandler.analyze_project_changes()` pipeline uses DeepDiff to compare previous and current YAML versions. The `_analyze_deployment_changes()` method categorizes changes into added/changed/deleted buckets.

**Bug fix**: A regex bug in `_parse_deepdiff_path()` previously caused all list-based deletions to be silently dropped because DeepDiff reports list removals as `root['deployments'][1]` which was not being converted to `deployments.1` dot notation.

### Two-Phase Deletion

When a deployment is removed from YAML:

1. **Immediate deletion** (ephemeral resources):
   - ArgoCD application files and AppProject files
   - Repository secret files
   - Keycloak clients
   - Redis ACL users
   - Deployment manifests from git repositories
   - Subdomain registrations

2. **Mark for deferred deletion** (persistent data resources):
   - PostgreSQL databases and users
   - MinIO buckets, users, and policies
   - Backup data (Kopia snapshots for the deployment)
   - Namespaces containing persistent resources

### Service-Level Change Detection

When a deployment *survives* a YAML change but one or more services are removed from it (e.g., `postgresql-database` dropped from a component's `uses-services`), the system detects the removal and triggers the same cleanup flow.

Each `ServiceDefinition` declares a `cleanup_strategy`:

| Strategy | Services | Behavior |
|----------|----------|----------|
| `deferred` | postgresql-database, namespace-postgresql-database, minio-storage | Mark for deferred deletion (data is precious) |
| `immediate` | redis, namespace-redis, keycloak | Delete right away (ephemeral/recreatable) |
| `none` | publish-on-web, authorization-wall, persistent-storage, temp-storage | No server-side resources to clean up |

Each service manager owns its cleanup logic via `handle_service_removal()`, which decides internally whether to mark or delete based on the availability of a `MarkedForDeletionService`. The orchestrator (`cleanup_removed_services_from_yaml_change`) iterates cleanable services, detects removals using `deployment_uses_service()`, and delegates to the appropriate manager.

### Reconciliation

A reconciliation job can be triggered via the admin API:
- Compares expected resources (from project YAMLs) against marked resources
- Purges resources that are both marked AND past the grace period
- Unmarks resources that reappear in project YAMLs (git revert recovery)

### Admin API

The admin router (`/api/v2/admin/`) provides manual control over the cleanup lifecycle. Authenticated via `ADMIN_API_KEY` (separate from `MASTER_API_KEY`).

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/v2/admin/marked-for-deletion` | GET | List marked resources (filterable by `project_name`) |
| `/api/v2/admin/cleanup/trigger` | POST | Purge expired marks for a specific project |
| `/api/v2/admin/reconciliation/trigger` | POST | Run full reconciliation cycle |
| `/api/v2/admin/marked-for-deletion/{mark_id}` | DELETE | Cancel a specific scheduled deletion |

Both trigger endpoints default to `dry_run=true` for safety. The cleanup endpoint works by `project_name` from the database, so it can clean up resources even after a project has been removed.

## Configuration

| Setting | Default | Description |
|---------|---------|-------------|
| `DELETION_GRACE_PERIOD_DAYS` | `7` | Days before marked resources are eligible for purging |
| `ADMIN_API_KEY` | `None` | API key for admin endpoints (cleanup, reconciliation). If not set, admin endpoints return 501. |

## Database Schema

The `marked_for_deletion` table tracks resources awaiting deferred deletion:

| Column | Type | Description |
|--------|------|-------------|
| `id` | UUID | Primary key |
| `resource_type` | VARCHAR(50) | e.g., `postgresql_database`, `minio_bucket`, `namespace` |
| `resource_name` | VARCHAR(255) | Name of the resource |
| `project_name` | VARCHAR(255) | Owning project |
| `deployment_name` | VARCHAR(255) | Owning deployment |
| `cluster` | VARCHAR(100) | Target cluster |
| `marked_at` | TIMESTAMPTZ | When the resource was first marked |
| `metadata` | JSONB | Additional info needed for deletion (server, namespace, etc.) |

A unique index on `(resource_type, resource_name, cluster)` prevents duplicate marks.

## Resource Types

| Type | Example Name | Metadata |
|------|-------------|----------|
| `postgresql_database` | `myproject_staging` | `{"server": "postgresql.kind"}` |
| `postgresql_user` | `myproject_staging` | `{"server": "postgresql.kind"}` |
| `minio_bucket` | `myproject-staging` | `{"server_alias": "minio"}` |
| `minio_user` | `myproject_staging` | `{"server_alias": "minio"}` |
| `minio_policy` | `myproject_staging-myproject-staging-policy` | `{"server_alias": "minio"}` |
| `backup_data` | `backup-rig-myproject-local/local/rig-myproject` | `{"s3_bucket": "...", "s3_prefix": "...", "kopia_password": "...", "namespace": "..."}` |
| `namespace` | `rig-myproject` | `{"has_marked_pvcs": true}` |

## Accident Recovery

If a deployment is accidentally removed from YAML and the user reverts the git commit:
- The reconciliation job detects the resource is back in the expected set
- It automatically unmarks the resource, preserving all data
- No data is lost as long as the revert happens within the grace period

## Dependencies

- PostgreSQL database (same instance as `async_tasks` and `subdomain_registry`)
- Alembic migration `002_add_marked_for_deletion`
- DeepDiff for YAML change detection

## Files

| File | Purpose |
|------|---------|
| `opi/handlers/project_file_handler.py` | Fixed path parsing for numeric indices |
| `opi/core/marked_for_deletion_schema.py` | SQL schema for the table |
| `opi/services/marked_for_deletion_service.py` | CRUD service layer |
| `opi/migrations/versions/002_add_marked_for_deletion.py` | Alembic migration |
| `opi/services/services.py` | `cleanup_strategy` on `ServiceDefinition` |
| `opi/manager/database_manager.py` | `handle_service_removal()` for PostgreSQL |
| `opi/manager/minio_manager.py` | `handle_service_removal()` for MinIO |
| `opi/manager/redis_manager.py` | `handle_service_removal()` for Redis |
| `opi/manager/keycloak_manager.py` | `handle_service_removal()` for Keycloak |
| `opi/manager/delete_project_manager.py` | `delete_deployment_from_yaml_change()` and `cleanup_removed_services_from_yaml_change()` |
| `opi/jobs/reconciliation.py` | Reconciliation job: compares expected vs marked resources |
| `opi/api/admin_router.py` | Admin API endpoints for cleanup and reconciliation |
| `opi/manager/project_manager.py` | Wired deletion + service removal into `process_project_from_git()` |
| `opi/jobs/reconciliation.py` | Reconciliation job with backup purge support |
| `opi/api/admin_router.py` | Admin API endpoints for cleanup and reconciliation |
| `opi/api/endpoint_util.py` | `validate_admin_api_key` decorator |
| `opi/core/config.py` | `DELETION_GRACE_PERIOD_DAYS` and `ADMIN_API_KEY` settings |
