Chisel server allows remote clients to establish HTTP/HTTPS tunnels to services within this cluster. This is useful for:
- Cross-cluster database cloning
- Accessing services from clusters behind firewalls
- Temporary access for migration/backup operations

### Client Usage

```bash
# PostgreSQL
chisel client --auth admin:pass https://chisel.domain.com \
  5432:postgres.db.svc:5432

# MinIO
chisel client --auth admin:pass https://chisel.domain.com \
  9000:minio.storage.svc:9000

# Redis
chisel client --auth admin:pass https://chisel.domain.com \
  6379:redis.cache.svc:6379
```
