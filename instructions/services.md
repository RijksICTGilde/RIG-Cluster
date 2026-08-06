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
├── __init__.py            the Service subclass: its ServiceDefinition, all hooks and declarations
├── config_model.py        Pydantic model for its config block
├── editables.py           Editable definitions (yaml paths, validators, converters)
├── variables.py           the env variables it hands to a deployment
├── visualizers.py         EditableVisualizer definitions (widgets, labels, help texts)
├── help.html.j2           the long explanation behind the question mark
├── section-detail.html.j2 its block on the project page
└── keycloak.v1.0.json     generated JSON-schema fragment, committed and drift-locked
```

Everything a service *is* lives here (RC-36): its metadata, its variables and its
explanation used to sit in shared files, so taking a service over meant editing three of
them. `tests/test_service_package_is_self_contained.py` fails if any of it creeps back
out. The measure is literal: copy the directory, rename it, and it works.

Only `__init__.py` is required. A behaviour-only service is a few lines
(`opi/services/catalog/redis/__init__.py`); keycloak and namespace-postgres use every file.
Shared building blocks live in `catalog/shared/` (e.g. `storage.py` for the two storage
services).

`editables.py` and `visualizers.py` are the service's user interface. A package without them
has no wizard screens, which is fine for a service the platform switches on itself and wrong
for one a user is supposed to configure. See "Forms and wizard screens".

## Identity and registration

Adding a service touches exactly three places:

1. `opi/services/services_enums.py` - a `ServiceType` member. This is the typed identity,
   used with Pyright coverage across the codebase.
2. `catalog/<name>/__init__.py` - a `ServiceDefinition` as the class attribute
   `definition`: display name, description, icon, colour, binding, the variables it exposes
   to an app, and optionally `requires`, `backup_label`, `cleanup_strategy`. This is what the
   `/services` page renders. `ServiceAdapter.SERVICE_DEFINITIONS` is assembled from what the
   services declare, in `ServiceType` order; there is no shared list to add to. A subclass
   that sets `service_type` without a `definition` is refused at class-creation time.
3. `opi/services/registry.py` - one line in `SERVICES`.

`tests/test_service_providers.py` fails CI if a `ServiceType` has no entry, so you cannot
forget step 3. Why the enum is hand-maintained rather than auto-discovered is argued in
`features/service-provider-registry.md`.

**Registration alone yields no UI.** Steps 1-3 make a service *exist* and be dispatchable;
they do not put it on any screen. A user-selectable service must NOT set `hidden=True` on its
`ServiceDefinition` -- `hidden` drops the service card from the wizard's services step, so the
service is fully wired yet invisible and unselectable. That is the intended state for
internal, always-on or namespace-variant services (`platform`, `namespace-redis`), and it is
exactly what once left `sleep-mode` working end to end while never appearing in the wizard.
If your service is meant to be chosen by a user, leave `hidden` off and give it its form
section (below).

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

### Three kinds of config: define, use, bind

"Service config" is one word for three different things, and until RC-38 nothing said
which one a layer meant. `ConfigRole` (`catalog/base.py`) names them, and a service
answers per layer through `config_roles(layer)`:

| Role | Means | Where it lives |
|---|---|---|
| `DEFINE` | put something into the project that nothing uses yet | under `data` on the project-level service entry |
| `USE` | this component/deployment uses this service, this thing | under `config` |
| `BIND` | *how* the used thing reaches the workload | under `config`, next to the use |

For nearly the whole catalog the answer is `USE` at every layer it carries config on, and
that is the default -- there is nothing to define, and the binding is implied by the
service itself. Attachments is the first service where the three come apart: it defines a
catalog at project level, and a component both uses one (`reference`) and binds it
(`provide-as` / `path` / `env-name`).

A DEFINE layer needs a model of its own, because a definition is not a config block:
`data_model_for(layer)` returns it, and `validate_service_configs` walks the `data` block
through it. That walk is why the attachments catalog is validated at all -- it sat under
`data`, the config walk only looked at `config`, and the shape was guarded by nothing.

The roles also answer whether a layer deserves an endpoint. "Config on a layer, so an
endpoint" is the right direction but not a law: check per service that the endpoint would
mean something. Attachments has no project-level *config* route (there is no config block
there) and instead declares an upload action for its DEFINE side.

`tests/test_service_config_roles.py` holds every service to naming a role for each layer
it carries config on.

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

### Binding is not a config layer

`ServiceDefinition.binding` (`ServiceBinding.COMPONENT` / `DEPLOYMENT`) and `ConfigLayer`
look like the same question and are not:

| | Answers | Read it for |
|---|---|---|
| `binding` | Does an individual component tick this service, or does a whole deployment get it at once | Selection: the per-component services checkbox group, "is this component-bound" checks |
| `config_layers()` | At which levels of the project file this service carries settings | Configuration: which screen a setting is edited on |

They genuinely disagree, so neither is a stand-in for the other. keycloak binds per
component (each component decides whether it sits behind login) while its configuration is
one realm for the whole project, so its config lives at `ConfigLayer.PROJECT` and nowhere
else. The field was called `scope` until RC-33, which read like an answer to "where do I
configure this" -- and the project-details card rendered it as literally "Component scope",
which is how a user came to expect a keycloak settings screen per component.

**Anything that tells a user where to configure something reads the layers**, via
`service.config_layers()` / `service.config_form_section(layer)`, never `binding`.
`opi/services/config_location.py` holds the derived, user-facing phrasing
(`project_step_config_hint`, `binding_label`); `tests/test_service_config_location.py`
locks which of the two is the source of truth, with keycloak as the counterexample.

That module is also the answer to a service that carries no project-level config at all.
The project-wide services step can only show sections for `ConfigLayer.PROJECT`, so ticking
a component-only service there used to produce nothing and explain nothing. The card now
carries one derived line ("Geen projectbrede instellingen; u stelt deze dienst per
component, bij Componenten in."). A new service needs no template change to get it: the
sentence is built from the layers the service declares.

## Forms and wizard screens

**Registering a service gives it no UI at all.** The enum, the `ServiceDefinition` and the
registry line make it exist, provision, and contribute manifests. They do not put a checkbox
in the wizard and they do not create a single input field. Every screen is something you
declare on the service, and for the project level you also wire it into the forms layer by
hand. Skipping this is the most common way a new service lands "finished" but unreachable
for a user: sleep-mode shipped that way, fully working, with no wizard presence whatsoever.

A service therefore has two independent UI questions, and you must answer both.

### 1. Does the user pick the service? The selection card

The services step renders one card per service (`SERVICES_EDITABLE` in
`forms/editables/fields/services.py`, the `SERVICE_CARDS` visualizer in
`forms/visualizers/fields/services.py`). Its options come from `ServiceOptionsProvider`
(`forms/visualizers/providers.py:83`), which iterates `ServiceType` and **skips every
definition with `hidden=True`** (`providers.py:116`). So:

- `hidden=False` (the default): the service appears as a card in the create wizard
  (`SERVICES_SECTION`) and in the detail-page modal `modal-edit-services`
  (`SERVICES_EDIT_SECTION`). A user can switch it on and off.
- `hidden=True`: no card anywhere. The service can only be switched on by editing the project
  file, by an API call, or by a cluster-wide default the service owns itself.

`hidden=True` is a legitimate choice (`platform` is implicit, `namespace-redis` is a variant
picked by policy, sleep-mode is driven by a cluster default plus a `match` pattern), but it
is a *decision*, not a default you inherit. If a user is supposed to enable your service,
`hidden` must stay `False` and you owe the user a configuration screen as well.

The card itself is rendered by the `service_block` macro in
`opi/templates/widgets/_macros.html.j2` - icon, name, description and help button - and the
services overview page (`services-overview.html.j2`) renders the same macro, so both places
show the same thing. Do not build a second service block; `tests/test_service_help.py`
fails if either template starts rendering its own.

Every definition carries a `help_template`: `"<package>/help.html.j2"`, the Jinja2 file in
the service's own package with the long explanation shown when the user clicks the question
mark. (`opi/templates/help/` still holds the few explanations that belong to no single
service, such as the container-image note.) The one-line `description` is
too short to choose on, so the long text is where a user actually decides. The same test
fails when a service has no `help_template` or points at a file that does not exist - both of
which fail silently in the UI (no button, or an error inside the modal).

Component-level selection is a second, separate list: the per-component `services` checkbox
group uses `FilteredServiceOptionsProvider`, which shows only services the *project* already
selected. A component-scoped service that is not selected at project level can never be
ticked on a component.

### 2. Where does its config live? One mechanism per layer

The four `ConfigLayer`s are not interchangeable, and only one of them is wired end to end
for you. Pick the layer from where the value belongs, then implement that row:

| Layer | Where the user sees it | What you implement | Wiring you must still do by hand |
|---|---|---|---|
| `PROJECT` | Its own wizard step / modal, shown when the service is selected | `config_editables(PROJECT)`, `config_form_section(PROJECT)`, `config_section_id`, optionally `modal_flow_id` | Register the section and add it to the flows, see below |
| `COMPONENT` | A fieldset inside the per-component form | `config_editables(COMPONENT)`, `config_component_visualizers()`, `config_component_layout()`, `config_component_order` | **None.** The registry collects it automatically |
| `DEPLOYMENT` | No service-owned form hook exists today | Nothing to hook into | Fields are hand-authored in `forms/editables/fields/deployments.py`. Deployment-level config is normally OPI-managed state, not user input |
| `DEPLOYMENT_COMPONENT` | A fieldset inside the per-deployment component form | `config_editables(DEPLOYMENT_COMPONENT)`, `config_deployment_component_visualizers()`, `config_deployment_component_layout()` | **None.** The registry collects it, like the component layer (RC-25) |

**Every layer you carry config on needs an answer to "where do I edit this".** That answer
is `config_form_section(layer)`, or an entry in `form_exempt_layers` naming the reason
there is no form (OPI-written state, API-only on purpose).
`tests/test_service_config_layers.py` fails if a layer has neither, in both directions --
an unanswered layer and a stale exemption for a layer that no longer carries config. Before
RC-25 half the catalog had config nobody could reach, which is what that test now prevents.

For the component and deployment-component layers you get the section for free: the base
class builds it from the visualizers and layout nodes you already declare, so it can never
show a different field set than the component form does. Only the project layer needs a
hand-built section (and the flow wiring below).

`config_api_fields(layer)` is separate from all of this: it tells the API/YAML validator which
field names the layer accepts, so an error message can name them. Declare it per layer, even
when the layer has no form.

### A service that owns a plain project-file property

A SYSTEM service can own a *property of the component* rather than a block in a `services:`
list -- `user-env-vars` and `aliases` do (RC-25). Declare it with `owned_property`; then
`validate_service_configs` walks that property on every layer the service declares editables
for, and the generic config API generates no route for the service (there is no config block
for that endpoint to address). Everything else -- config model, schema fragment, editables,
form sections -- is identical to any other service. See
`features/system-services-with-a-ui.md`.

### Project-level config: what "declarative" does and does not cover

The service builds the whole `FormSection` itself (auth-wall is the smallest complete
example, `catalog/authorization_wall/__init__.py:61`):

```python
class AuthorizationWallService(Service):
    config_section_id = "auth-wall-config"                  # names the section
    modal_flow_id = "modal-edit-auth-wall-config"           # names its modal flow

    def config_editables(self, layer: ConfigLayer):
        if layer is not ConfigLayer.PROJECT:
            return []
        from opi.services.catalog.authorization_wall.editables import AUTH_WALL_BANNER_EDITABLE
        return [AUTH_WALL_BANNER_EDITABLE]

    def config_form_section(self, layer: ConfigLayer):
        if layer is not ConfigLayer.PROJECT:
            return None
        ...
        return FormSection(
            section_id="auth-wall-config",
            title="Authorization wall configuratie",
            visible=self._config_selected,               # only when the service is selected
            post_save_action="process_project",
            editables=[AUTH_WALL_BANNER],                # visualizers, not editables
            layout=[config_path(ConfigLayer.PROJECT, self.service_type, "config", "banner")],
        )
```

Cache the built section on the instance: consumers compare section *identity*
(`EDIT_SECTIONS[...] is AUTH_WALL_CONFIG_SECTION`), so rebuilding it per call breaks them.

`config_section_id` and `modal_flow_id` are only *links*. The forms layer still has to be
told the object exists, in four places:

1. `forms/visualizers/wizard_sections.py`: re-export the section
   (`AUTH_WALL_CONFIG_SECTION = get_service(...).config_form_section(ConfigLayer.PROJECT)`)
   and add it to `_CONFIG_SECTIONS_BY_ID`. `SERVICE_CONFIG_SECTIONS` and `EDIT_SECTIONS`
   derive from there.
2. `forms/visualizers/flows.py`: add the section to `CREATE_FLOW` and `EDIT_FLOW` (a section
   with a `visible=` predicate is listed unconditionally and hides itself), and to
   `MODAL_EDIT_SERVICES_FLOW` so the detail-page "Services beheren" modal walks the user
   through the config right after switching the service on.
3. `forms/visualizers/flows.py`: if you declared `modal_flow_id`, add the matching
   `FormFlow` and its `FLOW_REGISTRY` entry, otherwise the per-service config button on the
   project page points at a flow that does not exist. `SERVICE_CONFIG_MODAL_FLOWS` is derived,
   the flow object is not.
4. `tests/golden/flow_registry.json`: regenerate, the added section and flow are locked
   (`UPDATE_GOLDEN=1 uv run pytest tests/test_flow_registry_snapshot.py -q`).

Yes, that is generic code you touch for a project-level screen. It is the one place the
declarative story is not finished; do not conclude from it that a component-level service
needs the same, and do not invent a new mechanism to avoid it.

### Component-level config: fully automatic

Declare the three hooks and you are done. metrics-scraper
(`catalog/metrics_scraper/__init__.py`) is the reference:

```python
def config_editables(self, layer):            # data: paths, converters, validators
    return [METRICS_PORT_EDITABLE, METRICS_PATH_EDITABLE] if layer is ConfigLayer.COMPONENT else []

def config_component_visualizers(self):       # presentation: widget, label, help text
    return [METRICS_PORT, METRICS_PATH]

def config_component_layout(self):            # placement inside the component form
    return [Fieldset(legend="...", depends_on="services",
                     show_when={"contains": svc}, children=[...])]
```

`registry.component_service_editables()` and `component_service_visualizers()` flatten these
across all services in `config_component_order`, and
`wizard_sections._service_component_layouts()` appends the layout nodes to the component
form. No section, no flow, no snapshot edit.

### Editable versus visualizer

Two objects, deliberately split, and mixing them up produces a field that saves nothing or
renders nothing:

- **`Editable`** (`<service>/editables.py`) is the *data* contract: `yaml_path` (always built
  with `config_path(...)`, never a hand-typed string), `converter`, `validator`, `required`,
  `default`, `depends_on` + `show_when` for conditional display, `children` for a repeating
  `Sequence`, `remove_when_none` to drop an emptied key.
- **`EditableVisualizer`** (`<service>/visualizers.py`) wraps that same editable with `widget`,
  `label` and `description`. A `FormSection.editables` list holds *visualizers*; the field
  name in `layout=[...]` is the editable's yaml path.

Editables that write into the `services` list use `virtualize=("services", "_services-config")`.
The form posts under the virtual key so per-service config cannot collide with the service
*selection* list, and `WizardState.get_merged_data` folds it back onto the real `services`
list. Leaving `virtualize` off is how a config field silently overwrites the selection.

A project-level config section reaches the user through three places, and all three must be
wired: the create/edit wizard (`CREATE_FLOW`/`EDIT_FLOW` in `flows.py`), the "Services
beheren" modal (`MODAL_EDIT_SERVICES_FLOW`, so add-then-configure works), and its own
`modal_flow_id` flow behind the service card's "Configureer" button. If a step needs data
from an earlier step (e.g. a component-name select), place its section *after* that step in
the flow. Cover the three flows with a **user-based** sandbox E2E test -- real button
clicks and field fills, no `page.evaluate` shortcuts and no direct modal-fragment URLs.
`tests/e2e/test_sandbox_sleep_mode_ui.py` + `tests/e2e/helpers/service_config.py` are the
pattern to copy.

### Editables: validators, enforcers and closed sets

An `Editable` declares three things: **where** the value lives (`yaml_path`, built with
`config_path` so the layer and service are enums, not a literal), **which** values are valid
(`validator`), and **how** a submitted string becomes a stored value (`converter`). A field
with no validator leans on the JSON schema, which fires at *process* time, not at *save*
time -- exactly how a project once got silently blocked (a value the schema forbade sailed
through the form and only failed later, invisibly).

1. **Pick a select when the set of valid values is known and closed** (cluster, template,
   probe scheme, duration, role); pick free text when the value is genuinely open (a name, a
   glob, a URL, a message). `opi/services/catalog/sleep_mode/editables.py` is the model: eight
   of nine fields are selects, only `match` is free text (it holds glob patterns for
   deployments that do not exist yet).
2. **Every select has an `OptionsProvider`** registered in
   `PROVIDER_REGISTRY` (`opi/forms/visualizers/providers.py`). A provider that depends on the
   surrounding form data takes `yaml_data` in its constructor -- the bridge injects it (and
   `current_value`) by matching the provider's `__init__` params.
   `WakerComponentOptionsProvider` and `InviteRealmRoleOptionsProvider` are the examples.
3. **A select is not validation.** The browser can post anything, and the API and YAML paths
   never go through the form at all. Behind a closed set put *either* an `AllowedValuesValidator`
   (`validators.py`) *or* a `Literal` in the config model (`WakeMode` in
   `sleep_mode/config_model.py`, `AuthMethod` in `invite/config_model.py`). A value's
   membership must be checked regardless of which widget the user saw.
4. **A select must never silently drop a value it does not recognise.** If the stored value is
   not in the current options (a role removed from the keycloak config, a renamed component),
   add it back as a flagged option; otherwise the next save falls back to the first option and
   the configuration changes with nobody touching it. `InviteRealmRoleOptionsProvider` keeps
   the stored value, marked "(bestaat niet meer)".
5. **Validator vs enforcer.** A `validator` is per-field, synchronous, and returns messages
   (`EditableValidator`, `opi/forms/editables/editable.py`). An enforcer is for rules across
   fields or with I/O, is async, and raises `ValueError`, `FieldError` or `FieldWarning`
   (`AsyncEditableEnforcer`). It hangs off the `FormSection` (`section.enforcer`), gets the
   merged data, and receives `enforcer_context` (e.g. `project_name`). Always tie a
   `FieldError` to a field the user actually sees -- an error on an invisible path arrives as
   a step that will not advance with no explanation. `UniqueInviteKeyEnforcer` is the model:
   cross-project, async, `FieldError` on the key field.
6. **Optional fields** carry `remove_when_none=True` so an emptied field drops the key instead
   of writing `null` (`KEYCLOAK_RESTRICT_ACCESS_EDITABLE`). Keep an editable `default=` equal
   to the model default; the model is the guardrail, the default is only what the empty form
   shows, and a default must never be sown into a project that did not select the service.

Existing building blocks, so nobody writes a fifth name validator:

| Need | Use | Where (`opi/forms/editables/`) |
|---|---|---|
| Kubernetes-resource name | `KubernetesNameValidator` | `validators.py` |
| Component name (incl. uniqueness) | `ComponentNameValidator` | `validators.py` |
| Keycloak realm-role name | `RealmRoleValidator` | `validators.py` |
| URL | `UrlValidator` | `validators.py` |
| Email | `EmailValidator` | `validators.py` |
| Closed value set | `AllowedValuesValidator` | `validators.py` |
| Required field | `required=True` + `RequiredValidator` | `validators.py` |
| Uniqueness within a sequence | `UniqueNamesEnforcer` | `enforcers.py` |
| Cross-field rule with I/O | `DomainConfigEnforcer` (example) | `enforcers.py` |

**Checklist for a new field:** a `yaml_path` built with `config_path`, a field in the config
model, a validator or a closed select with `AllowedValues`/`Literal`, a visualizer with a
label and help text, and a line in the section layout.

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

- Which targets a service exposes is measured from its own declarations: the layer is
  in `config_layers()` (editables, API fields, layout nodes or a modelled payload) **and**
  `config_model_for(layer)` gives something to validate a write against. A sequence-config
  service (storage is a `RootModel[list]`) has no flat `config_api_fields` but is still
  reached through its editables. Before RC-38 this re-derived the same hooks separately,
  so the target list and the generated routes could come apart.
- **Every config field carries a `description`.** It is what the schema fragment and the
  OpenAPI document show a caller, and `tests/test_service_config_field_descriptions.py`
  fails on a field without one -- nested value objects included.
- Validation is the model itself (the same one the wizard's save runs through
  `validate_service_configs`), so there is no second validation path. The typed body
  also rejects a bad value synchronously (422) before the async task is enqueued.
- Configuring on a component/deployment implicitly selects the service at the project
  level (a bare entry in the root `services` list), so a component-only write does not
  require the caller to add it there first.

Full reference: `features/service-config-api.md`. So a new service needs to do
nothing beyond declaring its `config_model` (and layer hooks) to be API-configurable.

### When editables are not enough: declared actions

Editables stay the starting point, and editables and the API will never coincide exactly
-- that is fine, as long as the difference is deliberate and both live with the service.
Where a service can do something the form has no field for, it declares an **action**
(`catalog/actions.py`, `api_actions()`), in its own package's `api.py`.

The declaration says once: the layer and its `ConfigRole`s, the fields and what each one
means (that text lands in the OpenAPI document), the verbs, the valid field combinations
with a dotted pointer to where that rule is *already* enforced, and a worked example.
Route, multipart signature and documentation are generated from it.

**A field points at the shared `Editable`; it never restates a rule.** Same move as
`opi/api/validation.py` (RC-26): reference the object the wizard renders, wrap it in
"required here" or "optional here" and nothing else. A field with genuinely no editable
(a file's bytes) sets `no_editable_reason` -- a written exception, not a second validator.

The verbs are a contract, not a style choice:

| Verb | HTTP | Id already exists | Id absent |
|---|---|---|---|
| create | `POST` | 409, refuse | create |
| update | `PUT` | replace | 404, refuse |
| upsert | `PUT ?upsert=true` | replace, without asking | create |

Replacing on id without warning is only ever the upsert, and the caller has to ask for it:
a `POST` that quietly overwrites lies about what it did.

Attachments is the first inhabitant: uploading a file into the catalog (project level) and
uploading plus coupling in one request (component level). See
`features/service-api-actions.md`.

## Detail page (read-only presentation)

A service owns not only its *input* (`config_form_section`) but also its read-only
*presentation* on the project-details page. Without this, a service's block sits hardcoded
in the general template and drifts away from its config on every move -- exactly what
happened to the Keycloak realm block when RC-5 relocated the realms into the service.

| Hook | Returns | Collected by |
|---|---|---|
| `detail_page_sections(project_data, user_role)` | `DetailPageSection`s (a template + its context) | `registry.collect_detail_page_sections()` |
| `deployment_page_sections(ctx)` | the same, for ONE deployment | `registry.collect_deployment_page_sections()` |
| `definition.actions_provider(project_data, deployment_name)` | `DeploymentAction`s (buttons) | `registry.collect_deployment_actions()` |
| `web_routers()` | the `APIRouter`s that serve this service's own fragments/modals | `registry.collect_service_routers()` |

- `project_data` is the **decrypted** project dict, so a service can surface managed
  credentials; `user_role` lets the service gate on the viewer (return `[]` to omit).
- Only services the project actually uses (project-level or referenced by a component)
  are asked; sections render in registry order, in place of a hardcoded `{% include %}`.
- Put the template **next to the service** under `opi/services/catalog/<svc>/` and address
  it as `<svc>/<file>` -- the catalog directory is on the Jinja search path (see
  `opi/core/templates.py`). The include gets the `DetailPageSection` as `section`, so the
  template reads its data from `section.context`.

### Which of the two section hooks

`detail_page_sections` is about the project (the Keycloak realms, the invite links, the
attachment catalog). `deployment_page_sections` is about ONE deployment (its metrics, its
backups) and is asked once per deployment on the Deployments tab. Its `ctx`
(`DeploymentPageContext`) adds the deployment, the managed cluster, and
`backend_available` -- the availability of optional back-ends (`prometheus`, `backups`)
that the view probed, because a service must not call a connector itself.

### A block a service does not own alone

Backups belong to every service with a `backup_label`; the two modals belong to both
PostgreSQL services. Such a block is delivered by each owner (through a shared mixin in
`catalog/shared/`), and the collectors keep one copy: sections dedupe on template name,
actions on (label, endpoint), routers on object identity -- so return the SAME router
object from every owner. Page mixins are cooperative (`super()`), since a service can
carry more than one.

### Endpoints belong with the block

A block that lazy-loads (backups) or is a modal (the database console, the job runner)
needs routes. Declare them on the service via `web_routers()` and they are mounted onto
the web app, so the block and the endpoints that fill it travel together. Import the
route module **inside** `web_routers()`: those modules import managers, which the catalog
itself must not do.

A modal button is a `DeploymentAction` with `modal_endpoint` + `modal_title` instead of
`endpoint`; the shared modal shell loads that URL (`openServiceModal`). One or the other,
never both.

`KeycloakService.detail_page_sections` is the reference implementation for the project
level, `MetricsScraperService.deployment_page_sections` for the deployment level, and
`catalog/shared/backups.py` for a jointly-owned block with its own route.

## Hooks at a glance

Every hook a service may implement, so a new service knows what it can own:

| Hook | Purpose |
|---|---|
| `config_editables(layer)` / `config_api_fields(layer)` | config data + accepted API fields; also determine which API config targets the service exposes |
| `config_roles(layer)` | what the config at a layer is: define, use and/or bind |
| `data_model_for(layer)` | model for the DEFINE-side payload (under `data`), for a service that defines something |
| `api_actions()` | extra API actions the service declares (fields, verbs, example) beyond the generic config endpoints |
| `config_form_section(layer)` | project-level wizard/edit config step |
| `config_component_layout()` / `config_component_visualizers()` | per-component form fields |
| `detail_page_sections(project_data, user_role)` | read-only detail-page block (project level) |
| `deployment_page_sections(ctx)` | read-only detail-page block for one deployment |
| `web_routers()` | the endpoints those blocks need (fragments, modals) |
| `config_approvals(layer)` | values that need approval before taking effect |
| `provision(ctx)` / `handle_service_removal(ctx)` | server-side resources |
| `contribute_manifest_context(ctx)` / `build_secret_files(ctx)` | manifest + secret contributions (per component) |
| `contribute_deployment_manifests(ctx)` | deployment-wide manifests (once per deployment, e.g. a NetworkPolicy) |
| `observe_deployment(ctx)` | act on a just-synced deployment (`HookPoint.AFTER_SYNC`) |
| `deployment_state(ctx)` | what this service knows about a deployment (`HookPoint.DEPLOYMENT_STATE`) |
| `on_redeploy(ctx)` | clear the state you recorded about content that was just replaced (`HookPoint.REDEPLOY`) |

### Contributing state about a deployment

A service that puts a deployment in a particular situation -- sleep-mode scaling it to
zero and parking a waker in front of it -- reports that through `deployment_state(ctx)`.
Generic code (`collect_deployment_state`, the health check, the deployment page) then
learns the situation from the service that caused it instead of inferring it from what
the cluster happens to show. Read `features/deployment-state-and-health.md` before adding
one.

Two rules, both load-bearing:

- **Return facts, never a health verdict.** `DeploymentStateFact` deliberately has no
  "healthy" field. If "I am asleep" could be phrased as "and therefore fine", a service
  with a stale state would hide a real outage. `expects_no_application_pods` is the one
  operational consequence a service may state, and it excuses only the ABSENCE of the
  application's pods -- never a problem observed on a pod that is there.
- **Answer from the project file, not the cluster.** That is where a service records what
  it did, and it keeps the hook synchronous and connector-free, so a page render can ask
  it as cheaply as the health check does.

### Clearing state when new content is rolled out

`deployment_state(ctx)` reports what a service did to a deployment; `on_redeploy(ctx)` is
where it undoes it. The hook fires when a deliberate action puts new content on a
deployment -- an image update, an upsert of an existing deployment -- and every state a
service recorded about the previous content stops holding at that moment. Read
`features/redeploy-clears-recorded-state.md` before adding one.

Three rules:

- **Named after the action, not after the trigger.** An image update and an upsert are the
  same event as far as recorded state is concerned. The hook this replaced was a hardcoded
  `if` on one disable reason in `project_manager`, and every further case would have been
  another exception beside it.
- **Clear unconditionally; do not reason about the new content.** Whatever the recorded
  reason said, it was about content that is gone. If the new content has the same problem,
  the observing path records it again -- against the thing that actually caused it.
- **Say what you cleared.** Return one line per cleared item, in the user's language. State
  that disappears silently leaves a component switched back on with nobody able to see why
  it was off.

Mutate `ctx.project_data` in place and never commit: the caller commits the rollout and
every cleanup in one commit, so two services cannot race to two commits.

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

### Deployment-wide manifests

`contribute_manifest_context` runs once per component. For a resource that belongs to the
*whole deployment* (a NetworkPolicy that references the deployment's cross-project peers,
say), override `contribute_deployment_manifests(ctx: DeploymentManifestContext) ->
list[DeploymentManifestSpec]` instead. It runs once per deployment, after the component loop.
Each `DeploymentManifestSpec` names a template + values + a `filename` that **must** start
with `f"{deployment}-{service_type.value}-"` -- the symmetric prune
(`project_manager._prune_obsolete_service_manifests`) keys on that prefix to remove a
service's deployment manifests when it stops contributing (switched off, last rule removed,
target gone). `registry.deployment_manifest_services()` collects the overriding services in
`manifest_order`; the generic emitter in `create_application_manifests` writes the specs and
the on-disk-glob kustomization picks them up. cross-domain-access is the reference user.

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

1. `ServiceType` member.
2. `catalog/<name>/__init__.py` with a `Service` subclass carrying its own `definition`, and
   one line in `SERVICES`. Variables it exposes go in `variables.py` in the same package.
3. Config? Add `config_model.py`, set `config_model` + `config_schema_version`, run
   `uv run python -m opi.services.config_schema`, commit the fragment.
4. **Decide the UI, explicitly, and write down the decision.** Two questions:
   - *May a user switch this on?* Then `hidden` stays `False` and the service gets a card in
     the services step. If not, set `hidden=True` **and say in the definition why**, so the
     next reader sees a choice instead of an oversight.
   - *Does it have settings a user should reach?* Then add `editables.py` + `visualizers.py`
     and implement the row for its layer: `config_component_layout()` for a component-level
     service (automatic), or a `FormSection` + `config_section_id` plus the four wiring steps
     for a project-level one. A user-selectable service without a config screen only works if
     it genuinely has nothing to configure.
5. **Write the explanation.** Add `catalog/<name>/help.html.j2` and point `help_template` at
   `"<name>/help.html.j2"`. Follow the shape of the existing ones: one paragraph *what is it*
   in plain language, *Wanneer gebruik je dit?* as a list of recognisable situations, and
   *Wat wordt er ingesteld?* with what happens technically and which other services come
   along. A system service has no "when do you use this" - explain instead that it always
   runs and what it does for the user. Use the service's own icon and colour.
6. Provisions resources? Override `provision`, set `provision_order`, delegate to a manager.
7. Server-side resources to clean up? Set `cleanup_manager_key`.
8. Manifest contribution? `manifest_secret_class` for the simple case, otherwise override
   `contribute_manifest_context` / `build_secret_files` and set `manifest_order`.
9. Needs approval? Add `ApprovalSpec`s from `config_approvals(layer)`.
10. Walk the UI you claim to have built: open `/projects/<name>/modal-wizard/modal-edit-services`
   and the create wizard, and check the card, the config step and the modal button are really
   there. "The code is complete" and "a user can reach it" are two different statements.

Behaviour, component-level fields and the global schema `$defs` need no generic-code edits: if
you find yourself special-casing your service there, the hook you need is probably missing and
adding it beats the special case. A **project-level** config screen is today's exception, and
its four wiring points are listed under "Forms and wizard screens".

## Traps

- **A registered service has no UI.** The registry drives behaviour, not screens. A service
  without form hooks is invisible in the wizard, and `hidden=True` removes even its card.
  Neither is reported by any test, because "no UI" is a valid configuration for some services.
- **The four config layers each have their own wiring.** Only the component layer is collected
  automatically. A project-level section also needs registering in `wizard_sections.py`, adding
  to the flows in `flows.py`, a modal `FormFlow` when you declared `modal_flow_id`, and a
  regenerated flow snapshot.
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
              tests/test_service_help.py \
              tests/test_golden_manifests.py tests/test_flow_registry_snapshot.py -q
uv run ruff check . --fix && uv run ruff format . && uv run pyright
```

| Test | Fails when |
|---|---|
| `test_service_providers.py` | A `ServiceType` has no service, the registry has extras, a definition drifted, or the provisioning/cleanup/manifest contract changed |
| `test_service_config_schema.py` | A config model and its committed schema fragment diverge |
| `test_service_help.py` | A service has no `help_template`, its file is missing or does not render, or a template stopped using the `service_block` macro |
| `test_golden_manifests.py` | Rendered manifests changed |
| `test_flow_registry_snapshot.py` | The registry-derived flow and section dicts changed |

## Related

- `features/service-provider-registry.md` - why the registry exists and what it replaced
- `features/components-services-deployments.md` - the Project / Service / Component / Deployment model
- `features/manifest-extension-pipeline.md` - how manifests are assembled
- `operations-manager/CLAUDE.md` - module map and code style
