# Domain Restrictions

## Overview

ZAD restricts which subdomains and custom domains can be used in project deployments. This prevents unauthorized subdomain usage on platform domains and ensures custom domains go through an approval process.

## Subdomain Restrictions

All platform domains (e.g., `rijks.app`, `rijksapps.nl`) have `restricted_subdomains: true` in the cluster configuration. When a domain is restricted, projects must explicitly list which subdomains they are allowed to use.

### Configuration

In the project YAML file, add an `allowed-subdomains` section under `domains`:

```yaml
domains:
  allowed-subdomains:
    - domain: rijks.app
      subdomains:
        - name: wies
          status: approved
        - name: portaal
          status: requested
          history:
            - date: "2026-03-28T14:30:00+00:00"
              status: requested
              by: developer@rijksoverheid.nl
    - domain: rijksapp.nl
      subdomains:
        - name: mijn-service
          status: approved
```

### Subdomain fields

| Field | Required | Description |
|---|---|---|
| `name` | Yes | The subdomain name |
| `status` | Yes | Current approval status: `requested`, `approved`, or `denied` |
| `history` | No | Audit trail of status changes (same format as custom domain history) |

### Behavior

- If a deployment uses a subdomain-based domain format (e.g., `subdomain`, `component-subdomain`) on a restricted domain, the subdomain must appear in the project's `allowed-subdomains` list for that domain with `status: approved`.
- If no `allowed-subdomains` entry exists for the domain, the wizard shows a warning and offers a checkbox to request the subdomain.
- Requesting a subdomain creates an entry with `status: requested`. Only approved subdomains are valid for deployment.
- Matching is case-insensitive.
- Reserved subdomains (www, api, admin, etc.) are still rejected regardless of allow-list.

## Custom Domains

Custom domains (domains not managed by ZAD, like `mijn-app.nl`) require explicit registration and approval in the project file before they can be used in deployments.

### Configuration

```yaml
domains:
  allowed-domains:
    - domain: mijn-app.nl
      supports-dots: true
      issuer: letsencrypt
      restricted-subdomains: false
      status: approved
      history:
        - date: "2026-03-28T14:30:00+00:00"
          status: approved
          by: admin@rijksoverheid.nl
          message: "Approved for project use"
        - date: "2026-03-27T10:00:00+00:00"
          status: requested
          by: developer@rijksoverheid.nl
          message: "Need this domain for public-facing service"
```

### Fields

| Field | Required | Description |
|---|---|---|
| `domain` | Yes | The custom domain name |
| `supports-dots` | No | Whether dot-separated URL formats are allowed (default: false) |
| `issuer` | No | TLS certificate issuer (default: letsencrypt) |
| `restricted-subdomains` | No | Whether subdomains on this custom domain are restricted (default: false) |
| `status` | Yes | Current approval status: `requested`, `approved`, or `denied` |
| `history` | No | Audit trail of status changes |

### History entries

| Field | Required | Description |
|---|---|---|
| `date` | Yes | ISO 8601 timestamp |
| `status` | Yes | Status at this point: `requested`, `approved`, `denied` |
| `by` | No | Email of person who made the change |
| `message` | No | Free-text reason |

### Status flow

- **requested** - User added the domain, awaiting admin approval
- **approved** - Admin approved, domain can be used in deployments
- **denied** - Admin denied, domain cannot be used

Only domains with `status: approved` are valid for use in deployments. Using a `requested` or `denied` domain results in a validation error.

### Custom domain issuer

When a custom domain has an `issuer` field in its project config, that issuer is used for TLS certificate generation. If no issuer is specified, `letsencrypt` is used as default.

### Custom domain dot support

The `supports-dots` field controls whether dot-separated domain formats (like `component.deployment.domain`) are available. This is checked during both form rendering (format options) and validation (enforcer).

## How it works technically

1. **Cluster config** (`cluster_config.py`): Each domain entry has `restricted_subdomains: true/false`. Helper functions: `is_domain_subdomain_restricted()`, `get_restricted_subdomain_domains()`.

2. **Project YAML model** (`project_file.py`): `DomainsModel` with `AllowedSubdomainEntry` and `CustomDomainEntry` Pydantic models.

3. **Validation helpers** (`subdomain.py`): `is_subdomain_allowed_for_project()` and `is_domain_allowed_for_project()` check restrictions against project data.

4. **Enforcer** (`enforcers.py`): `DomainConfigEnforcer` checks subdomain restrictions and custom domain approval during form submission. It has access to the full project YAML data.

5. **JSON schema** (`project_v2.json`): Updated with `domains`, `allowed-subdomain-entry`, `custom-domain-entry`, and `custom-domain-history-entry` definitions.
