# Job Runner ("Job uitvoeren")

Run one-off Kubernetes Jobs from the ZAD self-service portal. Useful for database migrations, data imports, one-time scripts, and ad-hoc maintenance tasks.

## How It Works

1. Navigate to a project's **Deployments** tab
2. Click **"Job uitvoeren"** on any deployment
3. Fill in the form:
   - **Container image** (required) - e.g., `alpine:latest` or `registry.example.com/my-tool:v1`
   - **Command** (optional) - shell command executed as `/bin/sh -c "..."`
   - **Environment variables** (optional) - `KEY=VALUE` format, one per line
   - **Services** - checkboxes for all deployment services (PostgreSQL, MinIO, Keycloak, Redis). Selected services inject their credentials as environment variables into the job container.
4. Review the confirmation summary
5. Click **"Job starten"** to execute

## Service Selection

The form automatically detects which services the deployment uses across all its components and presents them as checkboxes (all selected by default). When a service is selected, the corresponding Kubernetes Secret is mounted via `envFrom`, making all service credentials available as environment variables:

| Service | Secret pattern | Example variables |
|---------|---------------|-------------------|
| PostgreSQL | `{deployment}-database` | `DATABASE_SERVER_HOST`, `DATABASE_PASSWORD`, etc. |
| MinIO | `{deployment}-minio` | `OBJECT_STORE_HOST`, `OBJECT_STORE_PASSWORD`, etc. |
| Keycloak | `{deployment}-keycloak` | `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`, etc. |
| Redis | `{deployment}-redis` | `REDIS_HOST`, `REDIS_PASSWORD`, etc. |

The service-to-secret mapping is defined in `opi/utils/secrets.py:get_deployment_service_secrets()`, which is the single source of truth shared with the project_manager's manifest generation.

## Technical Details

- **Kubernetes resource**: `batch/v1 Job` (not a bare Pod)
- **Pre-provisioning**: Runs the full ProjectManager flow (`process_project_from_git`) before the job to ensure namespace, databases, secrets, and ArgoCD sync are all up to date. This is idempotent.
- **Execution**: Direct `kubectl apply` (jobs are transient, not managed via GitOps)
- **Timeout**: 1 hour (`activeDeadlineSeconds: 3600`)
- **Retries**: None (`backoffLimit: 0`) - user re-triggers manually
- **Cleanup**: Job is deleted after completion/failure. Safety net: `ttlSecondsAfterFinished: 300`
- **Resource limits**: 512Mi memory, 500m CPU
- **Progress tracking**: In-memory `TaskProgressManager` with HTMX polling (same as backup/restore)
- **Security**: Requires SSO + admin/owner role. Job runs in the project's own namespace.
- **Job manifest**: Mirrors the deployment template's security context, CA certificate mounts, and imagePullSecrets patterns (`manifests/job.yaml.jinja`)

## Architecture

```
Button click
    |
    v
GET /projects/{name}/run-job/{deployment}  --> Form HTML
    |
    v
POST /projects/{name}/run-job/{deployment} --> Confirm HTML
    |
    v
POST .../confirm --> BackgroundTask(run_job_task)
    |                     |
    v                     v
Progress polling    1. ProjectManager.process_project_from_git()
(HTMX every 2s)       (ensures secrets, namespace, ArgoCD sync)
                    2. JobManager.run_job()
                        +-- render job.yaml.jinja
                        +-- kubectl apply -f -
                        +-- poll job status every 5s
                        +-- collect pod logs
                        +-- kubectl delete job (cleanup)
```

## Shared Code

- `opi/utils/secrets.py:get_deployment_service_secrets()` — single source of truth for service→secret mapping
- `opi/core/backup_tasks.py:_resolve_deployment_info()` — namespace/cluster resolution
- `opi/generation/manifests.py:render_template()` — Jinja2 manifest rendering
- `KubectlConnector` for direct cluster access
- `TaskProgressManager` + progress polling infrastructure
- `validate_and_parse_env_vars()` for env var parsing
- Modal wizard UI framework (edit-section-modal)
