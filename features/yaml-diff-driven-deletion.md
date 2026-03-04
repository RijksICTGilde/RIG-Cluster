# YAML Diff-Driven Deletion & Resource Cleanup

## What It Is

When a user removes a deployment from their project YAML file, the system automatically detects the removal and cleans up the associated infrastructure resources. Persistent data resources (databases, MinIO buckets) are protected by a two-phase deletion process with a configurable grace period, allowing accident recovery via git revert.

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
   - Deployment manifests from git repositories
   - Subdomain registrations

2. **Mark for deferred deletion** (persistent data resources):
   - PostgreSQL databases and users
   - MinIO buckets, users, and policies
   - Namespaces containing persistent resources

### Reconciliation

A reconciliation job periodically:
- Compares expected resources (from project YAMLs) against marked resources
- Purges resources that are both marked AND past the grace period
- Unmarks resources that reappear in project YAMLs (git revert recovery)

## Configuration

| Setting | Default | Description |
|---------|---------|-------------|
| `DELETION_GRACE_PERIOD_DAYS` | `7` | Days before marked resources are eligible for purging |

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
| `opi/manager/delete_project_manager.py` | New `delete_deployment_from_yaml_change()` method |
| `opi/manager/project_manager.py` | Wired deletion into `process_project_from_git()` |
| `opi/jobs/reconciliation.py` | Periodic reconciliation job |
| `opi/core/config.py` | `DELETION_GRACE_PERIOD_DAYS` setting |
