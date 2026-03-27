# Shared Resource Warning: ACME HTTP Network Policy

## What It Is

ArgoCD reports a `SharedResourceWarning` when the same Kubernetes resource is managed by multiple ArgoCD Applications. In the Operations Manager context, this occurs with the `acme-http-network-policy` NetworkPolicy (and the `issuer-letsencrypt-*` Issuer) when a project has multiple deployments that share the same namespace.

Example warning:
```
SharedResourceWarning
NetworkPolicy/acme-http-network-policy is part of applications rig-prd-operations/wies-pr-164 and wies-staging2
```

## Why It Occurs

The Operations Manager generates a Let's Encrypt Issuer and an ACME HTTP NetworkPolicy **per deployment** when the deployment has `issuer: letsencrypt` (or `letsencrypt-staging`) and a `base-domain` configured. These resources are generated with fixed names based on their purpose, not the deployment name:

- `acme-http-network-policy` (name from `generate_network_policy_name("acme-http")`)
- `issuer-letsencrypt-rijksapp-nl` (name from `generate_issuer_name(base_domain, issuer_config)`)

The namespace is derived from the project, not the deployment. So when multiple deployments in the same project share a namespace and both use Let's Encrypt, they each generate identical resources with the same name in the same namespace.

### Example: Project `wies`

```yaml
# Project file (simplified)
deployments:
- name: staging2
  base-domain: rijksapp.nl
  issuer: letsencrypt
  namespace: wies          # <-- same namespace
  cluster: odcn-production

- name: pr-164
  base-domain: rijksapp.nl
  issuer: letsencrypt
  namespace: wies          # <-- same namespace
  cluster: odcn-production
```

Both deployments generate:
- `rig-prd-wies/acme-http-network-policy` (identical NetworkPolicy)
- `rig-prd-wies/issuer-letsencrypt-rijksapp-nl` (identical Issuer)

ArgoCD sees two Applications both claiming ownership of the same resource, triggering the warning.

### Code Paths

The generation happens in three places in `project_manager.py`:
1. **Helmfile deployments** (~line 2832): Creates network policy alongside the Let's Encrypt Issuer
2. **Kustomize deployments** (~line 3220): Same pattern for kustomize-based projects
3. **Component deployments** (~line 4530): Same pattern for component-based projects

Each uses the same naming functions from `opi/utils/naming.py`:
- `generate_network_policy_name("acme-http")` always returns `acme-http-network-policy`
- `generate_issuer_name(base_domain, issuer_config)` returns the same name for the same domain/issuer combo

## Impact

- **Functional impact: None.** The resources are identical, so whichever Application syncs last produces the same result.
- **Operational risk: Low but real.** If one deployment is deleted, ArgoCD may remove the shared resources, breaking TLS certificate issuance for the remaining deployment(s) until the next sync.
- **Warning noise:** The warning clutters the ArgoCD UI and may mask more important issues.

## Possible Solutions

### Option B: Deployment-Scoped Resource Names

Make the resource names unique per deployment by including the deployment name:

```
acme-http-network-policy-staging2
acme-http-network-policy-pr-164
issuer-letsencrypt-rijksapp-nl-staging2
issuer-letsencrypt-rijksapp-nl-pr-164
```

**Pros:** Each Application owns its own resources, no sharing conflicts
**Cons:** Creates duplicate Issuers in the same namespace (functionally wasteful but harmless), duplicate NetworkPolicies (also harmless)

As network policies in the future also need to target pods owned by their own deployment, this is the preferred solution
