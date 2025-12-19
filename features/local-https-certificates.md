# Local HTTPS Certificates

## What it is

Enables trusted HTTPS for local Kind cluster development. Both browsers and pods within the cluster trust the same locally-generated CA certificate, eliminating SSL verification errors for `*.kind` domains.

## How it works

1. A root CA is generated locally and stored in `security/tls/`
2. The CA is installed in the OS trust store (browsers trust it)
3. The CA is mounted into the Kind node via `extraMounts` (all pods inherit trust automatically)
4. cert-manager uses the CA to issue certificates for ingresses via a ClusterIssuer

## Setup (one-time per developer machine)

### Quick start

The `setup-local-cluster` task handles everything automatically:

```bash
task setup-local-cluster
```

This runs the following in order:
1. Generates AGE key for SOPS encryption
2. Generates local CA certificate
3. Creates Kind cluster (with CA mounted)
4. Installs ingress-nginx, CNPG operator
5. Configures CoreDNS for `.kind` domains
6. Deploys ArgoCD operator and operations manager
7. Imports CA to cert-manager

After setup completes, run these manual steps:

```bash
# Install CA in your OS trust store (requires sudo, one-time)
task install-local-ca

# Bootstrap ArgoCD
task bootstrap-argo-system
```

### Individual steps (for reference)

If you need to run steps individually:

| Step | Command | Description |
|------|---------|-------------|
| 1 | `task generate-local-ca` | Generate CA cert and key |
| 2 | `task install-local-ca` | Install CA in OS trust store |
| 3 | `task create-local-kind-cluster` | Create cluster with CA mounted |
| 4 | `task import-ca-to-cluster` | Import CA for cert-manager |
| 5 | `task bootstrap-argo-system` | Deploy ClusterIssuer via ArgoCD |

## Configuration

### Cluster config

The cluster configuration in `opi/core/cluster_config.py` defines the TLS settings:

```python
"ingress": {
    "enable_tls": True,
    "cluster_issuer": "kind-ca-issuer",  # Local development
    "ip_whitelist": "0.0.0.0",
}
```

For production clusters, use a different issuer (e.g., Let's Encrypt):

```python
"ingress": {
    "enable_tls": True,
    "cluster_issuer": "letsencrypt-production",
    "ip_whitelist": "0.0.0.0/0",
}
```

### Automatic TLS for generated ingresses

When `enable_tls` is `true` and `cluster_issuer` is configured in the cluster config, the operations manager automatically:

1. Adds the `cert-manager.io/cluster-issuer` annotation to ingresses
2. Configures the TLS section with hostname and secret name
3. Generates unique TLS secret names using `generate_tls_secret_name()`

Generated ingress example:
```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: main-webapp
  annotations:
    cert-manager.io/cluster-issuer: kind-ca-issuer
spec:
  rules:
    - host: webapp-main-myproject.kind
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: main-webapp
                port:
                  number: 80
  tls:
    - hosts:
        - webapp-main-myproject.kind
      secretName: main-webapp-tls
```

cert-manager automatically provisions a certificate signed by the local CA.

## Files

| File | Purpose |
|------|---------|
| `security/tls/ca.crt` | CA certificate (gitignored) |
| `security/tls/ca.key` | CA private key (gitignored) |
| `kind-config.yaml` | Kind cluster config with extraMounts |
| `infrastructure/bootstrap/infrastructure/cert-manager/config/overlays/local/cluster-issuer.yaml` | ClusterIssuer definition |

## Taskfile commands

| Command | Description |
|---------|-------------|
| `task generate-local-ca` | Generate root CA (one-time) |
| `task install-local-ca` | Install CA in OS trust store |
| `task import-ca-to-cluster` | Import CA to cert-manager namespace |
| `task inject-ca-to-running-cluster` | Inject CA into existing cluster (no recreate needed) |

## How pods trust the CA

The Kind `extraMounts` configuration mounts the CA certificate directly into the node's `/etc/ssl/certs/` directory:

```yaml
extraMounts:
  - hostPath: ./security/tls/ca.crt
    containerPath: /etc/ssl/certs/kind-local-ca.crt
    readOnly: true
```

Since containerd inherits the node's trust store, all containers automatically trust certificates signed by this CA without requiring per-pod volume mounts or additional operators like trust-manager.

## Troubleshooting

### Browser still shows certificate warning

- Verify CA is installed: `task install-local-ca`
- Restart browser after installing CA
- On macOS, check Keychain Access for "Kind Local Development CA"

### Pod SSL verification fails

- Verify cluster was created after CA was generated
- Check CA is mounted: `kubectl exec <pod> -- ls -la /etc/ssl/certs/ | grep kind`
- If CA was generated after cluster creation, either:
  - Run `task inject-ca-to-running-cluster` (no cluster recreate)
  - Or recreate cluster with `task uninstall-local-kind-cluster && task create-local-kind-cluster`

### cert-manager not issuing certificates

- Verify secret exists: `kubectl get secret kind-ca-secret -n cert-manager`
- Check ClusterIssuer status: `kubectl describe clusterissuer kind-ca-issuer`
- Run `task import-ca-to-cluster` if secret is missing

### Certificate not trusted by specific application

Some applications use their own trust store (Java, Python requests, etc.). For these:
- Set `SSL_CERT_FILE` or `REQUESTS_CA_BUNDLE` environment variable
- Mount the CA and configure the application to use it

## Dependencies

- cert-manager must be installed in the cluster
- Kind cluster with `extraMounts` support
- OpenSSL for CA generation
