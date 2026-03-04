# Invites as a Service

**Status**: Planned

## What It Is

Migration of the invite system from a standalone top-level `invites:` YAML key to a proper service under the `services:` list. This aligns invites with how all other project features (keycloak, auth-wall, postgresql, etc.) are modeled — selectable via service cards, configured in a wizard step, stored under `services/invites/config/...`.

## Why

- **Consistency**: Every other project capability is a service. Invites being a top-level key is an outlier.
- **Wizard integration**: Users can enable/configure invites through the same UI as other services.
- **Dependency management**: `requires=["services/keycloak"]` gives automatic dependency resolution — selecting invites auto-selects keycloak.

## Current YAML Structure (Top-Level)

```yaml
invites:
  settings:
    default_language: nl
  active:
  - key: welcome-to-docs
    realm_roles:
    - allowed-user
    application_url: https://docs.rijksapp.nl
    contact_email: robbert.uittenbroek@rijksoverheid.nl
    message:
      nl: Welkom bij Docs
      en: Welcome to Docs
    success_title:
      nl: Account aangemaakt
      en: Account created
    success_button:
      nl: Ga naar applicatie
      en: Go to application
```

## Target YAML Structure (Under Services)

```yaml
services:
- invites:
    config:
      key: welcome-to-docs
      realm_roles:
      - allowed-user
      contact_email: robbert.uittenbroek@rijksoverheid.nl
      message:
        nl: Welkom bij de applicatie
      success_title:
        nl: Account aangemaakt
      success_button:
        nl: Ga naar applicatie
```

### Key Changes

- **`active` list removed** — config IS the invite directly (single invite for now)
- **`application_url` omitted** — derived at runtime from first deployment's subdomain + base-domain
- **Dutch-only** — only `nl` language key for message/success_title/success_button (for now)
- **`settings` block removed** — `default_language` hardcoded to `nl`

---

## Implementation Plan

### Phase 1: Service Registration

**`opi/services/services_enums.py`** — Add `INVITES = "invites"` to `ServiceType` enum.

**`opi/services/services.py`** — Add `ServiceDefinition`:
- `name="Uitnodigingen"`
- `icon="brief"`, `color="hemelblauw"`
- `scope="deployment"` (project-wide, not per-component)
- `requires=["services/keycloak"]`
- `variables=[]` (no env vars injected into deployments)

### Phase 2: Wizard Editables

**`opi/forms/editables/fields/services.py`** — Add editable fields:

| Field | yaml_path | Widget | Validator |
|-------|-----------|--------|-----------|
| `INVITES_KEY` | `services/invites/config/key` | text | `SlugValidator` |
| `INVITES_REALM_ROLES` | `services/invites/config/realm_roles` | sequence | `RealmRoleValidator` (per item) |
| `INVITES_CONTACT_EMAIL` | `services/invites/config/contact_email` | text | `EmailValidator` |
| `INVITES_MESSAGE_NL` | `services/invites/config/message/nl` | textarea | — |
| `INVITES_SUCCESS_TITLE_NL` | `services/invites/config/success_title/nl` | text | — |
| `INVITES_SUCCESS_BUTTON_NL` | `services/invites/config/success_button/nl` | text | — |

All validators already exist. Defaults: `realm_roles=["allowed-user"]`, `success_title="Account aangemaakt"`, `success_button="Ga naar applicatie"`.

Register in `SERVICE_CONFIG_EDITABLES["invites"]`.

### Phase 3: Wizard Section

**`opi/forms/editables/wizard_sections.py`** — Add `INVITES_CONFIG_SECTION`:
- `visible=lambda data: "invites" in _extract_services(data)`
- Layout: 3 fieldsets (Uitnodigingslink, Toegangsrechten, Pagina-inhoud)

Register in `SERVICE_CONFIG_SECTIONS["invites"]` and `ALL_SECTIONS`.

**`opi/forms/editables/flows.py`** — Add section after `AUTH_WALL_CONFIG_SECTION` in both `CREATE_FLOW` and `EDIT_FLOW`.

### Phase 4: Runtime — Dual-Read for Migration

**`opi/handlers/project_file_handler.py`** — Update invite methods to read from new location first, legacy fallback second:

1. `_extract_invite_config_from_services()` — new helper, reads from `services/invites/config`
2. `extract_invites_config()` — try new location, fall back to legacy `invites.active[0]`
3. `get_invite_by_key()` — new schema: config dict IS the invite (no list), match on `config["key"]`
4. `get_all_active_invites()` — new schema: return `[config]` if key exists

Methods that operate on a passed-in invite dict (`get_invite_message`, `get_invite_success_title`, `get_invite_success_button`) need no changes — field names are identical.

### Phase 5: application_url Derivation

**`opi/api/invite_routes.py`** — Add `_derive_application_url()` helper:
- Reads first deployment's `subdomain` + `base-domain`
- Uses existing `generate_external_hostname()` + `generate_public_url()` from `opi/utils/naming`
- Fallback: explicit `application_url` in invite dict takes precedence (backward compat)

### Phase 6: Tests

- `tests/forms/test_wizard_sections.py` — Update section count, add invites visibility test
- `tests/test_invite_manager.py` — Dual-read tests (new + legacy schema), derivation test
- `tests/test_editables_integration.py` — Verify `smart_get_value`/`smart_set_value` for invite paths

---

## What Does NOT Change

- **`invite_manager.py`** — operates on a passed-in invite dict; works once handler returns correct data
- **`ServiceListConverter`** — already handles mixed str/dict service list
- **`smart_get_value`/`smart_set_value`** — already handle `services/X/config/...` paths
- **`ServiceOptionsProvider`** — auto-includes from `SERVICE_DEFINITIONS`
- **`resolve_service_dependencies`** — auto-adds keycloak from `requires`
- **Invite routes** (except `application_url` change) — call handler methods that are being updated

---

## Future Considerations

- **Multiple invites**: Restore list support with individual keys, each with different roles/settings
- **Dynamic/limited keys**: Keys that expire after N uses or are generated on-the-fly
- **Multi-language**: Add `en` fields alongside `nl` for message/success_title/success_button
- **`application_url` per-component**: When a project has multiple components, allow specifying which component URL to redirect to
- **`restrict_domain`**: Add to wizard UI (currently only available in legacy YAML)
- **`auth_methods`**: Add SSO/local toggles to wizard UI

---

## Files Summary

| File | Action |
|------|--------|
| `opi/services/services_enums.py` | Add `INVITES` enum value |
| `opi/services/services.py` | Add `ServiceDefinition` |
| `opi/forms/editables/fields/services.py` | Add 7 editable constants + register |
| `opi/forms/editables/wizard_sections.py` | Add section + register |
| `opi/forms/editables/flows.py` | Add section to both flows |
| `opi/handlers/project_file_handler.py` | Dual-read logic |
| `opi/api/invite_routes.py` | `_derive_application_url` helper |
| `tests/forms/test_wizard_sections.py` | Update + add tests |
| `tests/test_invite_manager.py` | Dual-read + derivation tests |
