# Custom Domain Support

## What it is

Users can now specify their own custom domain (e.g. `mijnorganisatie.nl`) for deployments instead of being limited to pre-configured cluster domains (e.g. `rijks.app`, `rijksapp.nl`). The user takes responsibility for DNS configuration.

## How it works

The base-domain dropdown includes an "Eigen domein..." option. When selected, a text input appears where the user can type their custom domain. On submission, the custom domain value replaces the sentinel in the final YAML - no `__custom__` sentinel or transient field ever persists.

### Create wizard flow

1. User selects "Eigen domein..." from the base-domain dropdown
2. `data-rerender` fires, re-rendering the section
3. A text input appears (via `depends_on` / `show_when`)
4. User enters `mijnorganisatie.nl`
5. `CustomDomainValidator` validates the domain format
6. On submit, the processor resolves the deferral: `base-domain` gets `mijnorganisatie.nl`

### Edit wizard flow

1. Project is loaded with `base-domain: mijnorganisatie.nl` (a non-standard domain)
2. `populate_deferred_fields` detects the defer condition (via converter) and injects the stored value into the transient `base-domain:custom` field
3. The `CustomDomainSelectConverter` maps the select to `__custom__` for display
4. The text input shows `mijnorganisatie.nl`
5. On save, the same deferral resolution applies

## Architecture: The `defers_to` Pattern

This feature introduces a reusable "select with other" pattern for the editable system, built on three new primitives:

### EditableCondition protocol (`conditions.py`)

```python
class EditableCondition(Protocol):
    def check(self, value: Any) -> bool: ...
```

Concrete implementation: `SentinelValueCondition` - returns True when the value equals a configurable sentinel string (default: `__custom__`).

### `transient` flag on Editable

```python
Editable(
    yaml_path="deployments[0]/base-domain:custom",
    transient=True,
    ...
)
```

Transient fields participate fully in form state (rendering, validation, wizard step storage) but are stripped from the final YAML output by the processor. The colon-namespaced path (`base-domain:custom`) keeps the field visually associated with its parent.

### `defers_to` + `defer_when` on Editable

```python
Editable(
    yaml_path="deployments[0]/base-domain",
    defers_to="deployments[0]/base-domain:custom",
    defer_when=SentinelValueCondition(),
    ...
)
```

When `defer_when.check(current_value)` returns True, the processor copies the transient field's value into the parent's path. This happens generically in `EditableFormProcessor._resolve_deferrals()`.

### Processor methods

| Method | Direction | Purpose |
|--------|-----------|---------|
| `_resolve_deferrals()` | Write | Copy transient value to parent path when condition met |
| `_strip_transients()` | Write | Remove all transient fields from output YAML |
| `populate_deferred_fields()` | Read | Inject stored value into transient path for edit forms |

## Configuration

### Providers

Both `ClusterBaseDomainOptionsProvider` and `BaseDomainOptionsProvider` include `{"value": "__custom__", "label": "Eigen domein..."}` as the last option.

### Domain format

`DomainFormatOptionsProvider` treats `__custom__` base domains as supporting dot-separated hostnames, so all format options (dot + dash variants) are available.

### Validation

- `BaseDomainValidator` accepts `__custom__` as a valid intermediate value
- `CustomDomainValidator` checks syntactic domain validity (e.g. `voorbeeld.nl`)
- `validate_base_domain()` in `connectors/subdomain.py` now accepts any syntactically valid domain, not just the configured list

## Examples

### Editable definition (create wizard)

```python
DOMAIN_BASE_DOMAIN_EDITABLE = Editable(
    yaml_path="deployments[0]/base-domain",
    converter=CustomDomainSelectConverter(),
    defers_to="deployments[0]/base-domain:custom",
    defer_when=SentinelValueCondition(),
    ...
)

DOMAIN_CUSTOM_BASE_DOMAIN_EDITABLE = Editable(
    yaml_path="deployments[0]/base-domain:custom",
    transient=True,
    depends_on="deployments[0]/base-domain",
    show_when={"value": ["__custom__"]},
    validator=CustomDomainValidator(),
)
```

### Reusing the pattern for other fields

To add "select with other" to any field:

1. Add a sentinel option to the provider
2. Create a `CustomXxxConverter` that maps non-standard values to the sentinel on `view()`/`read()`
3. Add `defers_to` + `defer_when` to the parent editable
4. Create a transient child editable with `depends_on`/`show_when`

## Files

| File | Role |
|------|------|
| `editables/conditions.py` | `EditableCondition` protocol, `SentinelValueCondition` |
| `editables/editable.py` | `transient`, `defers_to`, `defer_when` fields |
| `editables/processor.py` | Deferral resolution and transient stripping |
| `editables/converters.py` | `CustomDomainSelectConverter` |
| `editables/validators.py` | `BaseDomainValidator` (sentinel), `CustomDomainValidator` |
| `connectors/subdomain.py` | `validate_base_domain()` accepts custom domains |
| `visualizers/providers.py` | `__custom__` option in domain providers |
| `editables/fields/domains.py` | Create wizard editables |
| `editables/fields/deployments.py` | Edit wizard editables |
| `visualizers/fields/domains.py` | Create wizard visualizers |
| `visualizers/fields/deployments.py` | Edit wizard visualizers |
| `visualizers/wizard_sections.py` | `DOMAIN_SECTION` layout |

## Troubleshooting

- **Custom domain not persisting**: Check that the parent editable has `defers_to` and `defer_when` configured, and the transient child has `transient=True`.
- **Text field not showing in edit mode**: Ensure `populate_deferred_fields()` is called before `_split_data_across_sections()` in the router.
- **`__custom__` appearing in saved YAML**: The processor's `_resolve_deferrals` should run before output. Check that the editables list passed to `process_json_submission` includes both the parent and transient child.
