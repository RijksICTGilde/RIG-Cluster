# Redis Cloning - Consideration

## Context

The RIG platform supports cloning for databases (pg_dump/pg_restore) and MinIO buckets (bucket mirroring) between deployments. This document evaluates whether Redis cloning should be added as well.

## Current Redis Architecture

Each project/deployment gets:
- A dedicated Redis ACL user with a unique password
- Key and channel restrictions via prefix: `{deployment}-{project}:*`
- Full command access (`+@all`) within that prefix
- Pub/sub channel access within the same prefix pattern

## How Redis is Typically Used

| Use Case | Data Lifetime | Cloning Value |
|---|---|---|
| Caching (most common) | Ephemeral, derived from database | Low - cache is regenerated from source of truth |
| Session storage | Ephemeral, user-specific | None - sessions are environment-specific |
| Pub/sub messaging | Transient, fire-and-forget | None - no data to clone |
| Lists as queues | Consumed and discarded | Low - queue state is environment-specific |
| Lists as message history | Semi-persistent | Medium - demo/testing value |
| Streams | Persistent event log | Medium - could be useful for replaying events |
| Sorted sets / leaderboards | Application-managed | Medium - depends on whether data references other env-specific resources |

## Technical Feasibility

Cloning is technically possible using Redis `SCAN` + `DUMP`/`RESTORE`:

1. Connect with admin credentials to source Redis
2. `SCAN` all keys matching source prefix (`{source-deployment}-{source-project}:*`)
3. For each key: `DUMP` the serialized value (type-agnostic, preserves TTLs)
4. Connect with admin credentials to target Redis
5. `RESTORE` each key under the target prefix (`{target-deployment}-{target-project}:*`)

### Challenges

- **Prefix rewriting**: Key names contain the source prefix and must be rewritten to the target prefix
- **Admin access required**: ACL users are restricted to their own prefix, so cloning requires admin credentials
- **Cross-instance cloning**: If source and target are on different Redis instances, data must transit through the Operations Manager
- **TTL handling**: Cloned keys may have near-expired TTLs that are meaningless in the new context
- **Data references**: Cached data often contains environment-specific references (database IDs, URLs, session tokens) that become invalid in another deployment

## Arguments For

- Feature parity with database and MinIO cloning
- Useful for demo/testing scenarios with persistent list data
- Could help with Redis Streams if used as event logs

## Arguments Against

- Redis is designed as an ephemeral cache/broker — cloning cache data is an anti-pattern
- Cloned cache data is likely stale or contains invalid cross-environment references
- Applications should be resilient to empty Redis (cold start) by design
- Adds operational complexity for a rarely-needed feature
- If data is important enough to clone, it probably belongs in PostgreSQL

## Recommendation

**Do not implement Redis cloning at this time.** The primary use cases for Redis (caching, sessions, pub/sub) do not benefit from cloning. Applications should handle an empty Redis gracefully.

Revisit this decision if:
- Redis Streams are adopted for persistent event sourcing
- A concrete use case emerges where Redis holds non-derived, non-ephemeral data that must be transferred between deployments
