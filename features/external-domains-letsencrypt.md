# External Domains with Let's Encrypt

> **Waar dit wordt opgeslagen (schemaversie 2.7):** de velden hieronder staan in het
> projectbestand onder `deployments[].services[publish-on-web].config`, niet meer los in de
> wortel van de deployment. Zie [webadres-onder-de-dienst.md](webadres-onder-de-dienst.md).
> De YAML-fragmenten in dit document tonen de velden zonder dat omhulsel, om over hun
> betekenis te gaan en niet over hun plek.

## What it is

Enables projects to use custom external domains (e.g., `myapp.rijksapp.com`) with automatic TLS certificate provisioning via Let's Encrypt. Each project can specify its own base domain and subdomain, with certificates managed through namespace-scoped cert-manager Issuers.

## How it works

1. A deployment specifies `base-domain` (e.g., `rijksapp.com`) and `subdomain` (e.g., `myapp`)
2. The `issuer` field determines how TLS certificates are obtained:
   - `letsencrypt` - Uses Let's Encrypt production ACME server
   - `letsencrypt-staging` - Uses Let's Encrypt staging ACME server (for testing)
   - Custom value - References an existing namespace-scoped Issuer
3. A namespace-scoped `Issuer` is automatically generated for Let's Encrypt configurations
4. Ingress resources are annotated to use the namespace Issuer instead of a ClusterIssuer
5. cert-manager handles the ACME HTTP-01 challenge and provisions certificates

## Configuration

### Project YAML Schema

```yaml
name: my-project
display-name: My Application
clusters:
- odcn-production
config:
  contact-email: team@example.com  # Optional: overrides cluster default for Let's Encrypt
deployments:
- name: production
  cluster: odcn-production
  namespace: my-project
  base-domain: rijksapp.com      # The apex/root domain
  subdomain: myapp               # Creates myapp.rijksapp.com
  issuer: letsencrypt            # letsencrypt | letsencrypt-staging | <custom-issuer-name>
  components:
  - reference: webapp
    image: ghcr.io/org/app:v1
```

### Issuer Field Options

| Value | Behavior |
|-------|----------|
| `letsencrypt` | Auto-generates namespace Issuer with Let's Encrypt production ACME |
| `letsencrypt-staging` | Auto-generates namespace Issuer with Let's Encrypt staging ACME |
| `<custom-name>` | References an existing Issuer by name (no auto-generation) |
| Not specified | Falls back to cluster's `cluster_issuer` configuration |

### Hostname Construction

| Configuration | Resulting Hostname |
|---------------|-------------------|
| `base-domain: rijksapp.com` + `subdomain: myapp` | `myapp.rijksapp.com` |
| `subdomain: myapp` (no base-domain) | Uses cluster `ingress_postfix` (e.g., `myapp-project.rig.example.com`) |
| Neither specified | Component-specific URLs using cluster domain |

### Contact Email

The Let's Encrypt contact email is determined in this order:
1. Project-level `contact-email` in the config section
2. Cluster-level `letsencrypt.contact_email` in cluster configuration

```python
# In cluster_config.py
"odcn-production": {
    "letsencrypt": {
        "contact_email": "rig-platform@rijksoverheid.nl",
    },
}
```

## Generated Resources

When using `letsencrypt` or `letsencrypt-staging`, the system generates:

### Namespace-scoped Issuer

```yaml
apiVersion: cert-manager.io/v1
kind: Issuer
metadata:
  name: letsencrypt-rijksapp-com
  namespace: my-project
spec:
  acme:
    email: team@example.com
    server: https://acme-v02.api.letsencrypt.org/directory
    privateKeySecretRef:
      name: letsencrypt-rijksapp-com-key
    solvers:
    - http01:
        ingress:
          ingressClassName: nginx
          serviceType: ClusterIP
```

### Ingress with Issuer Annotation

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: production-webapp
  annotations:
    cert-manager.io/issuer: letsencrypt-rijksapp-com
spec:
  rules:
  - host: myapp.rijksapp.com
    http:
      paths:
      - path: /
        pathType: Prefix
        backend:
          service:
            name: production-webapp
            port:
              number: 80
  tls:
  - hosts:
    - myapp.rijksapp.com
    secretName: production-webapp-tls
```

## Naming Conventions

Resources are named consistently using the normalized base domain:

| Resource | Naming Pattern | Example |
|----------|---------------|---------|
| Issuer name | `{issuer-type}-{normalized-domain}` | `letsencrypt-rijksapp-com` |
| Issuer secret | `{issuer-name}-key` | `letsencrypt-rijksapp-com-key` |
| Manifest file | `issuer-{issuer-name}.yaml` | `issuer-letsencrypt-rijksapp-com.yaml` |

Domain normalization:
- Dots (`.`) replaced with hyphens (`-`)
- Lowercased
- Truncated to 50 characters if needed

## Prerequisites

- **cert-manager** must be installed in the cluster
- **Public ingress** - HTTP-01 challenges require the domain to be publicly accessible
- **DNS configured** - The domain must point to the cluster's ingress

## Comparison: ClusterIssuer vs Namespace Issuer

| Aspect | ClusterIssuer | Namespace Issuer |
|--------|---------------|------------------|
| Scope | Cluster-wide | Single namespace |
| RBAC | Requires cluster admin | Project team can manage |
| Use case | Internal domains | External/custom domains |
| Annotation | `cert-manager.io/cluster-issuer` | `cert-manager.io/issuer` |

This feature uses namespace-scoped Issuers because:
- Projects may not have permissions to create ClusterIssuers
- Each project can have its own Let's Encrypt account
- Isolation between projects

## Troubleshooting

### Certificate not issued

1. Check Issuer status:
   ```bash
   kubectl describe issuer letsencrypt-rijksapp-com -n my-project
   ```

2. Check Certificate status:
   ```bash
   kubectl get certificates -n my-project
   kubectl describe certificate production-webapp-tls -n my-project
   ```

3. Check ACME challenges:
   ```bash
   kubectl get challenges -n my-project
   ```

### HTTP-01 challenge failing

- Verify DNS points to cluster ingress
- Check ingress controller logs
- Ensure no firewall blocking port 80
- Verify the `/.well-known/acme-challenge/` path is accessible

### Rate limits (Let's Encrypt production)

- Use `letsencrypt-staging` for testing
- Let's Encrypt has rate limits: 50 certificates per domain per week
- Staging has much higher limits for testing

## Files

| File | Purpose |
|------|---------|
| `opi/utils/naming.py` | Domain normalization and issuer naming functions |
| `opi/core/cluster_config.py` | Cluster-level Let's Encrypt contact email |
| `manifests/issuer-letsencrypt.yaml.jinja` | Issuer manifest template |
| `manifests/ingress.yaml.jinja` | Ingress template with issuer annotation support |

## Related Features

- [Local HTTPS Certificates](local-https-certificates.md) - TLS for local development with custom CA
