# Service Provider Registry

**Status**: Implemented
**Created**: 2026-07-24

## Summary

Every platform service (keycloak, postgres, minio, redis, auth-wall, storage, ...)
is now defined **once** as a self-contained `Service` subclass, registered in
a single `SERVICES` map. Generic code drives config validation, wizard/flow
assembly, provisioning, cleanup, and manifest generation by iterating that registry
instead of the ~14 hand-synced per-service edit sites that existed before.

This is the implementation of the "Uniform, Declarative Platform Services" design
brief (`features/futures/uniform-declarative-services.md`). Adding a new service used
to be a scavenger hunt across wizard sections, flow registries, a fixed provisioning
sequence, a removal map, and manifest-context flags; it is now one subclass plus one
registry line, and a coverage test fails CI if you forget the registry line.

## Where

- `opi/services/catalog/base.py` — the `Service` base class and the context/
  contribution dataclasses (`ProvisionContext`, `RemovalContext`, `ManifestContext`,
  `ManifestContribution`, `SecretFileSpec`).
- `opi/services/catalog/<service>/` — one **package** per service since RC-5: its
  `Service` subclass (`__init__.py`) plus the config model, editables, visualizers,
  committed schema fragment and any detail-page template, all owned in one place. A
  service is a user-facing configuration-as-code unit, **not** a connector/provider
  ("how OPI talks to a system"). See `instructions/services.md` for the full contract
  and every hook a service declares.
- `opi/services/registry.py` — assembles `SERVICES` (one entry per `ServiceType`) plus
  the derivation helpers (`get_service`, `provisioning_services`,
  `manifest_secret_services`, `manifest_services`).
- `opi/services/config_models/` — the typed Pydantic config models, one per
  configurable service.
- `opi/schemas/services/<name>.v<major.minor>.json` — the committed JSON-schema
  fragment generated from each config model (drift-locked by test).

## How a provider is defined

A concrete provider sets `service_type` and its own `definition` (RC-36), both in its
own package; `__init_subclass__` refuses a provider that declares one without the other.
`ServiceAdapter.SERVICE_DEFINITIONS` is assembled from what the providers declare, so it
cannot drift from them. Everything else is an optional hook
with a no-op default, so a trivial service is a one-liner while keycloak overrides a
handful:

```python
class PublishOnWebService(Service):
    service_type = ServiceType.PUBLISH_ON_WEB          # no config, no provisioning

class KeycloakService(Service):
    service_type = ServiceType.KEYCLOAK
    cleanup_manager_key = "keycloak"                   # cleanup dispatch (Phase 5)
    config_model = KeycloakConfig                      # typed config (Phase 2)
    config_section_id = "keycloak-config"              # wizard/edit section (Phase 3)
    modal_flow_id = "modal-edit-keycloak-config"       # modal flow (Phase 3)
    provision_order = 30                               # provisioning order (Phase 4)
    manifest_secret_class = KeycloakSecret             # envFrom secret (Phase 6a)
    manifest_order = 30

    async def provision(self, ctx: ProvisionContext) -> None:
        await ctx.keycloak_manager.create_resources_for_deployment(ctx.project_data, ctx.deployment)
```

Providers are **thin adapters**: `provision` / cleanup delegate to the existing
managers (which keep their own self-guards and stay replay-safe), so dispatching
through the registry is behaviour-preserving. Provider modules stay dependency-light
(no forms/manager imports at module scope) to respect the circular-import constraint;
managers are reached lazily through the context objects.

## The hooks

| Concern | Hook | Registry helper | Notes |
|---|---|---|---|
| Config shape + versioning | `config_model`, `config_schema_version`, `migrate_config`, `validate_config` | — | Pydantic model is both the value guardrail and the JSON-schema source. Migrate-then-validate, fail-closed. |
| Wizard / edit / modal | `config_section_id`, `modal_flow_id` | — | Forms layer derives `SERVICE_CONFIG_SECTIONS` / `EDIT_SECTIONS` / `SERVICE_CONFIG_MODAL_FLOWS` by iterating the registry. The `FormSection` object itself stays in the forms layer; the provider only holds the declarative link. |
| Provisioning | `provision(ctx)`, `provision_order` | `provisioning_services()` | Ordered loop replaces the fixed db -> minio -> keycloak -> redis sequence. No-op default = no manager needed. |
| Cleanup on removal | `handle_service_removal(ctx)`, `cleanup_manager_key` | — | Generic dispatch by manager key replaces the old `_SERVICE_TYPE_MANAGER_ATTR` map. |
| Manifest contribution | `contribute_manifest_context(ctx)`, `build_secret_files(ctx)`, `manifest_secret_class`, `manifest_order`, `manifest_activated_by` | `manifest_secret_services()`, `manifest_services()` | Emits a declarative `ManifestContribution` (additive `env_from_secrets`/`sidecars`/`secret_files`, override `template_vars`). The component loop merges it; providers never touch the manifest generator. |

## Typed, versioned config

Each configurable service owns its config as an independently versioned unit,
mirroring the Kubernetes CRD model (envelope + discriminator + forward-only
conversion):

- The **global** `project_v2.json` validates only the service-entry envelope
  (`{name|reference, schema-version?, config?}`) and stays stable as service configs
  evolve.
- The **per-service** config is validated against the provider's `config_model`. The
  committed `opi/schemas/services/<name>.v<version>.json` fragment is generated from
  that model and a drift-lock test fails CI if the two diverge
  (`python -m opi.services.config_schema` regenerates them).
- `schema-version` is a quoted `major.minor` string (avoids YAML float coercion of
  e.g. `2.10`), a sibling of `config`. `migrate_config` is forward-only (old file ->
  current version), because ZAD only ever reads a possibly-old file and writes the
  current version.

Managers validate config through `provider.validate_config(...)` instead of raw
`dict.get()`, collapsing what used to be three validation layers into the model.

## Adding a new service

1. Add the member to `ServiceType` (`opi/services/services_enums.py`).
2. Add a `Service` subclass carrying its own `ServiceDefinition` + one line in `SERVICES`.
   The coverage guard (`tests/test_service_providers.py`) fails CI until you do.
3. If it takes config: add a Pydantic model under `opi/services/config_models/`, set
   `config_model` + `config_schema_version`, and run
   `python -m opi.services.config_schema` to emit the committed schema fragment.
4. If it needs a wizard/edit UI: add the `FormSection` in the forms layer and point
   `config_section_id` / `modal_flow_id` at it.
5. If it provisions resources: override `provision` (+ `provision_order`) and delegate
   to a manager.
6. If it has server-side resources to clean up: set `cleanup_manager_key`.
7. If it contributes to manifests: set `manifest_secret_class` / override
   `contribute_manifest_context` / `build_secret_files` (+ `manifest_order`).

No generic code, flow list, or schema `$defs` edit is needed — that is the point.

## Why `ServiceType` stays a hand-maintained enum (not auto-discovered)

A fair question when reading step 1: if a service is meant to be a self-contained
drop-in, why must you still add a `ServiceType` member and a `SERVICES` line by hand?
Couldn't the registry auto-discover subclasses from the `catalog/` folder at startup?

We looked at this deliberately and chose to keep the enum. The reasoning:

- **`ServiceType` is the typed identity, referenced ~159x across 33 files** (`get_service(ServiceType.KEYCLOAK)`, `list[ServiceType]` signatures, presence helpers). Those references are what let Pyright catch a mistyped or non-existent service at check time. Pyright (a hard rule in this repo) can only check names that exist in source — an auto-discovered identity is `Any` to the type checker. **You cannot have both statically-typed `ServiceType.X` references and an auto-discovered identity; it is one or the other.**
- **The enum is internal only — it is *not* a serialized contract.** Project YAML and the API key services by their **name string** (`keycloak`, `namespace-postgresql-database`); `ServiceAdapter.parse_services_from_strings` is the single string -> enum boundary. So the enum is not in `project_v2.json`, any Pydantic model, or a DB column. Changing it is therefore **not data- or contract-breaking** — it is purely an internal-code concern. (Roughly half the enum references are `ServiceType.X.value`, i.e. code converting straight back to the string to compare against the YAML.)
- **Auto-discovery would only remove the one `SERVICES` line, not the enum member** — because a subclass still needs `service_type = ServiceType.X`, and that member has to exist for the 159 typed sites. So it does not deliver real "drop a module and you're done" plug&play; it trades an explicit, greppable registry for import-time folder-scanning magic to save a single line. Under KISS/YAGNI that is a poor trade for an event (adding a service) that happens rarely.
- **Going fully plug&play means dropping the enum for string identity** (`Service.name = "keycloak"`, `get_service("keycloak")`). That is *possible and not data-breaking*, but it deletes Pyright's coverage of service identity on all 159 sites (a typo becomes a runtime `KeyError`) and forces a mechanical rewrite that reaches into the manager internals this migration intentionally left untouched. The cost (type-safety loss + churn) outweighs the benefit (one fewer edit per new service).

**Conclusion:** the one hand-maintained enum line per service is the price of type-checked
service identity across the codebase, and it is the cheapest insurance we have. It is a
conscious trade-off, not an oversight. If a future need makes plug&play worth the
type-safety loss, the migration path is string identity — and it is safe to attempt
because nothing stored depends on the enum.

## Guardrails (tests)

- `tests/test_service_providers.py` — **coverage guard** (the key one): fails if a
  `ServiceType` has no provider, if the registry has extras, or if a provider's
  `definition` is not the exact object `SERVICE_DEFINITIONS[t]` was assembled from. Also freezes
  the provisioning order, cleanup-key map, and manifest-contribution contract.
- `tests/test_golden_manifests.py` — byte-diff of rendered manifests against committed
  goldens, per contribution + combinations. Regenerate intentional changes with
  `UPDATE_GOLDEN=1`.
- `tests/test_service_config_schema.py` — config-model validation + the schema-fragment
  drift-lock.
- `tests/test_flow_registry_snapshot.py` — snapshot of the registry-derived flow /
  section dicts.

## Deliberately left alone

- **`platform`** — hidden, always-on; provider overrides nothing.
- **attachments** — polymorphic (project-level `data` catalog + component-level
  `uses`), already guardrailed by `attachment-data-entry` / `attachment-use-entry`
  `$defs`; a `config_model` would only duplicate an existing guard.
- **Manager internals** — providers are thin adapters; manager internals were not
  refactored as part of this work.

## Related

- `features/futures/uniform-declarative-services.md` — the original design brief and
  phased migration roadmap.
- `features/futures/uniform-service-declaration.md` — the `{name|reference, config}`
  project-file format the registry reads.
- `features/unified-service-references.md` — the earlier schema-v1 -> v2 refactor that
  unified how services are *referenced*; this work unifies how they are *defined and
  implemented*.
- `features/components-services-deployments.md` — the Project / Service / Component /
  Deployment conceptual model.
