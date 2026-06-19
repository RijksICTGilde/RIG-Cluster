# OpenProject on ZAD — sandbox runbook

How OpenProject 17.5 was wired onto ZAD (sandbox), what env vars are needed, which
manual steps are required, and the known limitations. Use this if the sandbox is
rebuilt or to clone the setup for another tenant.

## Prerequisites

1. **ZAD branch deployed**: `claude/sandbox-uid-override` (or its successor) MUST
   be merged into main and the operations-manager redeployed. That branch carries
   the platform features this setup depends on:
   - per-component `security:` block (UID override)
   - per-component `command:` field
   - `OIDC_HOSTNAME` and `PUBLIC_HOSTNAME` variables
   - alias-parser fix and user-env-vars quote preservation
   - REDIS/NAMESPACE_REDIS categorisation fix
   - Secret manifests get `Replace=true` sync option

2. **OpenProject Community Edition does NOT support OIDC SSO out of the box**
   (gated by `EnterpriseToken.allows_to?(:sso_auth_providers)` in
   `modules/auth_plugins/lib/open_project/plugins/auth_plugin.rb`). For PoC we use
   a thin wrap image that patches the gate. Replace with a real Enterprise Token
   before any non-test deployment.

## Wrap image (EE bypass)

```dockerfile
FROM openproject/openproject:17.5-slim
USER 0
RUN sed -i \
    "s#EnterpriseToken.allows_to?(:sso_auth_providers) || name == \"developer\"#true#" \
    /app/modules/auth_plugins/lib/open_project/plugins/auth_plugin.rb \
 && grep -F 'true' /app/modules/auth_plugins/lib/open_project/plugins/auth_plugin.rb | head -3 \
 && echo "=== EE bypass patch applied ==="
USER 1000
```

Build + load into Kind:

```bash
docker build -t openproject-eebypass:17.5-slim .
docker tag openproject-eebypass:17.5-slim local/openproject-eebypass:17.5-slim
kind load docker-image openproject-eebypass:17.5-slim --name rig-sandbox
```

The `local/` prefix in the project YAML is a ZAD signal "do not pull from
registry, use the kind-loaded image". ZAD strips the prefix before writing
the K8s manifest, so kubelet looks for the unprefixed image name — hence
loading under both names.

## ZAD project YAML

`projects/openp-<slug>.yaml` in `zad-projects` repo. Skeleton showing the
non-default bits; ordinary fields (display-name, clusters, deployments, etc)
are omitted.

```yaml
services:
  - publish-on-web
  - keycloak:
      config:
        template: sso-support
        restrict-access:
          realm-role: allowed-user
          error-message: ${accessDeniedNoPermission}
  - postgresql-database
  - minio-storage
  - redis

components:
  - name: app
    image: local/openproject-eebypass:17.5-slim
    ports:
      inbound: [8080]
    security:
      run-as-user: 1000
      run-as-group: 1000
      fs-group: 1000
    command: [sh, -c, "/app/docker/prod/seeder && exec /app/docker/prod/web"]
    services:
      - publish-on-web
      - keycloak
      - postgresql-database
      - minio-storage
      - redis
    resources:
      requests: { memory: 1Gi, cpu: 500m }
      limits:   { memory: 2Gi, cpu: 2000m }
    aliases:
      # DB — single service substitution chain
      DATABASE_URL: postgresql://$DATABASE_SERVER_USER:$DATABASE_PASSWORD@$DATABASE_SERVER_HOST:$DATABASE_SERVER_PORT/$DATABASE_DB

      # Keycloak — note convention: __ = literal underscore in segment name,
      # _ = nesting separator. Config key openid_connect.keycloak.identifier
      # maps to OPENPROJECT_OPENID__CONNECT_KEYCLOAK_IDENTIFIER.
      OPENPROJECT_OPENID__CONNECT_KEYCLOAK_IDENTIFIER: $OIDC_CLIENT_ID
      OPENPROJECT_OPENID__CONNECT_KEYCLOAK_SECRET: $OIDC_CLIENT_SECRET
      OPENPROJECT_OPENID__CONNECT_KEYCLOAK_ISSUER: $OIDC_URL/realms/$OIDC_REALM
      OPENPROJECT_OPENID__CONNECT_KEYCLOAK_AUTHORIZATION__ENDPOINT: /realms/$OIDC_REALM/protocol/openid-connect/auth
      OPENPROJECT_OPENID__CONNECT_KEYCLOAK_TOKEN__ENDPOINT: /realms/$OIDC_REALM/protocol/openid-connect/token
      OPENPROJECT_OPENID__CONNECT_KEYCLOAK_USERINFO__ENDPOINT: /realms/$OIDC_REALM/protocol/openid-connect/userinfo
      OPENPROJECT_OPENID__CONNECT_KEYCLOAK_HOST: $OIDC_HOSTNAME

      # Public URL — OpenProject's host_name field requires bare hostname (no scheme)
      OPENPROJECT_HOST__NAME: $PUBLIC_HOSTNAME

      # MinIO S3 storage (via fog-aws)
      OPENPROJECT_FOG_DIRECTORY: $OBJECT_STORE_BUCKET_NAME
      OPENPROJECT_FOG_CREDENTIALS_AWS__ACCESS__KEY__ID: $OBJECT_STORE_USER
      OPENPROJECT_FOG_CREDENTIALS_AWS__SECRET__ACCESS__KEY: $OBJECT_STORE_PASSWORD
      OPENPROJECT_FOG_CREDENTIALS_ENDPOINT: http://$OBJECT_STORE_HOST:$OBJECT_STORE_PORT

      # Redis cache (active — see notes below)
      CACHE_REDIS_URL: $REDIS_URL
      OPENPROJECT_CACHE_NAMESPACE: $REDIS_PREFIX
      CACHE_NAMESPACE: $REDIS_PREFIX

    user-env-vars: |
      # 64-byte hex; generate with: openssl rand -hex 64
      SECRET_KEY_BASE: "<replace-with-fresh-secret>"
      OPENPROJECT_HTTPS: "true"
      OPENPROJECT_OPENID__CONNECT_KEYCLOAK_DISPLAY__NAME: Keycloak
      OPENPROJECT_ATTACHMENTS__STORAGE: "fog"
      OPENPROJECT_FOG_CREDENTIALS_PROVIDER: "AWS"
      OPENPROJECT_FOG_CREDENTIALS_PATH__STYLE: "true"
      OPENPROJECT_RAILS__CACHE__STORE: redis
      OPENPROJECT_DISABLE__PASSWORD__LOGIN: "true"
      OPENPROJECT_LOGIN__REQUIRED: "false"
      # Memory tuning (see "Notes on memory tuning")
      LD_PRELOAD: "libjemalloc.so.2"          # MUST set this; command: bypasses the entrypoint that would
      USE_JEMALLOC: "true"                     #   honour USE_JEMALLOC, so this var alone does nothing
      OPENPROJECT_WEB_WORKERS: "2"             # forked puma processes (CPU parallelism + resilience)
      OPENPROJECT_WEB_MIN__THREADS: "2"        # NOTE the double underscore before THREADS
      OPENPROJECT_WEB_MAX__THREADS: "8"        #   single underscore misroutes to web.max.threads and is ignored
```

### Notes on the alias block

- Three nesting positions: `openid_connect` is one segment with an underscore
  in its name (so `OPENID__CONNECT`); `keycloak` is a sub-key (preceded by
  single `_`); the leaf setting like `authorization_endpoint` is a segment
  with an underscore (so `AUTHORIZATION__ENDPOINT`).
- `DISPLAY__NAME` in `user-env-vars` is intentional — `display_name` is one
  key. If you accidentally write `DISPLAY_NAME` (single _) it becomes
  `openid_connect.keycloak.display.name` — wrong tree — and the seeder
  crashes with "undefined method 'merge' for an instance of String" because
  the provider entry ends up as a String, not a Hash.
- The redis-cache aliases (`CACHE_REDIS_URL`, `OPENPROJECT_CACHE_NAMESPACE`,
  `CACHE_NAMESPACE`) feed the active redis cache store. Set the Redis
  `maxmemory-policy` to an `allkeys-*` variant (e.g. `allkeys-lru`) — otherwise
  cache entries never expire and Redis eventually OOMs.
- `OPENPROJECT_CACHE_NAMESPACE` MUST be a bare token with no trailing `:`.
  ActiveSupport adds the `:` itself (`namespace_key` builds `"<ns>:<key>"`), and
  the shared `rig-redis` ACL only permits keys matching `~<prefix>:*`. ZAD's
  `$REDIS_PREFIX` is colon-free (see naming.py `generate_redis_key_prefix`), so
  the alias above is correct. Do NOT hand it a value ending in `:` — OpenProject
  YAML-parses env values, and `foo:` parses to the hash `{"foo"=>nil}`, which
  corrupts the namespace and every key read fails with `NOPERM`.
- The `CACHE_REDIS_URL` log line "Using unprefixed environment variables is
  deprecated. Please use OPENPROJECT_CACHE_REDIS_URL" is a cosmetic WARNING, not
  an error — the cache works with it. OpenProject does accept the prefixed name
  (`OPENPROJECT_CACHE_REDIS_URL`, or the unambiguous `OPENPROJECT_CACHE__REDIS__URL`),
  but its generic "CACHE_REDIS_URL is not set" message is a fixed string that
  always cites the unprefixed name regardless of which form you set — so if a
  rename appears to "miss" the value, check what ZAD actually rendered into the
  container env, not OpenProject.

### Notes on user-env-vars

- `OPENPROJECT_RAILS__CACHE__STORE: redis` — a shared cache so OpenProject can
  run more than one replica (file_store is pod-local and diverges across
  replicas, so it effectively pins you to a single replica). Earlier setups
  used `file_store` to dodge a positional-vs-kwargs bug in
  `cache_store_configuration` (`cache_config << parameters` appended the
  cache_namespace hash positionally and `RedisCacheStore.new` rejected it),
  present in OpenProject 17.4.0 + Rails 8.1. Fixed upstream in PR #23251 — a
  cache-serializer security fix that also restructured this to merge the params
  into the kwargs hash — shipped in 17.3.3 / 17.4.1 / 17.5.x. We pin a 17.5
  release (see wrap image above), so redis works. Do NOT drop back below
  17.4.1, or the bug returns and you must use `file_store`.
  Redis needs BOTH fixes: (1) the 17.5 image (this kwargs bug), and (2) a
  colon-free cache namespace (see the alias-block note on
  `OPENPROJECT_CACHE_NAMESPACE`). With only one of the two, the pod crash-loops.
- `OPENPROJECT_DISABLE__PASSWORD__LOGIN: "true"` removes the local-account
  login form. Only set this AFTER an OIDC user has been promoted to admin
  (see "Manual steps" below) or you lock yourself out.
- `OPENPROJECT_LOGIN__REQUIRED: "false"` allows anonymous read of public
  projects without forcing a Keycloak redirect.

### Notes on memory tuning

OpenProject runs Puma in clustered mode: a master forks `WEB_WORKERS`
processes, each running up to `WEB_MAX_THREADS` threads. Memory is dominated
by the workers (forked Ruby VMs). With `preload_app!` (default) the workers
share most pages copy-on-write, so the real footprint is far below the sum of
per-process RSS (e.g. master+2 workers showed ~650 MiB total via cgroup
`memory.current`, not 3x600 MiB).

- **Workers vs threads**: a worker is a full process → true CPU parallelism +
  fault isolation (a stuck/leaking worker is restarted without taking the
  others down). Threads only add I/O concurrency (Ruby's GVL serialises CPU
  work within a process), so they barely affect idle RSS. For a handful of
  users, `WEB_WORKERS: 2` with `MAX__THREADS: 8` is a good balance; drop to 1
  worker only for a pure idle/demo instance.
- **`LD_PRELOAD: "libjemalloc.so.2"` is mandatory for jemalloc here.** The
  image's entrypoint sets it from `USE_JEMALLOC=true`, but our `command:`
  override (seeder + web) bypasses the entrypoint, so `USE_JEMALLOC` alone is a
  no-op. Verify it actually loaded: `grep -c jemalloc /proc/1/maps` inside the
  pod (>0 = loaded). jemalloc does NOT lower cold-start RSS — its win is less
  fragmentation and returning freed memory over time, which curbs the slow
  creep toward the limit (and the per-boot seeder spike).
- **Double underscore in thread vars**: `web` is a hash setting, so the leaf
  key `max_threads` needs `OPENPROJECT_WEB_MAX__THREADS`. Single underscore
  (`..._MAX_THREADS`) misroutes to `web.max.threads`, is silently ignored, and
  you keep the default of ~16 threads (DB pool 17). Same trap as the cache
  namespace. Proof it took effect: the log "Increasing database pool size to N
  to match max threads" should show N = max_threads + 1.
- Optional: `MALLOC_CONF: "dirty_decay_ms:1000,muzzy_decay_ms:0"` makes
  jemalloc return idle memory to the kernel faster — only if you want the RSS
  number to visibly shrink between bursts.
- envFrom secret changes do NOT restart the pod; delete the pod (or roll the
  deployment) to pick up new env values.

## Manual steps after first deploy

1. **Bump pod startup probe budget** — first boot runs all 588 prisma-style
   migrations + seeder, default 185s budget is too tight. Survives until the
   next OPI re-render, so re-do this if you trigger a refresh:

   ```bash
   kubectl --context kind-rig-sandbox -n rig-openp-<slug> patch deploy productie-app \
     --type='json' -p='[{"op":"replace","path":"/spec/template/spec/containers/0/startupProbe/failureThreshold","value":120}]'
   ```

   See `features/futures/` for the configurable-probe field that should land
   in ZAD's project schema.

2. **Promote your Keycloak user to admin** (only the seeded local "admin"
   user has admin by default, and password login is disabled). After first
   Keycloak login, find your user id and flip the flag:

   ```bash
   kubectl --context kind-rig-sandbox -n rig-system exec rig-db-1 -c postgres -- \
     psql -U postgres -d openp_<slug>_productie -c \
     "SET search_path TO openp_<slug>_productie; \
      UPDATE users SET admin = true WHERE login = 'your.email@example.com';"
   ```

3. **Configure Anonymous role** in OpenProject admin (UI only):
   - `/admin/roles` → Anonymous → enable "View work packages", "View wiki", etc.
   - `/admin/roles` → Non-member → similar but ruimer
   - Per-project: Project settings → Information → enable "Public project"

## Replacing the bypass image with a real Enterprise Token

Once you have a valid OpenProject Enterprise Token, the wrap image is no
longer needed.

**Note on requesting the token**: OpenProject EE tokens (v2.0+) are bound
to a specific hostname. OpenProject's sales/onboarding team will ask for
the deployment URL — it ends up embedded in the token and validated at
runtime against `Setting.host_name` (= our `OPENPROJECT_HOST__NAME` env
var, set to `$PUBLIC_HOSTNAME`). Implication:

- Sandbox needs its own token (e.g. for `productie-openp-7lh.sandbox.rijksapp.dev`)
- Production needs a separate token for its final hostname
- Changing the hostname after the fact → request a new token
- Wildcard / multi-domain tokens exist but must be explicitly requested

Source: `app/models/enterprise_token.rb` — `invalid_domain?` calls
`token_object.valid_domain?(Setting.host_name)`.

Two ways to install the token:

1. **Via env var** (declarative, survives DB resets):

   Add to `user-env-vars`:
   ```yaml
   OPENPROJECT_SEED__ENTERPRISE__TOKEN: |
     <paste full token string here>
   ```

   Setting name in `config/constants/settings/definition.rb`:
   ```ruby
   seed_enterprise_token: {
     description: "Seed enterprise-edition token through ENV",
     writable: false,
     format: :string,
   }
   ```

   `seed_enterprise_token` → `OPENPROJECT_SEED__ENTERPRISE__TOKEN` (double
   underscores between `seed`, `enterprise`, and `token` because the field
   name is `seed_enterprise_token`, three segments separated by underscores
   that are literal — not nesting). The token is multi-line so use a YAML
   block scalar (`|`).

   On boot OpenProject's EE seeder writes the token to the DB. After that
   it's loaded by the EE check that the wrap image used to bypass.

2. **Via admin UI** (one-off, stored in DB):

   Log in as admin → `/admin/enterprise` → paste token → submit. Survives
   re-deploys (DB persists) but not DB resets / migrations to a new
   environment.

After EE token is loaded, the wrap image can be replaced with the upstream
`openproject/openproject:17.5-slim` in the project YAML deployment image
field. Remove the `local/` prefix and the kind-loaded wrap image becomes
unused.

## Known limitations

- **OIDC EE-gated**: the bypass image is a temporary workaround. Replace
  with an Enterprise Token (see section above) before any non-test use.
- **Single replica only by default**: redis cache (above) makes multi-replica
  safe, but background workers and seeder-on-boot (below) still assume one
  instance. Scale the web container only after the worker/cron split is in place.
- **No background workers**: only the web container runs. Mail delivery, async
  PDF export, repository sync etc. don't run. Add `worker` and `cron`
  components from the openproject helm chart pattern when needed.
- **Seeder runs every boot**: the `command:` chains seeder before web. First
  boot creates schema (slow). Subsequent boots are a no-op for migrations
  but seeder always re-runs (idempotent, but adds ~10-20s to startup).
- **Shared PG cluster noise**: multi-tenant DB has been OOMKilled under
  combined load — see `docs/KNOWN-ISSUES.md` or follow-up issue for
  per-project DB option.

## Restoring from scratch

1. Push and merge `claude/sandbox-uid-override`
2. Wait for operations-manager hot-reload (skaffold)
3. Build the EE-bypass wrap image and `kind load`
4. Drop the project YAML above into `zad-projects/projects/openp-<slug>.yaml`
   (replace `<slug>` and `SECRET_KEY_BASE`)
5. Trigger project process via OPI UI: `Project herverwerken`
6. Once pod is running: bump probe budget, promote admin, configure roles

End-to-end from a clean cluster: ~20 minutes assuming the branch is already
deployed.
