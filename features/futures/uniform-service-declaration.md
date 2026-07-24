# Uniform Service Declaration (name-defines / reference-uses)

Status: Implemented (2026-07-24). Target format for how services are declared and
referenced in a project file, as part of RC-5 ("Uniform, Declarative Platform
Services"). This note is the canonical reference for the file-format change; the
delivered provider architecture that reads it is documented in
`features/service-provider-registry.md`.

## Why

Services were originally a simple list where the **value** named the service:

```yaml
services:
  - publish-on-web
  - keycloak
```

That worked until services needed config. Config turned the service name into a
**map key**:

```yaml
services:
  - keycloak:
      config:
        template: algoritmeregister
```

A name-as-key is a problem: every service name must then be enumerated in the JSON
schema (forcing a `oneOf` / `additionalProperties: {}` escape hatch), and code
everywhere does `next(iter(entry))` to recover the name. It is the opposite of how
**components** and **deployments** work — those give a thing a `name` and refer to
it by `reference`, which is generic and needs no per-item schema.

This design brings services in line with that proven pattern.

## The three levels (what the real files show)

A service touches three places in a project file, each with a distinct job:

| Level | Block | Role | Authored by |
|---|---|---|---|
| Project | `services:` | **Definition** — declares the service + its shared config (keycloak template, namespace-postgres image) | user |
| Component | `components[].services:` | **Consumption reference** — which component uses the service + per-component config (storage mounts, metrics port) | user |
| Deployment | `deployments[].services:` | **OPI-managed state** — provisioned revision tracking (`generation`, `revisions`) for deployment-scoped services (db) | OPI |

The deployment level already uses the clean `reference:` shape and is **not changed
by this design**. Only the project (definition) and component (reference) levels
carry the legacy name-as-key form; those are what we normalize.

## The rule (one line, applied in two blocks)

- **Definitions (project `services`)**: `oneOf[ string, { name, schema-version?, config? } ]`
- **References (component `services`)**: `oneOf[ string, { reference, config? } ]`

A **bare string** is the shorthand when there is nothing to configure; it becomes a
record only when it carries config (or, for a definition, a `schema-version`). So a
list of plain services stays clean, and records appear only where config earns them.

`name` (define) vs `reference` (use) mirrors components exactly: a component is
`name:` in `components:` but `reference:` in `deployments[].components[]`. The
identical-looking bare string is disambiguated by *which block it is in*.

`schema-version` is the per-service config version (see
`features/futures/` RC-5 typed-config design): a **quoted** `major.minor` string,
a sibling of `config`. Quoting matters — a bare `2.10` is parsed by YAML as the
float `2.1`.

## Before -> after (real: `wies.yaml`)

### Project-level definitions

Before:

```yaml
services:
  - publish-on-web
  - keycloak:
      config:
        template: sso-only
        additional_redirect_uris:
          - http://localhost:8080/*
          - http://127.0.0.1:8080/*
  - persistent-storage
  - temp-storage
  - postgresql-database
```

After:

```yaml
services:
  - publish-on-web
  - name: keycloak
    schema-version: "1.0"
    config:
      template: sso-only
      additional_redirect_uris:
        - http://localhost:8080/*
        - http://127.0.0.1:8080/*
  - persistent-storage
  - temp-storage
  - postgresql-database
```

Only `keycloak` changes (it has config); the rest stay bare strings.

### Component-level references

Before:

```yaml
components:
  - name: frontend
    services:
      - publish-on-web
      - keycloak
      - persistent-storage:
          config:
            - name: data
              size: 250Mi
              mount-path: /data
      - temp-storage:
          config:
            - name: temp
              size: 250Mi
              mount-path: /tmp
      - postgresql-database
```

After:

```yaml
components:
  - name: frontend
    services:
      - publish-on-web
      - keycloak
      - reference: persistent-storage
        config:
          - name: data
            size: 250Mi
            mount-path: /data
      - reference: temp-storage
        config:
          - name: temp
            size: 250Mi
            mount-path: /tmp
      - postgresql-database
```

The name-as-key `persistent-storage:` / `temp-storage:` become `reference:` records;
bare consumption stays bare.

### Deployment-level (unchanged)

```yaml
deployments:
  - name: productie
    services:
      - reference: postgresql-database
        config:
          generation: 1
          revisions:
            - generation: 1
```

## Migration (single source of truth preserved)

The project YAML is the single source of truth, and edits arrive via UI, API, or a
hand edit committed straight to git. So:

- **Read** accepts all three forms forever — bare string, legacy name-as-key dict,
  and the new `{name|reference, config}` record — and normalizes to the new record
  in memory. A hand-edited old-style file still reconciles.
- **Write** emits the new form on the next save, so files converge over time without
  a forced rewrite.
- The global `project_v2.json` gains the `{name|reference, config}` object in the
  relevant `service-entry` `oneOf` **additively**, keeping the single fail-closed
  validation chokepoint, and stays stable as service *configs* evolve (those are
  validated per-service by the provider models, not inlined into the global schema).

## Decisions locked

- `name` for definitions, `reference` for references (consistent with
  components/deployments).
- Bare string is the shorthand whenever config (and, for a definition, version) is
  absent — chosen for readability over strict uniformity.
- Deployment-level `services` (OPI-managed revision state) is untouched.
- `schema-version` is a quoted `major.minor` string, sibling of `config`.
- No name-as-key anywhere.
