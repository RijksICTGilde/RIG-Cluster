# 07 - Config Display Part (Read-Only)

## Overview

The Config Display part shows the project's encrypted configuration as read-only status cards. This includes AGE key pairs, API keys, and Keycloak realm configurations. All values are AGE-encrypted and cannot be edited through the form. This part is **purely display-only** — no form, no save button.

## YAML Structure

```yaml
config:
  age-public-key: age1ufgl52y9y2aumys23l3e6zplekaw4j3ndk2yrwgfteq44fgd0qaq6zcrz5
  age-private-key: |-
    -----BEGIN AGE ENCRYPTED FILE-----
    YWdlLWVuY3J5cH...
    -----END AGE ENCRYPTED FILE-----
  api-key: |-
    -----BEGIN AGE ENCRYPTED FILE-----
    YWdlLWVuY3J5cH...
    -----END AGE ENCRYPTED FILE-----
  keycloak:
    - host: https://keycloak.example.nl
      realm: project-realm
      username: admin_user
      password: |-
        -----BEGIN AGE ENCRYPTED FILE-----
        ...
        -----END AGE ENCRYPTED FILE-----
```

## Editable Definitions

All editables in this part have `readonly=True` and use `display-card` widgets with specialized converters.

```python
class ProjectEditables:

    # === Config (all read-only) ===

    CONFIG_AGE_PUBLIC_KEY = ProjectEditable(
        yaml_path="config/age-public-key",
        widget="display-card",
        label="config.age_public_key",
        readonly=True,
        converter=TruncateConverter(20),
    )

    CONFIG_AGE_PRIVATE_KEY = ProjectEditable(
        yaml_path="config/age-private-key",
        widget="display-card",
        label="config.age_private_key",
        readonly=True,
        converter=EncryptedDisplayConverter(),
    )

    CONFIG_API_KEY = ProjectEditable(
        yaml_path="config/api-key",
        widget="display-card",
        label="config.api_key",
        readonly=True,
        converter=EncryptedDisplayConverter(),
    )

    CONFIG_KEYCLOAK_REALMS = ProjectEditable(
        yaml_path="config/keycloak",
        widget="display-card",
        label="config.keycloak_realms",
        readonly=True,
        converter=KeycloakRealmsDisplayConverter(),
    )
```

## Converters

### TruncateConverter

```python
class TruncateConverter:
    """Truncates a value for display, showing first N characters + '...'."""

    def __init__(self, max_length: int = 20):
        self.max_length = max_length

    def view(self, value: Any) -> str:
        if not value:
            return "Niet geconfigureerd"
        value_str = str(value)
        if len(value_str) > self.max_length:
            return value_str[:self.max_length] + "..."
        return value_str

    def read(self, value: Any) -> Any:
        return value

    def write(self, value: Any) -> Any:
        return value  # Never written
```

### KeycloakRealmsDisplayConverter

```python
class KeycloakRealmsDisplayConverter:
    """Formats keycloak realm list for display."""

    def view(self, value: Any) -> list[dict[str, str]]:
        """Returns structured data for template rendering."""
        if not value or not isinstance(value, list):
            return []
        return [
            {
                "host": kc.get("host", ""),
                "realm": kc.get("realm", ""),
                "username": kc.get("username", ""),
            }
            for kc in value
            if isinstance(kc, dict)
        ]

    def read(self, value: Any) -> Any:
        return value

    def write(self, value: Any) -> Any:
        return value  # Never written
```

## How each `display-card` renders

### AGE public key (truncated)

Converter: `TruncateConverter(20)` → `"age1ufgl52y9y2aum..."`

```html
<c-card padding="md" outline>
    <c-layout-flow gap="xs">
        <div class="rvo-display-field__header">
            <c-icon icon="sleutel" size="md" color="blauw" />
            <c-heading type="h4" textContent="Versleuteling (AGE)" />
        </div>
        <c-tag type="success">Geconfigureerd</c-tag>
        <p class="rvo-text--sm rvo-text--subtle">
            Publieke sleutel: age1ufgl52y9y2aum...
        </p>
    </c-layout-flow>
</c-card>
```

### AGE private key / API key (encrypted)

Converter: `EncryptedDisplayConverter()` → `"Versleuteld opgeslagen"`

```html
<c-card padding="md" outline>
    <c-layout-flow gap="xs">
        <div class="rvo-display-field__header">
            <c-icon icon="sleutel" size="md" color="blauw" />
            <c-heading type="h4" textContent="API Sleutel" />
        </div>
        <c-tag type="success">Versleuteld opgeslagen</c-tag>
    </c-layout-flow>
</c-card>
```

### Keycloak realms

Converter: `KeycloakRealmsDisplayConverter()` → list of `{host, realm, username}`

```html
<c-card padding="md" outline>
    <c-layout-flow gap="xs">
        <div class="rvo-display-field__header">
            <c-icon icon="schild" size="md" color="blauw" />
            <c-heading type="h4" textContent="Keycloak Realms" />
        </div>
        <!-- One entry per realm -->
        <div class="rvo-display-field">
            <strong>project-realm</strong>
            <span class="rvo-text--sm rvo-text--subtle">
                https://keycloak.example.nl &middot; admin_user
            </span>
            <c-tag type="success" size="sm">Wachtwoord versleuteld</c-tag>
        </div>
    </c-layout-flow>
</c-card>
```

### Not configured state

When a config item doesn't exist yet:

```html
<c-card padding="md" outline>
    <c-layout-flow gap="xs">
        <div class="rvo-display-field__header">
            <c-icon icon="sleutel" size="md" color="grijs" />
            <c-heading type="h4" textContent="API Sleutel" />
        </div>
        <c-tag type="warning">Niet geconfigureerd</c-tag>
        <p class="rvo-text--sm">Wordt aangemaakt bij eerste deployment</p>
    </c-layout-flow>
</c-card>
```

## Part Definition

```python
class ProjectParts:

    CONFIG = EditablePart(
        part_id="config",
        title="Configuratie",
        icon="sleutel",
        description="Versleutelde configuratie (alleen-lezen)",
        editables=[
            ProjectEditables.CONFIG_AGE_PUBLIC_KEY,
            ProjectEditables.CONFIG_AGE_PRIVATE_KEY,
            ProjectEditables.CONFIG_API_KEY,
            ProjectEditables.CONFIG_KEYCLOAK_REALMS,
        ],
        layout=Fieldset(
            legend="config.title",
            children=[
                "config/age-public-key",     # → render_display_card()
                "config/age-private-key",    # → render_display_card()
                "config/api-key",            # → render_display_card()
                "config/keycloak",           # → render_display_card()
            ],
        ),
        in_create_wizard=False,
        is_readonly=True,  # Entire part is display-only
        summary_fn=config_summary,
    )
```

## No Dependencies

This part has no dependencies on other parts. It reads directly from the `config` section of the YAML. All fields are read-only, so there are no save-side dependencies either.

## Display Summary

```python
def config_summary(data: dict) -> str:
    items = []
    if get_value(data, "config/age-public-key"):
        items.append("AGE sleutels")
    if get_value(data, "config/api-key"):
        items.append("API sleutel")
    realms = get_value(data, "config/keycloak") or []
    if realms:
        items.append(f"{len(realms)} Keycloak realm{'s' if len(realms) != 1 else ''}")
    return ", ".join(items) if items else "Nog niet geconfigureerd"
```

## Acceptance Criteria

- [ ] All config items render as `display-card` with appropriate icons
- [ ] AGE public key shown truncated (first 20 chars + "...")
- [ ] AGE private key and API key shown as "Versleuteld opgeslagen"
- [ ] Keycloak realms show host, realm name, username per realm
- [ ] Unconfigured items show yellow "Niet geconfigureerd" tag
- [ ] No form inputs, no save button (`is_readonly=True`)
- [ ] Part does not appear in create wizard
- [ ] Part hidden entirely if project has no `config` section at all
- [ ] Private keys and passwords never displayed in any form
