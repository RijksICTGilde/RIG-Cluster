# Uniform, Declarative Platform Services

Status: Implemented (2026-07-24). This was the design brief and phased migration
roadmap; the delivered architecture is documented in
`features/service-provider-registry.md`. Kept here for the rationale and the
phase-by-phase history.

## Why

Adding or changing one provisioned + configurable ZAD service today touches
roughly 14 edit sites across ~8 files. The service *catalog* is already a clean
data-driven registry, and several code paths already read it generically, but six
other concerns are still hand-maintained per service. The result: every new
service is a scavenger hunt through wizard sections, flow registries, a fixed
provisioning sequence, a removal map, and manifest-context flags — easy to get
half-right (the `namespace-postgresql-database` config, for example, is not
modeled in the JSON schema at all).

The goal: **define each service once, as a self-contained declarative unit, and
let generic code drive all CRUD + wizard + provisioning + cleanup + manifest
generation off the registry.**

The direction is already signalled in code: `ServiceDefinition.storage_config`
carries `# TODO: specific definitions should not be here`
(`opi/services/services.py:59`).

## What is already uniform vs hand-maintained

**Already data-driven (leave as the foundation):**
- `ServiceType` enum — `opi/services/services_enums.py` (13 members).
- `ServiceAdapter.SERVICE_DEFINITIONS: dict[ServiceType, ServiceDefinition]` —
  `opi/services/services.py:373`. Definition already carries metadata + declarative
  `VariableDefinition` enums + `requires` (path-syntax deps, resolved by
  `resolve_service_dependencies` at `services.py:511`) + `cleanup_strategy` +
  `backup_label`.
- Service selection — `ServiceOptionsProvider.get_options` iterates
  `for service_type in ServiceType`.
- Add-submit — `ProjectManager.add_service` (`project_manager.py:7128`) →
  `ServiceAdapter.add_services_to_project`.
- Update-config-submit — generic `_modal_do_submit`
  (`opi/web/router_detail_edit.py:1156`), driven by a declarative
  `post_save_action`.
- Removal orchestration — diff-driven `cleanup_removed_services_from_yaml_change`
  (`opi/manager/delete_project_manager.py:2263`).

**Hand-maintained per service (the pain):**

| # | Concern | Where |
|---|---|---|
| 1 | Untyped config dicts; validation split across 3 layers | `opi/schemas/project_v2.json` (partial `$defs`), editables, raw `dict.get()` in `database_manager.py:1293-1346` |
| 2 | Per-service wizard `FormSection` + `visible` lambda | `wizard_sections.py` (keycloak:220, postgres:269, auth-wall:344, attachments:838) |
| 3 | Per-service editable field lists w/ hardcoded yaml paths | `opi/forms/editables/fields/services.py` |
| 4 | Four hand-synced flow registries + section lists | `wizard_sections.py:359,457`; `flows.py:70,91,141,225` |
| 5 | Per-service `MODAL_EDIT_*_FLOW` + `FLOW_REGISTRY` | `flows.py:158-222` |
| 6 | Fixed 4-manager provisioning sequence | `project_manager.py:4485-4488` |
| 7 | Removal manager map `_SERVICE_TYPE_MANAGER_ATTR` | `delete_project_manager.py:2239` |
| 8 | Label map + `component_uses_*` manifest flags + context assembly | `project_manager.py:836`, `:5128-5138`, `~4877-5364` |

## Target abstraction: one `ServiceProvider` per service

Introduce a `ServiceProvider` base class, one subclass per `ServiceType`,
registered in a single `SERVICE_PROVIDERS` registry. The provider **carries** its
existing `ServiceDefinition` (metadata unchanged) and adds behavior + config-shape
hooks with no-op defaults. Generic code iterates the registry instead of the
hand-maintained lists.

```python
# opi/services/provider.py  (new)
class ServiceProvider(ABC):
    service_type: ClassVar[ServiceType]
    definition: ClassVar[ServiceDefinition]          # the existing dataclass, unchanged

    # --- config shape ------------------------------------------------------
    config_model: ClassVar[type[BaseModel] | None] = None   # Pydantic, single source of truth
    def config_editables(self) -> list[Editable]: return []  # derived from config_model where simple
    def config_section(self) -> FormSection | None: return None  # wizard / edit / modal section

    # --- provisioning + cleanup -------------------------------------------
    provision_order: ClassVar[int] = 100             # lower runs first; keycloak < auth-wall
    async def provision(self, ctx: ProvisionContext) -> None: ...          # default no-op
    async def handle_service_removal(self, ctx: RemovalContext) -> dict: ...  # default no-op

    # --- manifest contribution --------------------------------------------
    def contribute_manifest_context(self, ctx: ManifestContext) -> dict[str, Any]: return {}
```

```python
# opi/services/registry.py  (new)
SERVICE_PROVIDERS: dict[ServiceType, ServiceProvider] = {...}
def get_provider(t: ServiceType) -> ServiceProvider: ...
```

### Why a provider class, not just more `ServiceDefinition` fields

- **Behavior doesn't fit a dataclass.** Provisioning and cleanup are async methods
  with real logic that already lives on managers. A base class gives a typed
  contract with a place for shared helpers; a dataclass field holding a callable
  is a class-per-service in disguise, without the contract.
- **Heterogeneity is the norm, so defaults matter (KISS/YAGNI).** Some services
  have no manager (publish-on-web, attachments, metrics-scraper, storage), some
  have no config, some have no variables. No-op defaults let a trivial service be
  a 5-line subclass while keycloak overrides four hooks. Cramming these into
  `Optional` dataclass fields that are `None` for most services is exactly the
  sparse-config smell behind the `storage_config` TODO.
- **Open-closed.** New service = new subclass + one registry line. No generic
  code, schema, or flow-list edits.
- **It composes with, not replaces, today's design.** `SERVICE_DEFINITIONS` stays;
  each provider references its definition, so the paths that already read the
  definitions keep working unchanged during migration.

**Providers are thin adapters.** A provider does not re-implement provisioning; it
delegates to the existing manager (`self._db_manager.create_resources_for_deployment(...)`),
which keeps its own self-guard (`_deployment_uses_postgresql`) so behavior stays
byte-identical. Manager internals are **not** refactored as part of this work.

## How each site collapses

| # | Today | After |
|---|---|---|
| 1 | Untyped config; validation in 3 layers | `provider.config_model` (Pydantic). JSON-schema `$defs` and editables derive from it. Managers use `config_model.model_validate(raw)` instead of `dict.get()`. |
| 2 | Per-service `FormSection` + `visible` (`wizard_sections.py`) | `provider.config_section()`; generic assembly wires `visible`/`post_save_action` from the service name. |
| 3 | Per-service editables (`fields/services.py`) | `provider.config_editables()`; paths derived from `services/<name>/config/...` + config-model fields. |
| 4 | `SERVICE_CONFIG_SECTIONS`, `EDIT_SECTIONS`, `SERVICE_CONFIG_MODAL_FLOWS` + flow section lists | `for t in ServiceType: get_provider(t).config_section()` — all four become derived. |
| 5 | Per-service `MODAL_EDIT_*_FLOW` + `FLOW_REGISTRY` | Generic `build_service_config_modal_flow(name)` factory; registry gains service flows by iteration. |
| 6 | Fixed 4 calls (`project_manager.py:4485`) | `for p in ordered_providers(...): await p.provision(ctx)`. |
| 7 | `_SERVICE_TYPE_MANAGER_ATTR` (`delete_project_manager.py:2239`) | `get_provider(t).handle_service_removal(ctx)`; the map disappears. |
| 8 | `component_uses_*` flags + inline context + label map | `for p in providers_used_by(component): ctx |= p.contribute_manifest_context(...)`; label from `definition.backup_label`. |

## Typed config (single source of truth)

Introduce one Pydantic model per configurable service — `KeycloakConfig`,
`NamespacePostgresConfig {image, registry, instances, storage, privileges}`,
`AuthWallConfig`, `AttachmentsConfig` — placed under `opi/services/config_models/`
or co-located with each provider.

Two things derive **from** the model rather than duplicating it:

- **JSON schema.** `project_v2.json` is loaded and cached in
  `opi/core/project_schema.py:44`. Replace hand-written service-config `$defs`
  with fragments generated from `Model.model_json_schema()`, wired by service name.
  Keep the single `validate_project_schema` chokepoint intact and keep loading a
  committed static file (fails closed). A CI test asserts the committed
  `project_v2.json` equals the generated one, so a model can't silently drift from
  the schema.
- **Editables.** For simple scalar fields, `config_editables()` emits
  `Editable(yaml_path=f"services/{name}/config/{field}")` from the model, attaching
  validators. Complex nested editables (keycloak `additional-clients` /
  `realm-roles` sequences) stay hand-authored in the provider — don't auto-generate
  the hard 10%. This is the Pydantic-sourced version of the pipeline in
  `features/futures/editables-as-shared-validation-layer.md`.

Managers stop doing raw `dict.get()` (`database_manager.py:1293-1346`) and instead
`config_model.model_validate(config_dict)` — collapsing the third validation layer
into the first.

## Generic provisioning + cleanup + manifest

- **Provisioning** replaces `project_manager.py:4485-4488` with an ordered loop
  over providers (`provision_order`: keycloak=10, auth-wall=20; stable sort keeps
  today's db→minio→keycloak→redis order for equal ranks). No-op default means
  services without a manager need no guard. Managers stay instantiated in
  `ProjectManager.__init__` (`project_manager.py:407`); providers reach them lazily
  via the context, preserving `_ensure_database_manager` semantics.
- **Cleanup** swaps the `_SERVICE_TYPE_MANAGER_ATTR` lookup for
  `get_provider(t).handle_service_removal(...)`. The diff-driven
  `was_used and not still_used` gate and the deferred/immediate `cleanup_strategy`
  are unchanged; both postgres providers delegate to the same `database_manager`
  (idempotent), preserving the "check once per manager" behavior.
- **Manifest contribution** moves the `component_uses_*` booleans and per-service
  context dicts into `contribute_manifest_context`, which *emits* the same context
  keys (`uses_postgresql=True`, `env_from_secrets=[...]`, storage/attachment
  mounts) so the Jinja templates in `manifests/` render byte-identical output.

## Phased migration (non-big-bang)

Introduce the abstraction as an additive shadow first, then cut generic paths over
one at a time. Every phase keeps all services working and is a shippable PR.

- **Phase 0 — Guardrails first (no behavior change).** Golden-manifest byte-diff
  harness over representative project YAMLs; provider-coverage CI check (warn).
  Baseline established against the `tests/e2e` sandbox lifecycle.
- **Phase 1 — `ServiceProvider` + registry, metadata-only.** One thin subclass per
  `ServiceType` holding its existing definition; nothing consumes providers yet.
  Flip the coverage check to hard-fail. Verify: `get_provider(t).definition is
  SERVICE_DEFINITIONS[t]` for all types; suite green.
- **Phase 2 — Typed config + schema generation (highest leverage).** Pydantic
  models for the configurable services, starting with **namespace-postgres** (no
  schema today, highest bug surface). Generate its `$defs`; add the schema-equality
  test; point `database_manager` config reads at `model_validate`.
- **Phase 3 — Generic wizard/flow assembly.** Move sections/editables onto
  providers; derive the four dicts + flow section lists by iteration; keep section
  objects behaviorally identical. Verify with `tests/forms/` + a `FLOW_REGISTRY`
  snapshot + wizard e2e per service.
- **Phase 4 — Generic provisioning dispatch.** Ordered provider loop; providers
  delegate to managers. Verify: sandbox create-project e2e exercising
  db+minio+keycloak+redis+auth-wall; assert keycloak-before-authwall.
- **Phase 5 — Generic cleanup dispatch.** Retire `_SERVICE_TYPE_MANAGER_ATTR`.
  Verify: service-removal e2e per cleanable service.
- **Phase 6 — Manifest contribution (last, riskiest).** Move `component_uses_*` +
  context assembly into `contribute_manifest_context`. Verify: golden-manifest
  byte-diff empty across all golden projects; sandbox render.

## Guardrails

- **Golden-manifest byte-diff** (primary): representative YAMLs (one per service +
  combinations: keycloak+auth-wall, shared-vs-namespace postgres,
  storage+attachments) rendered through `generation/manifests.py`; byte-stable
  before/after each phase.
- **Sandbox lifecycle e2e** — `tests/e2e` create → provision → edit-config →
  remove-service → delete against the `rig-system` sandbox for phases 4-6.
- **Schema-equality CI test** — generated `project_v2.json` must equal the
  committed file (phase 2+).
- **Provider-coverage CI guard (the key one)** — a test iterating `ServiceType`
  asserting `get_provider(t)` exists. Adding a `ServiceType` without a provider
  fails CI; this is what keeps the registry the single source of truth.
- **Form/section snapshot tests** — `tests/forms/` section counts + visibility per
  flow.

## Deliberately left alone (YAGNI)

- **`PLATFORM`** — hidden, always-on, no CRUD/config/removal. Provider overrides
  nothing.
- **publish-on-web / metrics-scraper inline `show_when` toggles** in
  `COMPONENTS_SECTION` — boolean component flags, not config sections.
- **Storage PVC generation** and **backup/restore flows** (`MODAL_BACKUP_FLOW`,
  `build_restore_flow`) — cross-service, not per-service; "one provider per
  service" buys no uniformity there.
- **Complex nested keycloak editables** (`additional-clients`, `realm-roles`) —
  hand-authored in the provider.
- **Manager internals** — providers are thin adapters; refactoring manager
  internals during this migration multiplies risk for no uniformity gain.

## Risks

- **Manifest byte-drift (Phase 6)** — highest regression risk; mitigated by the
  Phase 0 golden-diff harness.
- **Schema-generation parity** — `oneOf`/`$ref` by service name and
  `additionalProperties` must match the currently-modeled keycloak-admin /
  publish-on-web / attachments `$defs`; mitigated by the schema-equality test and
  by doing the unmodeled namespace-postgres first (no baseline to preserve).
- **Provision-ordering regressions** — `provision_order` must reproduce today's
  implicit line-order for equal ranks; covered by an e2e ordering assertion.
- **Circular imports** — providers reference managers and forms reference
  providers; keep `provider.py` dependency-light (metadata + protocol) and access
  managers lazily via context, mirroring the existing `_ensure_*` patterns.

## Related

- `features/components-services-deployments.md` — the conceptual model (Project /
  Service / Component / Deployment).
- `features/unified-service-references.md` — the completed schema-v1→v2 refactor
  that unified how services are *referenced*; this brief unifies how they are
  *defined and implemented*.
- `features/futures/editables-as-shared-validation-layer.md` — the editables
  pipeline this design sources from the Pydantic config models.
- `features/futures/invites-service.md`, `features/futures/umami-analytics-service.md`
  — worked "add a new service" examples that become trivial under this model.
