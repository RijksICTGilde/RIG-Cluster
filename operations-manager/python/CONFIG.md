# Configuration Management

The Operations Manager supports hierarchical configuration loading from multiple sources.

## Configuration Hierarchy

Configuration is loaded in the following order (later sources override earlier ones):

1. **Base `.env` file** - Default development configuration
2. **Local `.env.local` file** - Local development overrides (optional)
3. **ConfigMap mounted `.env` file** - Container/Kubernetes production overrides (optional)

## Environment Files

### 1. Base Configuration (`.env`)
The base `.env` file contains default configuration values for development:

```bash
ENVIRONMENT=development
DEBUG=true
GIT_PROJECTS_SERVER_URL=git://localhost:9090/
# ... other development defaults
```

### 2. Local Development Overrides (`.env.local`)
Create a `.env.local` file to override settings for your local development environment:

```bash
# Local development overrides
DEBUG=true
GIT_PROJECTS_SERVER_URL=git://host.docker.internal:9090/
SOPS_AGE_PRIVATE_KEY=AGE-SECRET-KEY-...
```

**Note**: `.env.local` should be added to `.gitignore` to avoid committing local secrets.

### 3. ConfigMap Configuration (Production)
For Kubernetes deployments, use a ConfigMap to provide production configuration:

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: operations-manager-config
  namespace: rig-system
data:
  .env: |
    ENVIRONMENT=production
    DEBUG=false
    GIT_PROJECTS_SERVER_URL=https://git.example.com/repos/
    OIDC_CLIENT_ID=operations-manager-prod
    API_TOKEN=production-secret-token
```

## ConfigMap Mount Paths

The application automatically checks for ConfigMap mounted `.env` files at these paths:

1. `/etc/config/.env` (recommended)
2. `/app/config/.env`
3. `/config/.env`
4. Custom path via `CONFIG_ENV_FILE_PATH` environment variable

## Kubernetes Deployment

### Example Deployment with ConfigMap

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: operations-manager
spec:
  template:
    spec:
      containers:
      - name: operations-manager
        image: operations-manager:latest
        env:
        # Optional: Override ConfigMap mount path
        - name: CONFIG_ENV_FILE_PATH
          value: "/etc/config/.env"
        volumeMounts:
        - name: config-volume
          mountPath: /etc/config
          readOnly: true
      volumes:
      - name: config-volume
        configMap:
          name: operations-manager-config
```

### Creating the ConfigMap

```bash
# Create ConfigMap from file
kubectl create configmap operations-manager-config \
  --from-file=.env=config/production.env \
  -n rig-system

# Or apply from YAML
kubectl apply -f examples/configmap-config.yaml
```

## Configuration Variables

### Core Settings
- `ENVIRONMENT`: Environment name (development, staging, production)
- `DEBUG`: Enable debug logging (true/false)
- `API_TOKEN`: API authentication token

### Git Projects Server Settings
- `GIT_PROJECTS_SERVER_URL`: Git projects server repository URL
- `GIT_PROJECTS_SERVER_USERNAME`: Username for Git projects server authentication
- `GIT_PROJECTS_SERVER_PASSWORD`: Password for Git projects server authentication (can be SOPS encrypted)
- `GIT_PROJECTS_SERVER_BRANCH`: Default branch to use
- `ENABLE_GIT_MONITOR`: Enable Git file monitoring (true/false)

### OIDC Settings
- `OIDC_CLIENT_ID`: OpenID Connect client ID
- `OIDC_CLIENT_SECRET`: OpenID Connect client secret
- `OIDC_DISCOVERY_URL`: OIDC discovery endpoint URL

### SOPS Settings
- `SOPS_AGE_KEY_CONTENT`: Full SOPS age key content
- `SOPS_AGE_PRIVATE_KEY`: SOPS age private key
- `SOPS_AGE_PUBLIC_KEY`: SOPS age public key

### ArgoCD Settings
- `ARGOCD_MANAGER`: ArgoCD manager namespace

## Configuration Debugging

The application logs detailed information about configuration loading:

```
=== Configuration Loading Debug ===
Loading configuration from 3 files:
  1. .env
  2. .env.local
  3. /etc/config/.env
```

This helps verify which configuration files are being loaded and in what order.

## Best Practices

1. **Keep secrets out of ConfigMaps**: Use Kubernetes Secrets for sensitive data
2. **Use `.env.local` for development**: Never commit local development overrides
3. **Version control ConfigMaps**: Store ConfigMap YAML files in Git
4. **Environment-specific configs**: Use separate ConfigMaps per environment
5. **Validate configuration**: Check logs for configuration loading confirmation

## Example Configuration Flow

```
Development:
.env (base) → .env.local (dev overrides)

Production:
.env (base) → /etc/config/.env (ConfigMap overrides)

Staging:
.env (base) → /etc/config/.env (ConfigMap overrides)
```

This ensures consistent base configuration while allowing environment-specific customization.