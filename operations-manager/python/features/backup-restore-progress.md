# Future Feature: Backup/Restore Progress Tracking

## Overview

Currently, backup and restore operations run synchronously and block until completion with no visibility into progress. This feature would enable real-time progress tracking for all backup/restore operations.

## Current State

- Backup/restore operations spawn Kubernetes pods
- API endpoints block until pod completion (`_wait_for_pod()`)
- Logs are only fetched after completion or on failure
- No progress visibility during long-running operations

## Tool Progress Capabilities

All tools we use DO output progress information to stdout:

| Tool | Progress Output | Format Example |
|------|----------------|----------------|
| **kopia snapshot** | `--progress` (default ON) | `hashing 5, hashed 156 (2.1 GB), uploaded 2.1 GB, 45% done, ETA 2m30s` |
| **mc mirror** | Built-in progress bar | `123 files, 4.5 GB transferred, 2.3 GB/s` |
| **pg_dump** | `--verbose` | `dumping table "users"` (no percentage) |
| **pg_dump + pv** | Pipe viewer | `2.1GB 0:01:30 [24.1MB/s] [=========>] 45% ETA 0:01:45` |

## Proposed Architecture

### 1. Async Job Submission

Instead of blocking, return a job ID immediately:

```python
# Current (blocking)
POST /api/v1/backup/project/{project}/deployment/{deployment}
Response: { "status": "success", "snapshot_id": "..." }  # After minutes

# Proposed (async)
POST /api/v1/backup/project/{project}/deployment/{deployment}
Response: { "job_id": "backup-abc123", "status": "submitted" }  # Immediate
```

### 2. Redis for Progress Storage

Use Redis to store job state and progress:

```python
# Job structure in Redis
job:{job_id} = {
    "job_id": "backup-abc123",
    "type": "backup",  # or "restore"
    "resource_type": "pvc",  # or "database", "bucket"
    "project": "my-project",
    "deployment": "staging",
    "status": "running",  # pending, running, completed, failed
    "pod_name": "backup-pvc-abc123",
    "namespace": "rig-my-project",
    "started_at": "2024-01-15T10:30:00Z",
    "progress": {
        "percentage": 45,
        "bytes_processed": 2147483648,
        "bytes_total": 4772185088,  # If known (e.g., database size)
        "files_processed": 156,
        "current_operation": "uploading",
        "eta_seconds": 150,
        "message": "uploaded 2.1 GB, 45% done, ETA 2m30s"
    },
    "result": null  # Filled on completion
}

# TTL: Auto-expire after 24 hours
```

### 3. Progress Streaming from Pod Logs

Stream pod logs and parse tool-specific progress:

```python
async def _stream_and_track_progress(
    self,
    namespace: str,
    pod_name: str,
    job_id: str,
    resource_type: str
) -> bool:
    """Stream pod logs and update progress in Redis."""
    redis = get_redis_client()

    # Use kubectl logs -f for streaming
    process = await asyncio.create_subprocess_exec(
        "kubectl", "logs", "-f", pod_name, "-n", namespace,
        stdout=asyncio.subprocess.PIPE
    )

    async for line in process.stdout:
        line = line.decode().strip()
        progress = parse_progress(line, resource_type)
        if progress:
            await redis.hset(f"job:{job_id}", "progress", json.dumps(progress))

    return process.returncode == 0
```

### 4. Progress Parsing per Tool

```python
def parse_kopia_progress(line: str) -> dict | None:
    """Parse kopia progress output.

    Example: "hashing 5, hashed 156 (2.1 GB), uploaded 2.1 GB, 45% done, ETA 2m30s"
    """
    match = re.search(r'uploaded ([\d.]+\s*\w+),\s*(\d+)%\s*done(?:,\s*ETA\s*([\dm\s]+))?', line)
    if match:
        return {
            "bytes_uploaded": parse_size(match.group(1)),
            "percentage": int(match.group(2)),
            "eta": match.group(3),
            "message": line
        }
    return None

def parse_mc_mirror_progress(line: str) -> dict | None:
    """Parse mc mirror progress output."""
    # mc mirror outputs: "Total: X files, Y GB"
    match = re.search(r'(\d+)\s*files?,\s*([\d.]+\s*\w+)', line)
    if match:
        return {
            "files_processed": int(match.group(1)),
            "bytes_transferred": parse_size(match.group(2)),
            "message": line
        }
    return None

def parse_pg_dump_progress(line: str, total_tables: int = None) -> dict | None:
    """Parse pg_dump verbose output.

    Example: "dumping contents of table \"public.users\""
    """
    match = re.search(r'dumping.*table\s+"([^"]+)"', line)
    if match:
        return {
            "current_table": match.group(1),
            "message": line
        }
    return None
```

### 5. Database Size Pre-calculation

For database backups, pre-calculate size to enable percentage tracking:

```bash
# In backup-database-pod.yaml.jinja, add size calculation:
echo "Calculating database size..."
DB_SIZE=$(PGPASSWORD="${DB_PASSWORD}" psql \
  -h "${DB_HOST}" -p "${DB_PORT}" -U "${DB_USER}" -d "${DB_NAME}" \
  -t -c "SELECT pg_database_size('${DB_NAME}')")
echo "DATABASE_SIZE_BYTES=${DB_SIZE}"

# Then use pv for progress:
PGPASSWORD="${DB_PASSWORD}" pg_dump ... | pv -s ${DB_SIZE} | kopia snapshot create --stdin-file ...
```

### 6. API Endpoints

```python
# Submit backup job (async)
POST /api/v1/backup/project/{project}/deployment/{deployment}
Response: {
    "job_id": "backup-abc123",
    "status": "submitted"
}

# Get job status and progress
GET /api/v1/jobs/{job_id}
Response: {
    "job_id": "backup-abc123",
    "status": "running",
    "progress": {
        "percentage": 45,
        "bytes_processed": 2147483648,
        "eta_seconds": 150,
        "message": "uploaded 2.1 GB, 45% done"
    }
}

# List active jobs for a project
GET /api/v1/jobs?project={project}&status=running
Response: {
    "jobs": [...]
}

# Cancel a job
DELETE /api/v1/jobs/{job_id}
Response: {
    "status": "cancelled"
}

# Optional: SSE endpoint for real-time streaming
GET /api/v1/jobs/{job_id}/stream
Content-Type: text/event-stream
data: {"percentage": 45, "message": "uploading..."}
data: {"percentage": 46, "message": "uploading..."}
...
```

## Implementation Phases

### Phase 1: Infrastructure
- [ ] Add Redis dependency to operations-manager
- [ ] Create job tracking service with Redis storage
- [ ] Add job cleanup (TTL-based expiration)

### Phase 2: Async Job Execution
- [ ] Refactor backup/restore methods to run async
- [ ] Return job ID immediately from API endpoints
- [ ] Implement job status endpoint

### Phase 3: Progress Streaming
- [ ] Implement `kubectl logs -f` streaming
- [ ] Add progress parsers for each tool (kopia, mc, pg_dump)
- [ ] Update Redis with parsed progress

### Phase 4: Database Size Estimation
- [ ] Add database size pre-calculation to backup pod
- [ ] Add `pv` to backup container image
- [ ] Enable percentage tracking for database backups

### Phase 5: UI Integration
- [ ] Add progress indicators to web UI
- [ ] Show running jobs list
- [ ] Add cancel functionality

## Considerations

### Backward Compatibility
- Keep existing synchronous endpoints working
- Add `async=true` query parameter to opt-in to async mode
- Or create new `/async/` endpoints

### Error Handling
- Job timeout handling (pod deadline exceeded)
- Orphaned job cleanup
- Pod failure detection and status update

### Scalability
- Redis clustering for HA
- Job queue for rate limiting
- Maximum concurrent jobs per project

## Dependencies

- Redis (new dependency)
- `pv` tool in backup container image (for database progress)

## Related Files

- `opi/manager/backup/base.py` - Base backup manager with `_wait_for_pod()`
- `opi/manager/backup/pvc_backup.py` - PVC backup implementation
- `opi/manager/backup/database_backup.py` - Database backup implementation
- `opi/manager/backup/bucket_backup.py` - Bucket backup implementation
- `manifests/backup-*.yaml.jinja` - Pod templates
