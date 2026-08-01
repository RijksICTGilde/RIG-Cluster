# The service system

A **service** is a user-facing building block a project switches on in its project file:
`keycloak`, `postgresql-database`, `publish-on-web`, `persistent-storage`, ... Each one is
a self-contained package that declares everything about itself. Generic code iterates the
registry; it never branches per service.

All paths below are relative to `operations-manager/python/`.

## Service, manager, connector

Three layers, and mixing them up is the most common mistake.

| Layer | Where | Owns | May import |
|---|---|---|---|
| **Service** | `opi/services/catalog/<name>/` | The declaration: config shape, form fields, what to provision, what to add to manifests, what needs approval | Nothing heavy. No managers, no forms at module scope |
| **Manager** | `opi/manager/*.py` | Orchestration of one subsystem: create a realm and its clients, provision a database plus users and grants | Connectors, other managers |
| **Connector** | `opi/connectors/*.py` | The only place that talks to the outside world: kubectl, git, HTTP APIs, psql, subprocess | External libraries |

Two rules follow from this:

- **A service never imports a manager.** It receives the managers it needs on the context
  object (`ctx.keycloak_manager`, `ctx.get_manager("postgres")`). That keeps the catalog
  dependency-light and free of import cycles, which matters because the forms layer imports
  the catalog and the catalog would otherwise import the forms layer back.
- **A service never calls an external system directly.** No `subprocess`, no `httpx`, no
  `kubectl`. It delegates to a manager, which delegates to a connector.

A service is a *configuration-as-code unit*. A connector is *how OPI talks to a system*.
Redis the service and Redis the connector are different things and live in different layers.

## What a service package looks like

```
opi/services/catalog/keycloak/
├── __init__.py          the Service subclass: all hooks and declarations
├── config_model.py      Pydantic model for its config block
├── editables.py         Editable definitions (yaml paths, validators, converters)
├── visualizers.py       EditableVisualizer definitions (widgets, labels, help texts)
└── keycloak.v1.0.json   generated JSON-schema fragment, committed and drift-locked
```

Only `__init__.py` is required. A behaviour-only service is a few lines
(`opi/services/catalog/redis/__init__.py`); keycloak and namespace-postgres use every file.
Shared building blocks live in `catalog/shared/` (e.g. `storage.py` for the two storage
services).

## Identity and registration

Adding a service touches exactly three places:

1. `opi/services/services_enums.py` - a `ServiceType` member. This is the typed identity,
   used with Pyright coverage across the codebase.
2. `opi/services/services.py` - a `ServiceDefinition` in `ServiceAdapter.SERVICE_DEFINITIONS`:
   display name, description, icon, colour, scope, the variables it exposes to an app, and
   optionally `requires`, `backup_label`, `cleanup_strategy`. This is what the
   `/services` page renders and what a `Service` subclass binds automatically as
   `cls.definition`.
3. `opi/services/registry.py` - one line in `SERVICES`.

`tests/test_service_providers.py` fails CI if a `ServiceType` has no entry, so you cannot
forget step 3. Why the enum is hand-maintained rather than auto-discovered is argued in
`features/service-provider-registry.md`.

## Reading a service entry from a project file

The `services:` list is a *selection set keyed by service name*, and an entry has three
historical forms:

```yaml
services:
  - publish-on-web                      # bare string
  - name: keycloak                      # uniform record (project level)
    config: {...}
  - attachments:                        # legacy single-key dict
      data: [...]
```

Component and deployment references use `reference:` instead of `name:`.

**Always resolve the identity with `service_entry_name(entry)`** (`opi/services/services.py`),
never by reading the dict's keys. A record's raw keys are `name` and `config`, so key-reading
code silently drops every service that carries config. That exact bug has appeared in the
wizard merge, the component services picker and the approval lookup. Siblings:
`service_entry_config(entry)` and `service_entry_schema_version(entry)`.

For paths into a service's config use `smart_get_value` / `smart_set_value`
(`opi/forms/editables/service_path.py`); they navigate the mixed list correctly.

## Config

`config_model` is a Pydantic model and doubles as the guardrail and the schema source.

- `config_schema_version` is a quoted `major.minor` string, stored next to `config` in the
  file. `migrate_config(config, from_version)` is forward-only: an old file is migrated to
  the current version before validation, because ZAD only ever reads a possibly-old file and
  writes the current one.
- The global `opi/schemas/project_v2.json` validates only the entry envelope
  (`{name|reference, schema-version?, config?}`). Per-service config is validated against the
  model, and the committed fragment `catalog/<name>/<name>.v<version>.json` is drift-locked by
  `tests/test_service_config_schema.py`. Regenerate with
  `uv run python -m opi.services.config_schema`.
- Managers validate through `provider.validate_config(...)`, not raw `dict.get()`.

### Config layers

The same service can carry config at four levels, each with its own yaml-path shape.
Never hardcode those paths; build them with `config_path` (`catalog/base.py`):

```python
config_path(ConfigLayer.PROJECT,   ServiceType.KEYCLOAK, "config", "template")
# services/keycloak/config/template

config_path(ConfigLayer.COMPONENT, ServiceType.PUBLISH_ON_WEB, "config", "tls")
# components[*]/services{publish-on-web}/config/tls
```

| Layer | Shape | Typical use |
|---|---|---|
| `PROJECT` | `services/<svc>` | The service definition and its project-wide settings |
| `COMPONENT` | `components[*]/services{<svc>}` | Per-component settings (TLS mode, scrape port, storage mounts) |
| `DEPLOYMENT` | `deployments[*]/services{<svc>}` | Per-deployment state, usually OPI-managed |
| `DEPLOYMENT_COMPONENT` | `deployments[*]/components[*]/services{<svc>}` | Per-deployment override of a component setting |

## Forms

A service owns its own fields; the forms layer only collects them.

| Hook | Returns | Collected by |
|---|---|---|
| `config_editables(layer)` | `Editable`s (yaml path, validator, converter, defaults) | `registry.component_service_editables()` for the component layer |
| `config_component_visualizers()` | `EditableVisualizer`s (widget, label, help text) | `registry.component_service_visualizers()` |
| `config_component_layout()` | Layout nodes (`Fieldset`, `Sequence`) for the component form | `wizard_sections._service_component_layouts()` |
| `config_form_section(layer)` | A whole `FormSection` for a project-level config step | `wizard_sections.SERVICE_CONFIG_SECTIONS`, keyed by `config_section_id` |
| `config_api_fields(layer)` | Field names the API/YAML accepts | validation-error guidance + which API targets the service exposes |

`config_section_id` and `modal_flow_id` are declarative links: the provider names the
section, the forms layer holds the `FormSection` object. Ordering across services is
`config_component_order`.

Editables at the project level use `virtualize=("services", "_services-config")`. The form
posts under the virtual key so per-service config cannot collide with the service *selection*
list, and `WizardState.get_merged_data` folds it back onto the real `services` list.

A project-level config section reaches the user through three places, and all three must be
wired: the create/edit wizard (`CREATE_FLOW`/`EDIT_FLOW` in `flows.py`), the "Services
beheren" modal (`MODAL_EDIT_SERVICES_FLOW`, so add-then-configure works), and its own
`modal_flow_id` flow behind the service card's "Configureer" button. If a step needs data
from an earlier step (e.g. a component-name select), place its section *after* that step in
the flow. Cover the three flows with a **user-based** sandbox E2E test -- real button
clicks and field fills, no `page.evaluate` shortcuts and no direct modal-fragment URLs.
`tests/e2e/test_sandbox_sleep_mode_ui.py` + `tests/e2e/helpers/service_config.py` are the
pattern to copy.

## API (configuring via REST)

A service that owns a `config_model` is configurable through the REST API for free --
you do not write an endpoint. At startup `opi/api/v2/router.py`
(`_register_service_config_routes`) walks the registry and, for every
`(service, target)` the service accepts config on, generates a typed route whose
**request body is that service's own `config_model_for(target)`**:

```
PUT/DELETE /api/v2/projects/{project}/services/<service>/config/<target>[/{name}]
GET        /api/v2/projects/{project}/services/{service}/config      # read
GET        /api/v2/services                                          # catalog + targets
```

Because the body is the typed model, the OpenAPI spec documents the fields and enum
values per service, so a client can be generated from it. There is deliberately **no
per-service endpoint file** -- every service's endpoint is identical apart from its
model, so one generator beats 13 copies. What a service owns is the *contract* (the
model + the layer hooks); the router owns the uniform *exposure*.

- Which targets a service exposes is measured from its own declarations:
  `config_api_fields(layer)` **or** `config_editables(layer)` **or** (for the
  component layer) `config_component_layout()`. A sequence-config service (storage is
  a `RootModel[list]`) has no flat `config_api_fields` but is still reached through
  its editables.
- Validation is the model itself (the same one the wizard's save runs through
  `validate_service_configs`), so there is no second validation path. The typed body
  also rejects a bad value synchronously (422) before the async task is enqueued.
- Configuring on a component/deployment implicitly selects the service at the project
  level (a bare entry in the root `services` list), so a component-only write does not
  require the caller to add it there first.

Full reference: `features/service-config-api.md`. So a new service needs to do
nothing beyond declaring its `config_model` (and layer hooks) to be API-configurable.

## Detail page (read-only presentation)

A service owns not only its *input* (`config_form_section`) but also its read-only
*presentation* on the project-details page. Without this, a service's block sits hardcoded
in the general template and drifts away from its config on every move -- exactly what
happened to the Keycloak realm block when RC-5 relocated the realms into the service.

| Hook | Returns | Collected by |
|---|---|---|
| `detail_page_sections(project_data, user_role)` | `DetailPageSection`s (a template + its context) | `registry.collect_detail_page_sections()` |

- `project_data` is the **decrypted** project dict, so a service can surface managed
  credentials; `user_role` lets the service gate on the viewer (return `[]` to omit).
- Only services the project actually uses (project-level or referenced by a component)
  are asked; sections render in registry order, in place of a hardcoded `{% include %}`.
- Put the template **next to the service** under `opi/services/catalog/<svc>/` and address
  it as `<svc>/<file>` -- the catalog directory is on the Jinja search path (see
  `opi/core/templates.py`). The include gets the `DetailPageSection` as `section`, so the
  template reads its data from `section.context`.

`KeycloakService.detail_page_sections` is the reference implementation.

## Hooks at a glance

Every hook a service may implement, so a new service knows what it can own:

| Hook | Purpose |
|---|---|
| `config_editables(layer)` / `config_api_fields(layer)` | config data + accepted API fields; also determine which API config targets the service exposes |
| `config_form_section(layer)` | project-level wizard/edit config step |
| `config_component_layout()` / `config_component_visualizers()` | per-component form fields |
| `detail_page_sections(project_data, user_role)` | read-only detail-page block |
| `config_approvals(layer)` | values that need approval before taking effect |
| `provision(ctx)` / `handle_service_removal(ctx)` | server-side resources |
| `contribute_manifest_context(ctx)` / `build_secret_files(ctx)` | manifest + secret contributions |

## Provisioning and cleanup

```python
async def provision(self, ctx: ProvisionContext) -> None:
    await ctx.keycloak_manager.create_resources_for_deployment(ctx.project_data, ctx.deployment)
```

- `provisioning_services()` runs every service that overrides `provision`, ordered by
  `provision_order`. The default is a no-op, so a service without server-side resources
  declares nothing.
- Provisioning must be **replay-safe**. It runs again on every process of the project;
  "already exists" is a normal outcome, not an error.
- Cleanup on removal: set `cleanup_manager_key` and the generic dispatch reaches your
  manager through `ctx.get_manager(key)`. Override `handle_service_removal` only for
  behaviour the manager cannot express.
- `cleanup_strategy` on the `ServiceDefinition` decides whether removal acts immediately or
  is deferred (a PVC is marked, not deleted).

## Manifests

A service does not touch the manifest generator. It returns a declarative contribution and
the generic component loop merges it (`opi/manager/project_manager.py:5108`).

```python
def contribute_manifest_context(self, ctx: ManifestContext) -> ManifestContribution:
    return ManifestContribution(
        env_from_secrets=[KeycloakSecret.get_secret_name(ctx.deployment_name)],
    )
```

`ManifestContribution` has four fields with two different merge semantics:

| Field | Semantics |
|---|---|
| `env_from_secrets` | **Additive**, appended in `manifest_order` |
| `sidecars` | **Additive**, appended in `manifest_order` |
| `template_vars` | **Override**, `dict.update` on the template context (auth-wall moves `service_port` 8080 to 4180) |
| `secret_files` | `SecretFileSpec`s the shared writer turns into SOPS secret manifests |

What you get in `ManifestContext`: `deployment_name`, `project_data`, `unique_name`,
`cluster`, `component_def` (the resolved component, for component-level config) and
`get_secret(deployment_name, secret_type, secret_class)` to reach an already-provisioned
secret without importing the manager.

Two shortcuts for the common cases:

- Only need an `envFrom` secret? Set `manifest_secret_class` and `manifest_order`; the base
  class builds the contribution for you.
- Contribute on behalf of another service? `manifest_activated_by` says which service types
  switch you on, so exactly one provider contributes per manager.

`SecretFileSpec` declares *what* secret is needed; `ProjectManager._write_secret_file`
(`project_manager.py:1159`) does the writing, alias resolution and prune bookkeeping. Keep
that writer service-agnostic.

Rendered output is byte-locked by `tests/test_golden_manifests.py`. An intentional change is
regenerated with `UPDATE_GOLDEN=1`, and the diff is part of the review.

## Approvals

A service can declare that a value it manages needs someone's approval before it takes
effect (`opi/services/catalog/approval.py`). Today only publish-on-web uses it, for domains
and subdomains, but the mechanism is generic and the approver UI needs no change to pick up
a new one.

`ApprovalSpec` has four callbacks:

| Callback | Question | Consumed by |
|---|---|---|
| `status_of` | Is this value approved? | enforcers, gating |
| `list_items` | What is open for the approver? | `collect_approval_items` → the admin approvals page |
| `record` | Write down this verdict | `apply_approval_verdicts`, which builds the uniform history entry |
| `notices_for` | What does an ungranted approval mean for this deployment? | `collect_deployment_approval_notices` → the project page |

The verdict history (`{date, status, by, message}`) is appended by the spec's `record`; the
last status wins and the file is the audit trail. The *consequence* of a verdict is service
knowledge: publish-on-web writes the sentence itself, because only it knows that an
unapproved domain does not block the deployment but moves it to the cluster address
(`apply_domain_approval_fallback` in `opi/utils/naming.py`).

Note the split between blocking and enforcing: a user picking a rejected domain is stopped
at the form field, but the save gate accepts the state, otherwise an approver could not
record a revocation on a domain that is already in use. Enforcement happens at publication.

## Adding a service

1. `ServiceType` member + `ServiceDefinition` entry.
2. `catalog/<name>/__init__.py` with a `Service` subclass, and one line in `SERVICES`.
3. Config? Add `config_model.py`, set `config_model` + `config_schema_version`, run
   `uv run python -m opi.services.config_schema`, commit the fragment.
4. UI? Add `editables.py` + `visualizers.py`, and either `config_component_layout()` for the
   component form or a `FormSection` plus `config_section_id` for a project-level step.
5. Provisions resources? Override `provision`, set `provision_order`, delegate to a manager.
6. Server-side resources to clean up? Set `cleanup_manager_key`.
7. Manifest contribution? `manifest_secret_class` for the simple case, otherwise override
   `contribute_manifest_context` / `build_secret_files` and set `manifest_order`.
8. Needs approval? Add `ApprovalSpec`s from `config_approvals(layer)`.

You should not need to edit generic code, a flow list, or the global schema `$defs`. If you
do, the hook you need is probably missing and adding it beats special-casing your service.

## Traps

- **Identity via `service_entry_name`, always.** See the entry forms above.
- **A services list is a selection set.** A name may appear at most once, at every level.
  `validate_project_structure` rejects a duplicate; the wizard merge folds entries so one
  cannot arise.
- **Do not seed a service's config defaults onto something that has not selected it.** The
  `{K}` path filter materialises the service into the list as a side effect, so a default
  quietly turns into a selection.
- **Provisioning is replay-safe by contract**, and so is manifest generation.
- **Keep the catalog import-light.** Import forms, managers and connectors inside the method
  that needs them, not at module scope.

## Guardrails

```bash
cd operations-manager/python
uv run pytest tests/test_service_providers.py tests/test_service_config_schema.py \
              tests/test_golden_manifests.py tests/test_flow_registry_snapshot.py -q
uv run ruff check . --fix && uv run ruff format . && uv run pyright
```

| Test | Fails when |
|---|---|
| `test_service_providers.py` | A `ServiceType` has no service, the registry has extras, a definition drifted, or the provisioning/cleanup/manifest contract changed |
| `test_service_config_schema.py` | A config model and its committed schema fragment diverge |
| `test_golden_manifests.py` | Rendered manifests changed |
| `test_flow_registry_snapshot.py` | The registry-derived flow and section dicts changed |

## Related

- `features/service-provider-registry.md` - why the registry exists and what it replaced
- `features/components-services-deployments.md` - the Project / Service / Component / Deployment model
- `features/manifest-extension-pipeline.md` - how manifests are assembled
- `operations-manager/CLAUDE.md` - module map and code style
