# Phase 1: Domain Requirements Orchestration

## Goal

Define the domain requirements for each form section: what YAML fields are editable, how they group into UI sections, what dependencies exist between fields, and how the create wizard and edit tabs compose them. These specs describe **what** the form system needs to support — not how to build it.

**Reference architecture:** `00-architecture.md`

## Relationship to Other Phases

```
Phase 1 (this)   → Domain requirements: what each form part contains
Phase 2          → Core infrastructure: the editables package (editable.py, path.py, bridge.py, etc.)
Phases 3–9       → Wiring: connect each part spec to the infrastructure, build routes/templates
```

Phase 1 specs define the `ProjectEditable` and `EditablePart` instances. Phase 2 builds the classes and utilities those instances depend on. Phases 3–9 implement the actual form parts using both.

## Spec Files

| Spec | File | Scope | Complexity |
|------|------|-------|------------|
| 00 | `00-architecture.md` | Overall architecture, data flow, protocols, reuse strategy | Reference |
| 01 | `01-project-identity.md` | name, display-name, description, clusters | Simple |
| 02 | `02-team-members.md` | users sequence with email + role | Sequence + enforcer |
| 03 | `03-services.md` | Mixed str/dict services list, HTMX config sub-forms | Most complex UX |
| 04 | `04-components.md` | Nested sequence with cross-part dependencies | Complex nesting |
| 05 | `05-source-code.md` | Repositories + registries, encrypted fields, read-only | Display-only |
| 06 | `06-deployments.md` | Deployment sequence, cross-part refs, encrypted config | Complex nesting |
| 07 | `07-config-display.md` | AGE keys, API keys, keycloak realms, all read-only | Display-only |
| 08 | `08-flows.md` | Create wizard + edit tabs composition | Flow assembly |

## Dependency Map Between Specs

```
00-architecture (reference — all specs depend on this)
 │
 ├─ 01-project-identity     ← standalone, no cross-part dependencies
 ├─ 02-team-members          ← standalone, no cross-part dependencies
 ├─ 03-services              ← standalone, but other specs depend ON it
 │
 ├─ 04-components            ← depends on 03 (uses-services filtered by project services)
 │                           ← depends on itself (uses-components references other components)
 ├─ 05-source-code           ← standalone (read-only, edit-only)
 │
 ├─ 06-deployments           ← depends on 04 (component references)
 │                           ← depends on 05 (repository references)
 │                           ← depends on 03 (deployment services)
 │
 ├─ 07-config-display        ← depends on 03 (keycloak realms only if keycloak enabled)
 │                           ← standalone otherwise (read-only)
 │
 └─ 08-flows                 ← depends on all of 01–07 (composes parts into wizard/tabs)
```

### Cross-Part Dependencies (detail)

| From (spec) | Field | Depends on (spec) | Field | Type |
|-------------|-------|--------------------|-------|------|
| 04 Components | `uses-services` | 03 Services | `services` | Option filtering |
| 04 Components | `publish-on-web` | 03 Services | `services` contains "publish-on-web" | Conditional visibility |
| 04 Components | `sso-rijk` | 03 Services | `services` contains "keycloak" | Conditional visibility |
| 04 Components | `uses-components` | 04 Components | other `components[*]/name` | Self-reference filtering |
| 06 Deployments | `components[*]/reference` | 04 Components | `components[*]/name` | Option filtering |
| 06 Deployments | `repository` | 05 Source Code | `repositories[*]/name` | Option filtering |
| 07 Config | keycloak realms display | 03 Services | `services` contains "keycloak" | Conditional visibility |

## What Each Spec Defines

Each spec (01–07) follows the same structure:

1. **YAML structure** — the raw YAML format for that section
2. **Editable definitions** — `ProjectEditable` instances with yaml_path, widget, validators, converters, dependencies
3. **Part definition** — `EditablePart` grouping with layout, enforcer, wizard/tab config
4. **Layout** — `LayoutElement` tree for field arrangement
5. **Rendering behavior** — HTMX triggers, conditional visibility rules, display-only cards

Spec 08 defines the two `FormFlow` compositions (create wizard and edit tabs) that reference the parts from specs 01–07.

## Phase 2 Infrastructure Required Per Spec

Each domain spec relies on specific Phase 2 sub-parts:

| Spec | Phase 2 sub-parts needed | Key dependencies |
|------|--------------------------|------------------|
| 01 Identity | A (ProjectEditable, EditablePart), B (path utils), G (bridge) | SlugValidator (E), ClusterOptionsProvider (C) |
| 02 Users | A, B, G | EmailValidator (E), RequiredValidator (E), AdminRequiredEnforcer (F), sequence widget |
| 03 Services | A, B, G, H (display-card) | ServiceListConverter (D), service-cards widget, HTMX patterns |
| 04 Components | A, B, G | IntegerListConverter (D), UniqueNamesEnforcer (F), ServiceDependencyEnforcer (F), cross-part option filtering (C) |
| 05 Source Code | A, B, G, H (display-card) | EncryptedDisplayConverter (D), TruncateConverter (D), display-card widget (H) |
| 06 Deployments | A, B, G, H (display-card) | EncryptedDisplayConverter (D), CloneFromDisplayConverter (D), cross-part refs (C) |
| 07 Config | A, B, G, H (display-card) | EncryptedDisplayConverter (D), TruncateConverter (D), KeycloakRealmsDisplayConverter (D) |
| 08 Flows | A (FormFlow, FlowMode) | All parts from 01–07 |

## Incremental Build Order (Phases 3–9)

After Phase 2 infrastructure is complete, individual form parts are wired up in complexity order. Each phase proves an additional capability.

| Phase | Spec | What it proves |
|-------|------|----------------|
| 3 | 01 Identity | End-to-end: editable → FormField → render → save → YAML |
| 4 | 02 Users | Sequences work with editables + part-level enforcers |
| 5 | 03 Services | ServiceListConverter + HTMX config sub-forms + service-cards widget |
| 6 | 04 Components | Nested sequences + cross-part dependencies + option filtering |
| 7 | 05 + 07 Source Code + Config | Display-card widget + encrypted field handling (read-only parts) |
| 8 | 06 Deployments | Most complex nesting + cross-part refs to components/repos |
| 9 | 08 Flows | Full wizard + tabs composition, review step, session handling |

## Key Design Decisions (from Phase 1)

These decisions constrain Phase 2 and later implementations:

1. **YAML dict is the schema** — no intermediate Pydantic models. `get_value()` / `set_value()` operate directly on the project YAML dict.
2. **Sync protocols** — `EditableConverter`, `EditableValidator`, `EditableEnforcer` are intentionally synchronous, unlike the existing async protocols in `field.py`.
3. **Bridge pattern** — `editable_to_form_field()` converts editables to the existing `FormField` for rendering. The rendering pipeline (`FormRenderer` + `ROOSWidgetAdapter`) is reused unchanged.
4. **Mixed service list** — services are `list[str | dict]`. `ServiceListConverter` handles the conversion. This format is preserved in YAML.
5. **Three levels of field dependencies** — static visibility (`depends_on`/`show_when`), HTMX-driven dynamic visibility, and cross-part option filtering via context injection.
6. **Dutch error messages** — all validator and enforcer messages are in Dutch.
7. **display-card widget** — new widget type added to `ROOSWidgetAdapter` for read-only encrypted/status fields.

## Verification

Phase 1 is a spec-only phase — no code is produced. Verification is manual review:

- [x] ✅🔍 Each spec (01–07) defines complete `ProjectEditable` instances for all YAML fields
- [x] ✅🔍 Each spec defines an `EditablePart` with layout
- [x] ✅🔍 Cross-part dependencies are documented in the dependency map above
- [x] ✅🔍 Spec 08 composes all parts into wizard and tabs flows
- [x] ✅🔍 All specs reference infrastructure from Phase 2 sub-parts (protocols, converters, validators, enforcers, providers)
- [x] ✅🔍 No spec assumes infrastructure that isn't covered by a Phase 2 sub-part

BUILD_COMPLETE_MARKER
VERIFY_COMPLETE_MARKER
