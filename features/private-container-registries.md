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

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | Unique identifier (multiple registries can share same URL) |
| `url` | Yes | Registry URL without protocol (e.g., `docker.io`, `ghcr.io`) |
| `username` | Yes | Username or token name |
| `password` | Yes | AGE-encrypted password or token |

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

## Security Best Practices

1. Always encrypt passwords with AGE (never commit plain text)
2. Use service accounts with minimal permissions (read-only)
3. Rotate credentials regularly
4. Use descriptive registry names (e.g., `harbor-prod`, `github-org-packages`)
