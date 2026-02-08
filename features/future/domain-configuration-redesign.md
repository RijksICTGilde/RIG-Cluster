# Domain Configuration Redesign

**Status**: Planning / Discussion
**Priority**: Medium
**Created**: 2026-02-04

## Overview

This document outlines the current domain configuration options and proposes improvements to the UI/flow for domain configuration in the self-service portal. The goal is to provide a clearer, more flexible domain configuration experience.

## Current Domain Modes

### 1. Per-Component URLs (Default / Component-Specific)

Each component gets its own unique URL using dash-separated naming:

```
component1-deployment-projectname.domain.ext
component2-deployment-projectname.domain.ext
```

**Use case**: Components that are unrelated or standalone, for example:
- `frontend-productie-amt.rig.rijksapps.nl`
- `documentatie-productie-amt.rig.rijksapps.nl`

**Configuration**:
```yaml
deployments:
  - name: productie
    cluster: odcn-production
    components:
      - reference: frontend
      - reference: documentatie
    # No subdomain, no base-domain, no domain-mode
```

### 2. Per-Deployment URL (Path-Based)

All components share a single deployment URL, differentiated by paths:

```
deployment-projectname.domain.ext/           -> component1
deployment-projectname.domain.ext/docs       -> component2
```

**Use case**: Components that belong together under one domain, for example:
- `productie-amt.rig.rijksapps.nl/` -> frontend
- `productie-amt.rig.rijksapps.nl/documentatie` -> docs component

**Note**: When using this mode with multiple components, paths MUST be configured.

**Configuration**:
```yaml
deployments:
  - name: productie
    subdomain: productie         # subdomain = deployment name
    components:
      - reference: frontend
        paths:
          - /
      - reference: documentatie
        paths:
          - /documentatie
```

### 3. Custom Subdomain (External Domain)

User-defined subdomain on a supported base domain:

```
customname.domain.ext
```

**Use case**: Projects wanting a clean, branded URL, for example:
- `wies.rijksapp.nl`

**Configuration**:
```yaml
deployments:
  - name: staging2
    subdomain: wies
    base-domain: rijksapp.nl
    issuer: letsencrypt
    components:
      - reference: frontend
```

### 4. Nice URLs (Dotted Format)

When `domain-mode: nice-url` is enabled, URLs use dot-separation instead of dashes:

```
component.subdomain.base-domain
```

**Examples**:
- `frontend.myapp.rijksapp.nl`
- `backend.myapp.rijksapp.nl`
- `myapp.rijksapp.nl` (root URL, requires `root: true` on component)

**Configuration**:
```yaml
deployments:
  - name: productie
    domain-mode: nice-url
    subdomain: myapp
    base-domain: rijksapp.nl
    issuer: letsencrypt
    components:
      - reference: frontend
        root: true              # Responds to myapp.rijksapp.nl
      - reference: backend      # Responds to backend.myapp.rijksapp.nl
```

## Supported Base Domains

Different base domains are available depending on cluster:

| Cluster | Supported Domains |
|---------|-------------------|
| local | `kind`, `local` |
| odcn-production | `rijks.app`, `rijksapps.nl`, `rijksapp.nl` |

## Open Questions / Discussion Points

### 1. Subdomain Extensibility

Should subdomains support multiple levels? For example:

```
deployment.custom-subdomain.rijksapp.nl
```

Currently, the project name is often a generated unique identifier (like `amt-2m9`), which isn't user-friendly for URLs.

**Proposal**: Allow overriding the project name portion in nice-url mode:
```yaml
deployments:
  - name: productie
    domain-mode: nice-url
    subdomain: bzk              # Custom subdomain instead of project name
    base-domain: rijksapp.nl
```

Result: `frontend.bzk.rijksapp.nl` instead of `frontend.amt-2m9.rijksapp.nl`

### 2. Root Domain + Sub-Subdomains

Some projects may want both:
- `amt.rijksapp.nl` (root deployment)
- `bzk.amt.rijksapp.nl` (sub-deployment for specific ministry)

This requires:
1. One deployment on the root subdomain
2. Another deployment on a nested subdomain

**Current limitation**: The nice-url subdomain validation doesn't allow dots, preventing `bzk.amt` as a subdomain.

**Possible solutions**:
- A. Allow dots in subdomains for non-nice-url mode (already works)
- B. Introduce a "subdomain prefix" concept for nice-url mode
- C. Allow hierarchical subdomain registration

### 3. Cross-Project Subdomain Sharing

Should a subdomain be usable across multiple projects?

**Use case**: A ministry (BZK) wants multiple projects to share the `bzk` subdomain:
- `amt.bzk.rijksapp.nl` (project: amt)
- `woo.bzk.rijksapp.nl` (project: woo)
- `bzk.rijksapp.nl` (project: bzk-portal)

**Current behavior**: Subdomains are unique per (subdomain, base_domain) pair. Each project registers its own subdomain.

**Considerations**:
- Security: Who controls the parent subdomain?
- Governance: How to manage delegation?
- Technical: DNS wildcard certificates vs per-subdomain certs

### 4. UI/Flow Improvements

The self-service portal UI should guide users through these options clearly:

**Proposed flow**:

1. **Choose base domain**
   - Cluster default (rig.rijksapps.nl)
   - rijksapps.nl
   - rijksapp.nl
   - rijks.app

2. **Choose URL structure**
   - Per component (separate URLs)
   - Per deployment (path-based)
   - Custom subdomain

3. **If custom subdomain selected**:
   - Enter subdomain name
   - Enable "Nice URLs" checkbox (if supported by domain)

4. **If Nice URLs enabled**:
   - Select root component (for subdomain.domain.ext)
   - Preview generated URLs for all components

## Summary Table

| Mode | Pattern | Example | Registration |
|------|---------|---------|--------------|
| Component-specific | `comp-depl-proj.cluster` | `frontend-prod-amt.rig.rijksapps.nl` | None |
| Deployment-based | `depl-proj.cluster/*` | `prod-amt.rig.rijksapps.nl/api` | None |
| Custom subdomain | `sub.domain` | `wies.rijksapp.nl` | None |
| Nice URL | `comp.sub.domain` | `frontend.wies.rijksapp.nl` | Subdomain registry |

## Implementation Considerations

### Subdomain Validation

- **Nice-url mode**: Strict DNS validation (a-z, 0-9, hyphens only)
- **Non-nice-url mode**: More flexible (dots allowed in subdomain field)

### Wildcard Certificates

For nice-url mode with many components, consider wildcard certificates:
- `*.myapp.rijksapp.nl` covers all component subdomains
- Reduces cert-manager load
- Requires DNS-01 challenge (not HTTP-01)

### Migration Path

Projects using old domain configurations should continue to work. New features should be opt-in via explicit configuration.

## Related Features

- [external-domains-letsencrypt.md](../external-domains-letsencrypt.md) - Let's Encrypt integration for custom domains
- [multi-path-ingress.md](../multi-path-ingress.md) - Multiple paths per component
