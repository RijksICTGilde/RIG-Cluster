# External-DNS for TransIP

> **Note:** This component is deployed on **ODCN only**. It is not used on local clusters
> since local environments don't require real DNS management.

This deployment configures external-dns to automatically manage DNS records in TransIP
based on Ingress resources in the cluster. Combined with the "nice-url" feature, this
enables projects to use clean, readable domain names like `frontend.myapp.rijks.app`.

## Using Nice-URLs in Your Project

The nice-url feature provides clean, readable hostnames using dot-separation instead
of the default dash-separated format. External-DNS automatically creates DNS records
for these hostnames.

### Enabling Nice-URLs (Per Deployment)

Add these fields to your **deployment configuration** in your project YAML:

```yaml
deployments:
  - name: production
    cluster: odcn-production
    namespace: my-project
    repository: main-repo

    # Nice-URL configuration
    domain-mode: "nice-url"      # Required: enables nice-URL hostnames
    subdomain: "myapp"           # Required: globally unique subdomain
    base-domain: "rijks.app"     # Required: must be supported by cluster
    issuer: "letsencrypt"        # Required for nice-url: TLS certificate issuer

    components:
      - reference: frontend
        image: "myimage:latest"
        publish-on-web: true
        root: true               # Optional: receives traffic at subdomain.base_domain
      - reference: backend
        image: "mybackend:latest"
        publish-on-web: true
```

### Resulting Hostnames

With the configuration above:
- **frontend** (root): `frontend.myapp.rijks.app` AND `myapp.rijks.app`
- **backend**: `backend.myapp.rijks.app`

### Configuration Reference

| Field | Required | Description |
|-------|----------|-------------|
| `domain-mode` | Yes | Set to `"nice-url"` to enable this feature |
| `subdomain` | Yes | Globally unique name (per base-domain). Must be lowercase alphanumeric with hyphens, 1-63 characters |
| `base-domain` | Yes | Domain suffix, must be from cluster's supported domains |
| `issuer` | Yes | TLS certificate issuer (e.g., `"letsencrypt"`, `"self-signed"`) |

### Supported Base Domains

Each cluster supports specific base domains:

| Cluster | Supported Domains |
|---------|-------------------|
| `odcn-production` | `rijks.app`, `rijksapps.nl` |
| `local` | `kind`, `local` |

### Root Component

Mark one component with `root: true` to also receive traffic at the bare subdomain:
- Root component: `frontend.myapp.rijks.app` **and** `myapp.rijks.app`
- Other components: `component.myapp.rijks.app` only

Only one component per deployment can be marked as root.

### Via Self-Service API

When creating a project via the API, include:

```json
{
  "project_name": "my-project",
  "cluster": "odcn-production",
  "deployment_name": "production",
  "domain_mode": "nice-url",
  "subdomain": "myapp",
  "base_domain": "rijks.app"
}
```

Check subdomain availability first:
```
GET /api/subdomains/check/{subdomain}?base_domain=rijks.app
```

---

## How External-DNS Works

1. External-dns watches Ingress resources for hostnames
2. When an Ingress with a matching domain-filter is created/updated/deleted
3. External-dns creates/updates/deletes the corresponding DNS A record in TransIP

No special annotations are needed on Ingress resources. External-DNS automatically
detects the `host` field from `spec.rules[].host` and manages DNS accordingly.

## RBAC and Capsule Proxy

External-dns needs to watch Ingress resources across namespaces. In a multi-tenant
cluster using Capsule, we use the **Capsule Proxy** instead of cluster-wide RBAC.

### Why Capsule Proxy?

- We cannot create ClusterRoles/ClusterRoleBindings (requires cluster-admin)
- Capsule Proxy allows tenant-scoped access that appears cluster-wide to the client
- External-dns thinks it has cluster access, but only sees tenant namespaces

### Configuration

The deployment uses the Capsule Proxy endpoint:
```yaml
env:
  - name: KUBERNETES_SERVICE_HOST
    value: cluster-api.apps.prd1.gn2.quattro.rijksapps.nl
```

### What the cluster admin provides

The ServiceAccount bindings are managed by Capsule. The cluster admin configures:
- Tenant permissions to watch ingresses across tenant namespaces
- Access through the Capsule Proxy endpoint

### Files not applied (managed by Capsule)

The following files exist for documentation but are NOT applied:
- `clusterrole.yaml` - Permissions managed by Capsule tenant config
- `clusterrolebinding.yaml` - Bindings managed by Capsule tenant config

## Secrets

The `transip-credentials` secret must exist in the deployment namespace with:
- `TRANSIP_ACCOUNT_NAME` - TransIP account name
- `TRANSIP_PRIVATE_KEY` - RSA private key for API authentication
