# 05 - Source Code Part (Repositories & Registries)

## Overview

The Source Code part displays repository and container registry configurations. These are **mostly read-only** because they contain encrypted credentials (passwords, access tokens). Only `branch` and `path` are editable. This part appears only in the edit flow — repositories are auto-provisioned during project creation.

## YAML Structure

```yaml
repositories:
  - name: main-repo
    url: https://github.com/org/repo.git
    username: git
    password: |
      -----BEGIN AGE ENCRYPTED FILE-----
      YWdlLWVuY3J5cH...
      -----END AGE ENCRYPTED FILE-----
    branch: main
    path: .
    project_name: my-project

registries:
  - name: github-registry
    url: ghcr.io
    username: myuser
    password: |
      -----BEGIN AGE ENCRYPTED FILE-----
      ...
      -----END AGE ENCRYPTED FILE-----
```

## Editable Definitions

### Repository fields

```python
class ProjectEditables:

    # === Repositories ===

    REPO_NAME = ProjectEditable(
        yaml_path="repositories[*]/name",
        widget="text",
        label="repository.name",
        readonly=True,
    )

    REPO_URL = ProjectEditable(
        yaml_path="repositories[*]/url",
        widget="text",
        label="repository.url",
        readonly=True,
    )

    REPO_USERNAME = ProjectEditable(
        yaml_path="repositories[*]/username",
        widget="text",
        label="repository.username",
        readonly=True,
    )

    REPO_PASSWORD = ProjectEditable(
        yaml_path="repositories[*]/password",
        widget="display-card",
        label="repository.password",
        readonly=True,
        converter=EncryptedDisplayConverter(),
    )

    REPO_BRANCH = ProjectEditable(
        yaml_path="repositories[*]/branch",
        widget="text",
        label="repository.branch",
        description="repository.branch.description",
        # Editable — user can change default branch
    )

    REPO_PATH = ProjectEditable(
        yaml_path="repositories[*]/path",
        widget="text",
        label="repository.path",
        description="repository.path.description",
        # Editable — user can change subpath
    )
```

### Registry fields (fully read-only)

```python
    REGISTRY_NAME = ProjectEditable(
        yaml_path="registries[*]/name",
        widget="text",
        label="registry.name",
        readonly=True,
    )

    REGISTRY_URL = ProjectEditable(
        yaml_path="registries[*]/url",
        widget="text",
        label="registry.url",
        readonly=True,
    )

    REGISTRY_USERNAME = ProjectEditable(
        yaml_path="registries[*]/username",
        widget="text",
        label="registry.username",
        readonly=True,
    )

    REGISTRY_PASSWORD = ProjectEditable(
        yaml_path="registries[*]/password",
        widget="display-card",
        label="registry.password",
        readonly=True,
        converter=EncryptedDisplayConverter(),
    )
```

### Sequence grouping

```python
    REPOSITORIES_SEQUENCE = ProjectEditable(
        yaml_path="repositories",
        widget="sequence",
        label="source.repositories",
        min_items=0,
        children=[
            ProjectEditables.REPO_NAME,
            ProjectEditables.REPO_URL,
            ProjectEditables.REPO_USERNAME,
            ProjectEditables.REPO_PASSWORD,
            ProjectEditables.REPO_BRANCH,
            ProjectEditables.REPO_PATH,
        ],
    )

    REGISTRIES_SEQUENCE = ProjectEditable(
        yaml_path="registries",
        widget="sequence",
        label="source.registries",
        min_items=0,
        children=[
            ProjectEditables.REGISTRY_NAME,
            ProjectEditables.REGISTRY_URL,
            ProjectEditables.REGISTRY_USERNAME,
            ProjectEditables.REGISTRY_PASSWORD,
        ],
    )
```

## Converter

### EncryptedDisplayConverter

```python
class EncryptedDisplayConverter:
    """
    For read-only display of encrypted fields.

    Replaces the actual encrypted content with a status message.
    Never exposes the encrypted data in the form.
    """

    def read(self, value: Any) -> str:
        """For form inputs (not used — field is readonly)."""
        return ""

    def write(self, value: Any) -> Any:
        """Never writes — field is readonly."""
        return value  # Pass-through, preserve original

    def view(self, value: Any) -> str:
        """For display: show status message instead of encrypted content."""
        if value and isinstance(value, str) and "BEGIN AGE ENCRYPTED FILE" in value:
            return "Versleuteld opgeslagen"
        if value:
            return "Geconfigureerd"
        return "Niet geconfigureerd"
```

## How `display-card` renders

The `REPO_PASSWORD` editable uses `widget="display-card"`. The `EncryptedDisplayConverter.view()` transforms the AGE-encrypted blob into a display string. The `display-card` widget renders it as:

```html
<c-card padding="sm" outline>
    <c-layout-flow gap="xs">
        <div class="rvo-display-field__header">
            <c-icon icon="sleutel" size="sm" color="blauw" />
            <span class="utrecht-form-label">Wachtwoord</span>
        </div>
        <c-tag type="success" size="sm">Versleuteld opgeslagen</c-tag>
    </c-layout-flow>
</c-card>
```

This contrasts with the `text` widget for editable fields like `REPO_BRANCH`:

```html
<c-text-input-field
    id="repositories[0]/branch"
    name="repositories[0]/branch"
    label="Branch"
    value="main"
/>
```

## Part Definition

```python
class ProjectParts:

    SOURCE_CODE = EditablePart(
        part_id="source-code",
        title="Broncode",
        icon="code",
        description="Repository en registry configuratie",
        editables=[
            ProjectEditables.REPOSITORIES_SEQUENCE,
            ProjectEditables.REGISTRIES_SEQUENCE,
        ],
        layout=Fieldset(
            legend="source.title",
            description="source.description",
            children=[
                Fieldset(
                    legend="source.repositories.title",
                    children=[
                        Sequence(
                            field_name="repositories",
                            child_layout=Fieldset(
                                legend="repository.details",
                                children=[
                                    Row(children=[
                                        Column("name", width=4),     # → render_text() (readonly)
                                        Column("url", width=8),      # → render_text() (readonly)
                                    ]),
                                    Row(children=[
                                        Column("branch", width=4),   # → render_text() (editable)
                                        Column("path", width=4),     # → render_text() (editable)
                                    ]),
                                    "password",                      # → render_display_card()
                                ],
                            ),
                            min_items=0,
                        ),
                    ],
                ),
                Fieldset(
                    legend="source.registries.title",
                    children=[
                        Sequence(
                            field_name="registries",
                            child_layout=Row(children=[
                                Column("name", width=3),         # → render_text() (readonly)
                                Column("url", width=4),          # → render_text() (readonly)
                                Column("username", width=3),     # → render_text() (readonly)
                                Column("password", width=2),     # → render_display_card()
                            ]),
                            min_items=0,
                        ),
                    ],
                ),
            ],
        ),
        in_create_wizard=False,  # Edit only
        is_readonly=False,        # Has editable branch/path
        summary_fn=source_code_summary,
    )
```

## Save Flow

Only `branch` and `path` are saved. All other fields are readonly and **must not be included in form submission data**. The save handler:

```
POST /projects/{name}/parts/source-code

1. Parse form data — only contains "repositories[i]/branch" and "repositories[i]/path"
2. For each repository:
   - set_value(yaml, "repositories[0]/branch", "main")
   - set_value(yaml, "repositories[0]/path", ".")
3. All other fields (name, url, username, password) are preserved untouched
4. Registries are fully read-only — no changes
5. Write YAML, commit to git
```

## Display Summary

```python
def source_code_summary(data: dict) -> str:
    repos = get_value(data, "repositories") or []
    regs = get_value(data, "registries") or []
    parts = []
    if repos:
        parts.append(f"{len(repos)} repositor{'ies' if len(repos) != 1 else 'y'}")
    if regs:
        parts.append(f"{len(regs)} registr{'ies' if len(regs) != 1 else 'y'}")
    return ", ".join(parts) if parts else "Geen broncode geconfigureerd"
```

## Acceptance Criteria

- [ ] Repositories render with name, URL (read-only text) + branch, path (editable text)
- [ ] Passwords render as `display-card` with "Versleuteld opgeslagen" tag
- [ ] Registries render as read-only sequence (name, URL, username + password display-card)
- [ ] Saving only updates branch and path, preserving all other YAML fields exactly
- [ ] Encrypted fields in YAML are preserved bit-for-bit (no re-encryption or modification)
- [ ] No add/remove buttons on sequences (managed via API)
- [ ] Part does not appear in create wizard (`in_create_wizard=False`)
- [ ] Part conditionally shown in edit tabs only if repositories or registries exist
