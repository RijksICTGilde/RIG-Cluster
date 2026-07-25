# Service-Owned Config Fields (derive editables from the config model)

Status: Design / inventory (2026-07-24). Not implemented. This note inventories the
current coupling between the wizard/editables and the platform services, the drift it
causes, and a proposed target where **each service owns the fields it needs**, derived
from a single source of truth. It is the follow-up to the create-path "vorm-fix"
(`normalize_service_entries` on wizard save) and to the RC-5 Phase 3 idea in
`features/futures/uniform-declarative-services.md`.

## The problem in one sentence

A platform service's config fields are described **twice, by hand** — once as a
Pydantic `config_model` (the schema truth) and once as a set of `Editable`s in the
forms layer (the wizard/YAML truth) — and the two drift, so the wizard can produce
project files that don't match the current schema.

We already hit two symptoms of this:

1. The wizard stamped `schema-version: 2.2` while the schema was 2.4 (a hardcoded
   literal that nobody updated). Fixed by stamping `LATEST_SCHEMA_VERSION` in
   `load_project_template()` (`opi/forms/editables/template.py`).
2. The wizard wrote **component-level** service config in the legacy name-as-key /
   inline shape (`{persistent-storage: {config: …}}`, `{metrics-scraper: {port, path}}`)
   instead of the uniform `{reference, config}` form — so the file was *labelled* 2.4
   but wasn't. Fixed on the create path by running the assembled dict through the same
   normalizer the migration uses (`normalize_service_entries`,
   `opi/services/schema_migration.py`).

Both fixes are correct and stay, but they are **band-aids on a structural gap**: the
editables encode the schema shape by hand, independently of the `config_model`. This
doc is about closing that gap.

## How it works today (inventory)

### Data flow

```
Wizard form fields (HTML, HTMX/JSON)
   │  submitted nested dict, keyed by yaml_path
   ▼
EditableVisualizer  (forms/visualizers/fields/*.py)   ── UI: widget, label, help_text
   └─ wraps ─► Editable  (forms/editables/fields/*.py) ── DATA: yaml_path, validator, converter, default
   ▼
FormSection  (forms/visualizers/wizard_sections.py)    ── groups editables + layout + `visible`
   ▼
EditableFormProcessor.process_json_submission  (forms/editables/processor.py)
   │  read submitted[yaml_path] → validate → converter.write() → smart_set_value(dict, yaml_path)
   ▼
project YAML dict  ──►  (separately) validated by Service.config_model (Pydantic)
```

The editables layer and the `config_model` layer are **two independent descriptions
of the same fields**; they only meet in the final project dict. No code wires a
provider's `config_model` to its editables.

### The two writers, and why component config drifts

`smart_set_value` (`opi/forms/editables/service_path.py`) is service-aware, but its
detector `_SERVICE_CONFIG_RE = ^services/([^/\[]+)(/(.+))?$` only matches
**project-level** paths (`services/keycloak/config/…`). Those get the uniform
`{name, config}` record via `ensure_service_in_list` (which creates `{name: X}`).

**Component-level** paths (`components[*]/services{persistent-storage}/config[*]/name`)
do not match, so they fall through to the raw `{K}` path grammar in
`opi/forms/editables/path.py` (`_filter_ensure` / `_filter_set_terminal`), which
creates the legacy name-as-key `{K: {…}}`. That is the mechanical root cause of
symptom 2 above.

`metrics-scraper` is worse: its editable paths are `…/services{metrics-scraper}/port`
and `…/path` — no `config` segment at all — so even the intended shape is inline,
while `MetricsScraperConfig` wants `{config: {port, path}}`.

### Where per-service field knowledge lives today

Two files hold the hand-authored per-service editables:

**Project-level — `opi/forms/editables/fields/services.py`** (paths `services/<svc>/config/…`):

| Service | fields (yaml_path tail) |
|---|---|
| keycloak | `template`, `additional_redirect_uris[*]`, `restrict-access/{enabled,realm-role,error-message}`, `additional-clients[*]/{name,redirect-uris[*]}`, `realm-roles[*]/{name,description}` |
| namespace-postgresql-database | `instances`, `storage` |
| authorization-wall | `banner` |

**Component-level — `opi/forms/editables/fields/components.py`** (paths `components[*]/services{<svc>}/…`):

| Service | fields |
|---|---|
| persistent-storage | `config[*]/{name,size,mount-path}` |
| temp-storage | `config[*]/{name,size,mount-path}` |
| attachments | `config[*]/{reference,provide-as,path,env-name}` |
| publish-on-web | `config/{tls,attachment}` |
| metrics-scraper | `port`, `path` (inline, no `config` wrapper) |

The wizard config **sections** are hand-authored in
`opi/forms/visualizers/wizard_sections.py` (keycloak `KEYCLOAK_CONFIG_SECTION`,
namespace-postgres `POSTGRESQL_CONFIG_SECTION`, auth-wall `AUTH_WALL_CONFIG_SECTION`),
each with a `visible` lambda that hardcodes a service-name **string literal**
(`"keycloak" in _extract_services(data)`, etc.) rather than deriving from `ServiceType`.

### What the Service exposes today

`Service` (`opi/services/catalog/base.py`) carries for config:
`config_model`, `config_schema_version`, `migrate_config`, `validate_config`, and the
**string ids** `config_section_id` / `modal_flow_id`. It has **no** `config_editables()`
or `config_section()` — the link to the forms layer is one-directional and string
based: `wizard_sections.py` imports `get_service` and derives its section maps from
`config_section_id`. So the provider knows *that* a service has a config section, but
the section's **content** (fields, defaults, validators, layout, visibility) still
lives hand-authored in the forms layer.

## Evidence of drift (why this is not hypothetical)

Same field, declared in both layers with different values:

| Field | config_model | editable | drift |
|---|---|---|---|
| keycloak `template` default | `"sso-only"` | `"sso-support"` | different default |
| metrics `port` / `path` default | `None` / `None` | `8080` / `/metrics` | different default |
| namespace-postgres `instances` | `default=1, ge=1` | `RangeValidator(1..5)`, no default | max only in editable; default only in model |
| namespace-postgres `storage` | `"10Gi"` default | options-provider, no default | default only in model |
| keycloak `additional_redirect_uris` | `list[str]` | `max_items=10` | bound only in editable |

General pattern: **defaults, bounds and validators live in the editables; type shape
and extra-key policy live in the Pydantic model.** Neither derives from the other.

Other drift-prone literals: the `visible` service-name strings in `wizard_sections.py`;
the `schema-version` literal in `project-template.yaml`; `config_schema_version` set
per provider in `registry.py` independent of the `config_model`.

## Target: the service owns its fields, derived from the model

One source of truth = the `config_model`. The provider derives its editables and its
section from it, in the uniform emit shape:

```
config_model (Pydantic)  ── the only place a field's name, default, bound, alias lives
      │  derive
      ▼
provider.config_editables()  ── Editable(yaml_path, default, validator) per field,
      │                          ALWAYS in the uniform {reference|name, config} emit shape
      ▼
provider.config_section()    ── section + `visible` derived from service_type
      ▼
generic wizard assembly      ── already iterates the registry (SERVICE_CONFIG_SECTIONS)
```

This collapses the three drifts:

- **Shape**: one converter always writes `{reference|name, config}` → wizard output is
  genuinely current; the create-path `normalize_service_entries` band-aid becomes
  redundant (though cheap to keep).
- **Defaults / bounds**: taken from the Pydantic `Field` (`default`, `ge`/`le`,
  `max_length`, alias) → no second copy.
- **`visible`**: derived from `service_type`, not a hardcoded string.

### What derives vs what stays hand-authored

Derivable from the model (the ~90%):
- Scalar fields → one `Editable` each, `yaml_path = services/<name>/config/<field>` (or
  the component equivalent), `default` and validators from the `Field`.
- Simple lists of scalars.
- `visible` from `service_type`.

Stays hand-authored, co-located with the service (the hard ~10%):
- Complex nested sequences: keycloak `additional-clients`, `realm-roles`.
- **UI-only hints** (widget kind, options-provider, label/help text). These belong on
  the `EditableVisualizer`, not the `Editable`. Proposal: declare them as a light
  annotation on the Pydantic field (`Field(json_schema_extra={"widget": "select",
  "options": "StorageSizeOptionsProvider"})`) so the UI choice sits next to the field
  without re-copying its name/default/bound.

## The hard knots (what makes this non-trivial)

1. **The `{K}` grammar produces name-as-key.** Component service config uses
   `services{X}` filter syntax whose write path creates `{X: {…}}`. To emit
   `{reference: X, config: …}` the component write must route through a uniform engine
   (extend `service_path.py` to recognize `components[N]/services{X}/…`) — touching
   get/set/delete/exists and the reads that currently rely on `{K}`.
2. **metrics-scraper inline paths** (`…/port`, no `config`) must become
   `…/config/port` to match the model — a per-service editable-path correction.
3. **Import direction.** `provider.py` is deliberately forms-free
   (providers must not import forms). `Editable` is UI-free and could move to the
   provider, but `EditableVisualizer` (widget/label) cannot — so config_editables()
   would return data-only `Editable`s and the forms layer would still supply the UI
   wrapper (or read the widget hint off the model annotation).
4. **Edit flow + central chokepoint.** The create-path fix normalizes only the wizard
   create submit. The detail-page **edit** modals write via the same editables and can
   still produce legacy shape (then repaired on process). A shared normalization / a
   single save chokepoint would cover both.

## Phasing (each phase shippable, guarded by golden + all-services e2e)

- **Phase A — done:** version-stamp + create-path `normalize_service_entries`. Wizard
  create output is genuinely 2.4.
- **Phase B:** apply the same normalization to the **edit** flow (or a single save
  chokepoint), so no writer emits legacy shape.
- **Phase C:** derive `config_editables()` for the **simple scalar** services from the
  `config_model` (metrics, auth-wall banner, namespace-postgres instances/storage,
  storage name/size/mount-path). Delete the corresponding hand-authored editables.
  Fold defaults/bounds into the model; drop the duplicated literals. Fix the metrics
  path shape here.
- **Phase D:** derive `config_section()` + `visible` from the provider; retire the
  hardcoded service-name strings in `wizard_sections.py`.
- **Phase E:** keycloak (the complex one) — keep `additional-clients`/`realm-roles`
  hand-authored on the provider, derive the scalar parts.

## Working method: per-service checklist (agreed)

We migrate services **one at a time**; per service we run this checklist and note the
extra step that service needs (some have their own screen, some plug in at component
level, some depend on another service). Conventions applied throughout:

- **Enums, not strings.** Service identity via `ServiceType`, layer via `ConfigLayer`,
  paths via `config_path(layer, service, *segments)` (`opi/services/catalog/base.py`) - no
  hardcoded `"services/authorization-wall/config/..."` literals. Enums document, grep
  and validate better.
- **Reuse fields, don't re-declare.** Field names/defaults come from the service's
  `config_model` (`config_model_field_names()`), not a second copy; shared field sets
  (e.g. storage `name/size/mount-path`) are defined once and reused.
- **Dependencies are explicit.** A service that needs another declares it in
  `ServiceDefinition.requires`; `resolve_service_dependencies` pulls it in (auth-wall
  -> keycloak + publish-on-web). Keep this typed/enum-based.

Per service, tick:
1. `config_editables(layer)` - the data fields, paths via `config_path`, reused where possible.
2. `config_form_section(layer)` **or** a component/deployment hook-point (storage lives
   in the component definition, not a standalone section).
3. `config_api_fields(layer)` - derived from the `config_model`.
4. Dependencies verified (and enforced).
5. No string literals left.

**Reference implementation: `authorization-wall`** (done). Owns its `banner` field:
enum-built path, section built by the provider (`visible` from `service_type`), api
fields from `AuthorizationWallConfig`, keycloak dependency verified. Field atoms still
physically live in forms (re-used from there); relocating them into a per-service
module is a later step. Next services: namespace-postgres, then the component-level
storage/metrics (which exercise the hook-point), then keycloak (complex nested fields).

## Guardrails

- **Golden-manifest byte-diff** (`tests/test_golden_manifests.py`) — template render
  unaffected.
- **all-services sandbox e2e** (`tests/e2e/test_sandbox_all_services.py`) — already
  asserts project + component services are uniform and provisioned; extend with a
  per-field assertion as fields move.
- **Schema-equality / config-model drift-lock** (`tests/test_service_config_schema.py`).
- **Form/section snapshot** (`tests/forms/`, `tests/test_flow_registry_snapshot.py`).
- A new **"born current"** test: a freshly assembled wizard project has
  `migrate_to_latest(force_old_version) → was_migrated == False` (no field drifts from
  the schema). This is exactly the check used to validate Phase A.

## Open questions / decisions to make

1. **Where does the UI hint live?** On the Pydantic field (`json_schema_extra`) vs a
   parallel visualizer registry. (Leaning: on the field, to keep one source.)
2. **Extend `service_path.py` for component paths, or keep the create/edit
   normalization** as the canonical-shape guarantee and leave the `{K}` grammar as-is?
   (The normalization is DRY and low-risk; the grammar change is "editables write
   uniform" literally but higher blast radius.)
3. **One save chokepoint** for normalization (covers create+edit+API) vs per-path.
4. **Scope of auto-derivation:** only scalars, or also simple lists? Keep the hard 10%
   explicitly hand-authored.
5. **config_schema_version** — derive/validate against the model, or keep manual?

## Related

- `features/futures/uniform-declarative-services.md` — the RC-5 brief; Phase 3 named
  `provider.config_editables()`/`config_section()` but shipped only the string-id link.
- `features/futures/uniform-service-declaration.md` — the `{name|reference, config}`
  file format this builds on.
- `features/service-provider-registry.md` — the delivered provider registry.
- `features/futures/editables-as-shared-validation-layer.md` — the editables-from-model
  pipeline this design sources from.

## publish-on-web: a service that deviates (boundary decision, 2026-07-25)

publish-on-web is not a simple service - it spans three config planes (component
tls/attachment, the deployment "Webadres" domain wizard, and a project-root
`domains:` approval state) PLUS cross-project platform infrastructure: an admin
domain-approval router (`router_subdomain_admin`), a global subdomain registry
(`connectors/subdomain.py`, a DB table unique across all projects), and ingress
generation (`project_manager` / `naming.py`).

Decision: the **service owns only its config-as-code contributions** - the component
TLS/attachment fieldset (`config_component_layout()` in
`opi/services/catalog/publish_on_web.py`). The approval state, the global registry,
the admin approver and ingress generation stay **platform infrastructure** the service
depends on; they are cross-project concerns, not per-service config, so forcing them
into a service module would break the per-service boundary. The deployment-level domain
wizard (DOMAIN_SECTION) is a candidate for a future `ConfigLayer.DEPLOYMENT` hook but
was left in the forms layer for now. See the full code map in the commit that landed
this.

`config_component_order` (static ClassVar, sorted by `_service_component_layouts()`)
gives a stable display order across component-level services; a user-facing priority
remains a deferred refinement.
