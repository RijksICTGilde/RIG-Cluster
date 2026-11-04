# Architecture Decision Records (ADR)

Lightweight records of significant architectural decisions. Each ADR captures the context, decision, and consequences so future contributors understand *why* things are the way they are.

## Format

Each ADR is a numbered markdown file: `NNNN-short-title.md`

### Template

```markdown
# NNNN - Title

**Status**: Proposed | Accepted | Superseded by NNNN | Deprecated
**Date**: YYYY-MM-DD

## Context

What is the issue or situation that motivates this decision?

## Decision

What is the change that we're proposing or have agreed to implement?

## Consequences

What becomes easier or harder as a result of this decision?
```

## Statuses

- **Proposed** - under discussion, not yet implemented
- **Accepted** - agreed upon, implementation in progress or complete
- **Superseded** - replaced by a newer ADR (link to it)
- **Deprecated** - no longer relevant

## Index

| # | Title | Status | Date |
|---|-------|--------|------|
| 0001 | [Remove FormField, use ResolvedEditableVisualizer](0001-remove-formfield-use-resolved-editable.md) | Proposed | 2026-03-09 |
| 0002 | [Wait for ArgoCD sync completion after project refresh](0002-argocd-sync-wait-after-project-refresh.md) | Accepted | 2026-03-11 |
| 0003 | [SeaweedFS replaces MinIO as S3 storage](0003-seaweedfs-replaces-minio-as-s3-storage.md) | Accepted | 2026-10-22 |
| 0004 | [Server-side sessions with a bounded hot cache](0004-server-side-sessions-with-bounded-hot-cache.md) | Accepted | 2026-10-27 |
| 0005 | [Hot cache evicts least recently used, not first in](0005-hot-cache-evicts-least-recently-used.md) | Accepted | 2026-10-27 |
