# Private Container Registries

## What it is

Enables using container images from private registries by automatically creating Kubernetes `imagePullSecrets`.

## How to use it

### 1. Define registries in your project YAML

```yaml
registries:
  - name: my-registry              # Unique identifier
    url: registry.example.com      # URL without https://
    username: myusername
    password: |                    # AGE-encrypted
      -----BEGIN AGE ENCRYPTED FILE-----
      ...
      -----END AGE ENCRYPTED FILE-----
```

### 2. Reference registry in deployment components

```yaml
deployments:
  - name: production
    components:
      - reference: frontend
        image: "registry.example.com/myorg/frontend:v1.2.3"
        registry: my-registry    # Links to registry by name
```

**Key point**: The `registry` field goes in `deployments[].components[]`, not in the `components[]` section. This allows different deployments to use different registries for the same component.

## Configuration

A registry uses **either** credentials (username/password) **or** a pre-existing Kubernetes secret (`secretName`), not both.

### Credential-based (Operations Manager creates the secret)

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | Unique identifier (multiple registries can share same URL) |
| `url` | Yes | Registry URL without protocol, may include path (e.g., `ghcr.io`, `rcr.rijksapps.nl/rig`) |
| `username` | Yes | Username or token name |
| `password` | Yes | AGE-encrypted password or token (supports `plain:` prefix for unencrypted) |

### Pre-existing secret (Operations Manager references an existing secret)

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | Unique identifier |
| `url` | Yes | Registry URL without protocol, may include path |
| `secretName` | Yes | Name of an existing `kubernetes.io/dockerconfigjson` secret in the namespace |

When `secretName` is used, the Operations Manager skips secret creation and uses the referenced secret directly in `imagePullSecrets`. This is useful when:
- The secret is managed externally (e.g., by the platform team)
- The secret is pre-provisioned in the namespace
- You want to share a single pull secret across deployments

## Examples

### Docker Hub
```yaml
registries:
  - name: docker-hub
    url: docker.io
    username: mycompany
    password: |
      -----BEGIN AGE ENCRYPTED FILE-----
      ...
      -----END AGE ENCRYPTED FILE-----

deployments:
  - name: production
    components:
      - reference: api
        image: "mycompany/api:latest"
        registry: docker-hub
```

### GitHub Container Registry
```yaml
registries:
  - name: github-packages
    url: ghcr.io
    username: my-github-username   # GitHub username
    password: |                    # AGE-encrypted PAT with read:packages scope
      -----BEGIN AGE ENCRYPTED FILE-----
      ...
      -----END AGE ENCRYPTED FILE-----

deployments:
  - name: production
    components:
      - reference: frontend
        image: "ghcr.io/myorg/frontend:v1.2.3"
        registry: github-packages
```

### Multiple registries with same URL
```yaml
registries:
  - name: github-org-a
    url: ghcr.io
    username: org-a-bot
    password: |
      -----BEGIN AGE ENCRYPTED FILE-----
      ...
      -----END AGE ENCRYPTED FILE-----

  - name: github-org-b
    url: ghcr.io
    username: org-b-bot
    password: |
      -----BEGIN AGE ENCRYPTED FILE-----
      ...
      -----END AGE ENCRYPTED FILE-----

deployments:
  - name: production
    components:
      - reference: frontend
        image: "ghcr.io/org-a/frontend:latest"
        registry: github-org-a
      - reference: backend
        image: "ghcr.io/org-b/backend:latest"
        registry: github-org-b
```

### Pre-existing secret (secretName)
```yaml
registries:
  - name: platform-registry
    url: rcr.rijksapps.nl/rig
    secretName: rcr-pull-secret    # Must already exist in the namespace

deployments:
  - name: production
    components:
      - reference: api
        image: "rcr.rijksapps.nl/rig/my-project/api:v1.0"
        registry: platform-registry
```

### Sandbox registry (plain text credentials)
```yaml
registries:
  - name: sandbox-registry
    url: registry.sandbox.rijksapp.dev
    username: admin
    password: "plain:admin1234"     # plain: prefix skips AGE decryption

deployments:
  - name: production
    components:
      - reference: frontend
        image: "registry.sandbox.rijksapp.dev/rig/my-project/frontend:v1"
        registry: sandbox-registry
```

### Mixed public and private images
```yaml
deployments:
  - name: production
    components:
      - reference: custom-app
        image: "registry.internal.company.net/apps/custom:v1.0.0"
        registry: internal-registry
      - reference: nginx-proxy
        image: "nginx:alpine"      # No registry field needed for public images
```

### PostgreSQL infrastructure with private registry

When using `namespace-postgresql-database` service with a private PostgreSQL image:

```yaml
registries:
  - name: internal-registry
    url: registry.internal.company.net
    username: database-service-account
    password: |
      -----BEGIN AGE ENCRYPTED FILE-----
      ...
      -----END AGE ENCRYPTED FILE-----

services:
  - namespace-postgresql-database:
      config:
        image: "registry.internal.company.net/postgres/postgres:17-custom"
        instances: 2
        storage: "20Gi"
        registry: internal-registry    # Reference to registry for pulling PostgreSQL image
```

**Note**: The `registry` field in `namespace-postgresql-database` service config works the same as deployment components - it references a registry defined in the top-level `registries:` section.

## Image Upload API

The Operations Manager provides an API to push Docker image tarballs to the configured platform registry. This is the primary way to get custom images into the sandbox or production registry.

### Push an image

```bash
# 1. Save your image as a tar
docker save my-app:v1.0 -o /tmp/my-app.tar

# 2. Upload via the API
curl -X POST "https://<ops-manager-url>/api/v1/projects/<project-name>/images/push?image_name=my-app&tag=v1.0" \
  -H "X-API-Key: <project-api-key>" \
  -F "file=@/tmp/my-app.tar"
```

The image lands at `{REGISTRY_URL}/{REGISTRY_ORG}/{project-name}/{image-name}:{tag}`.

### Important

- The project namespace still needs an `imagePullSecret` to pull from the registry
- Either configure a registry with credentials in the project YAML, or use `secretName` to reference a pre-provisioned pull secret
- Maximum upload size is configurable via `IMAGE_UPLOAD_MAX_SIZE_MB` (default: 5120 MB)

## Registry URL Paths

Registry URLs can include paths, not just domains. This is useful when a single registry hosts multiple organizations:

```yaml
registries:
  - name: my-org-registry
    url: rcr.rijksapps.nl/rig/my-project    # URL with org/project path
    secretName: rcr-pull-secret
```

The `url` field is used as the key in `.dockerconfigjson` auth entries. Docker/containerd supports path-based matching, so `rcr.rijksapps.nl/rig/my-project` in the auth dict matches images under that path.

## Troubleshooting

### ImagePullBackOff errors
- Verify credentials are correct
- Ensure password is AGE-encrypted with project's public key
- Check registry URL has no `https://` prefix
- Verify network connectivity to registry

### Secret not created
- Check component has `registry: <name>` field in deployment
- Verify registry name matches exactly
- Review operations-manager logs

### Password decryption failure
- Ensure password is AGE-encrypted with project's public key
- Verify project has valid AGE keypair configured

## Registry-Specific Notes

### GitHub (ghcr.io)
- **Username**: GitHub username
- **Password**: Personal Access Token with `read:packages` scope

### Docker Hub (docker.io)
- **Username**: Docker Hub username
- **Password**: Docker Hub password or access token

### GitLab (registry.gitlab.com)
- **Username**: GitLab username or deploy token
- **Password**: Personal access token with `read_registry` scope

## ODC-Noord Specific: Quay Proxy Requirement

**IMPORTANT**: Direct pulls from external private registries (like GitHub Container Registry) are not allowed in ODC-Noord due to network restrictions.

**Solution**: Images must be proxied through Quay:
1. Request a Quay organization from ODC-Noord infrastructure team
2. Choose: proxy specific repositories OR use credentials to pull any image
3. Infrastructure team configures Quay to authenticate with upstream registry (e.g., GitHub)
4. Use Quay URLs in your project YAML instead of direct registry URLs (e.g., `quay.io/odcn-proxy-org/myorg-frontend:v1.2.3` instead of `ghcr.io/myorg/frontend:v1.2.3`)

## Security Best Practices

1. Always encrypt passwords with AGE (never commit plain text) — use `plain:` prefix only for local/sandbox development
2. Use `secretName` when credentials are managed externally (avoids storing them in the project YAML)
3. Use service accounts with minimal permissions (read-only)
4. Rotate credentials regularly
5. Use descriptive registry names (e.g., `harbor-prod`, `github-org-packages`)
6. In ODC-Noord: Always use Quay proxy, never expose direct external registry credentials
