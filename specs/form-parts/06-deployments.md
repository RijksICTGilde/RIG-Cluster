# 06 - Deployments Part

## Overview

The Deployments part manages where and how the project is deployed. Each deployment targets a specific cluster and maps component definitions to container images. This is the most complex edit-only section, with encrypted configuration fields, clone-from references, and **cross-part dependencies** to components and repositories.

## YAML Structure

```yaml
deployments:
  - name: productie
    cluster: odcn-production
    namespace: amt-odc-prd
    repository: main-repo
    subdomain: amt
    base-domain: rijksapp.nl
    issuer: letsencrypt
    components:
      - reference: component-1
        image: ghcr.io/minbzk/amt:pr-620
        imagePullPolicy: Always
    configuration: |-
      -----BEGIN AGE ENCRYPTED FILE-----
      ...
      -----END AGE ENCRYPTED FILE-----
    clone-from:
      type: remote-source
      reference: odcn-production
      mode: once
      status:
        completed: true
        timestamp: '2026-02-03T12:11:43.661019+00:00'
    services:
      - reference: minio-storage
        config:
          generation: 1
```

## Editable Definitions

### Per-deployment fields

```python
class ProjectEditables:

    # === Deployments ===

    DEPLOYMENT_NAME = ProjectEditable(
        yaml_path="deployments[*]/name",
        widget="text",
        label="deployment.name",
        placeholder="productie",
        readonly_on_edit=True,
        required=True,
    )

    DEPLOYMENT_CLUSTER = ProjectEditable(
        yaml_path="deployments[*]/cluster",
        widget="select",
        label="deployment.cluster",
        options_provider="ClusterOptionsProvider",
        required=True,
    )

    DEPLOYMENT_NAMESPACE = ProjectEditable(
        yaml_path="deployments[*]/namespace",
        widget="text",
        label="deployment.namespace",
        readonly=True,  # Auto-generated, never editable
    )

    DEPLOYMENT_REPOSITORY = ProjectEditable(
        yaml_path="deployments[*]/repository",
        widget="select",
        label="deployment.repository",
        # Options populated from project repositories (cross-part dependency)
        options_provider="RepositoryOptionsProvider",
    )

    DEPLOYMENT_SUBDOMAIN = ProjectEditable(
        yaml_path="deployments[*]/subdomain",
        widget="text",
        label="deployment.subdomain",
        placeholder="mijn-app",
    )

    DEPLOYMENT_BASE_DOMAIN = ProjectEditable(
        yaml_path="deployments[*]/base-domain",
        widget="select",
        label="deployment.base_domain",
        options_provider="BaseDomainOptionsProvider",
    )

    DEPLOYMENT_ISSUER = ProjectEditable(
        yaml_path="deployments[*]/issuer",
        widget="select",
        label="deployment.issuer",
        # Options: letsencrypt, self-signed, none
    )
```

### Deployment component mapping (nested sequence)

```python
    DEPLOYMENT_COMP_REFERENCE = ProjectEditable(
        yaml_path="deployments[*]/components[*]/reference",
        widget="select",
        label="deployment.component.reference",
        # Options populated from project components (cross-part dependency)
        options_provider="ComponentReferenceOptionsProvider",
    )

    DEPLOYMENT_COMP_IMAGE = ProjectEditable(
        yaml_path="deployments[*]/components[*]/image",
        widget="text",
        label="deployment.component.image",
        description="deployment.component.image.description",
        placeholder="ghcr.io/org/image:latest",
    )

    DEPLOYMENT_COMP_PULL_POLICY = ProjectEditable(
        yaml_path="deployments[*]/components[*]/imagePullPolicy",
        widget="select",
        label="deployment.component.pull_policy",
        options_provider="PullPolicyOptionsProvider",
    )

    DEPLOYMENT_COMPONENTS_SEQUENCE = ProjectEditable(
        yaml_path="deployments[*]/components",
        widget="sequence",
        label="deployment.components",
        min_items=0,
        children=[
            ProjectEditables.DEPLOYMENT_COMP_REFERENCE,
            ProjectEditables.DEPLOYMENT_COMP_IMAGE,
            ProjectEditables.DEPLOYMENT_COMP_PULL_POLICY,
        ],
    )
```

### Read-only display fields

```python
    DEPLOYMENT_CONFIGURATION = ProjectEditable(
        yaml_path="deployments[*]/configuration",
        widget="display-card",
        label="deployment.configuration",
        readonly=True,
        converter=EncryptedDisplayConverter(),
    )

    DEPLOYMENT_CLONE_FROM = ProjectEditable(
        yaml_path="deployments[*]/clone-from",
        widget="display-card",
        label="deployment.clone_from",
        readonly=True,
        converter=CloneFromDisplayConverter(),
    )

    DEPLOYMENT_SERVICES = ProjectEditable(
        yaml_path="deployments[*]/services",
        widget="display-card",
        label="deployment.services",
        readonly=True,
        converter=DeploymentServicesDisplayConverter(),
    )
```

## Converters

### CloneFromDisplayConverter

```python
class CloneFromDisplayConverter:
    """Formats clone-from metadata for display."""

    def view(self, value: Any) -> str:
        if not value or not isinstance(value, dict):
            return ""
        reference = value.get("reference", "onbekend")
        clone_type = value.get("type", "onbekend")
        status = value.get("status", {})
        if status.get("completed"):
            timestamp = status.get("timestamp", "")
            return f"Gekloond van {reference} ({clone_type}) — Voltooid op {timestamp}"
        return f"Gekloond van {reference} ({clone_type}) — Bezig..."

    def read(self, value: Any) -> Any:
        return value

    def write(self, value: Any) -> Any:
        return value  # Never written
```

### DeploymentServicesDisplayConverter

```python
class DeploymentServicesDisplayConverter:
    """Formats deployment-level service overrides for display."""

    def view(self, value: Any) -> str:
        if not value or not isinstance(value, list):
            return "Geen deployment services"
        names = [s.get("reference", "onbekend") for s in value if isinstance(s, dict)]
        return ", ".join(names) if names else "Geen deployment services"

    def read(self, value: Any) -> Any:
        return value

    def write(self, value: Any) -> Any:
        return value  # Never written
```

## Dependencies

| Field | Depends On | Type | Effect |
|-------|-----------|------|--------|
| `deployments[*]/repository` | `repositories[*]/name` | Cross-part option filter | Select shows repo names |
| `deployments[*]/components[*]/reference` | `components[*]/name` | Cross-part option filter | Select shows component names |

These are Level 3 dependencies (cross-part option filtering). At render time, the handler passes the full project YAML as context, and the options providers extract the available names.

## How `display-card` renders for deployment fields

### Encrypted configuration

```html
<c-card padding="sm" outline>
    <div class="rvo-display-field">
        <c-icon icon="sleutel" size="sm" color="blauw" />
        <span class="utrecht-form-label">Deployment configuratie</span>
        <c-tag type="success" size="sm">Versleuteld opgeslagen</c-tag>
    </div>
</c-card>
```

### Clone-from status

```html
<c-card padding="sm" outline>
    <div class="rvo-display-field">
        <c-icon icon="kopie" size="sm" color="blauw" />
        <span class="utrecht-form-label">Gekloond van</span>
        <span>odcn-production (remote-source)</span>
        <c-tag type="success" size="sm">Voltooid op 2026-02-03</c-tag>
    </div>
</c-card>
```

## Part Definition

```python
class ProjectParts:

    DEPLOYMENTS = EditablePart(
        part_id="deployments",
        title="Deployments",
        icon="raket",
        description="Beheer waar en hoe uw project wordt gedeployed",
        editables=[...],  # All deployment editables
        layout=Fieldset(
            legend="project.deployments.title",
            children=[
                Sequence(
                    field_name="deployments",
                    child_layout=Fieldset(
                        legend="deployment.details",
                        children=[
                            Row(children=[
                                Column("name", width=4),       # → render_text() (readonly on edit)
                                Column("cluster", width=4),    # → render_select()
                                Column("namespace", width=4),  # → render_text() (readonly)
                            ]),
                            Row(children=[
                                Column("subdomain", width=4),    # → render_text()
                                Column("base-domain", width=4),  # → render_select()
                                Column("issuer", width=4),       # → render_select()
                            ]),
                            "repository",                        # → render_select()
                            Fieldset(
                                legend="deployment.components.title",
                                children=[
                                    Sequence(
                                        field_name="components",
                                        child_layout=Row(children=[
                                            Column("reference", width=3),       # → render_select()
                                            Column("image", width=7),           # → render_text()
                                            Column("imagePullPolicy", width=2), # → render_select()
                                        ]),
                                        min_items=0,
                                        add_label="Component mapping toevoegen",
                                    ),
                                ],
                            ),
                            "configuration",  # → render_display_card()
                            "clone-from",     # → render_display_card()
                            "services",       # → render_display_card()
                        ],
                    ),
                    min_items=0,
                    add_label="Deployment toevoegen",
                    remove_label="Deployment verwijderen",
                ),
            ],
        ),
        in_create_wizard=False,  # Edit only
        summary_fn=deployments_summary,
    )
```

## Providers Needed

### PullPolicyOptionsProvider

```python
class PullPolicyOptionsProvider:
    def get_options(self) -> list[dict[str, Any]]:
        return [
            {"value": "Always", "label": "Always"},
            {"value": "IfNotPresent", "label": "IfNotPresent"},
            {"value": "Never", "label": "Never"},
        ]
```

### BaseDomainOptionsProvider

```python
class BaseDomainOptionsProvider:
    def get_options(self) -> list[dict[str, Any]]:
        return [
            {"value": "", "label": "Standaard (clusternaam)"},
            {"value": "rijksapp.nl", "label": "rijksapp.nl"},
        ]
```

## Save Flow

Only editable fields are saved. Encrypted `configuration`, `clone-from`, `services`, and `namespace` are **preserved untouched** from the original YAML.

```
POST /projects/{name}/parts/deployments

1. Parse form data — cluster, subdomain, base-domain, issuer, repository, component mappings
2. For each deployment:
   - set_value for editable fields
   - Skip readonly fields: namespace, configuration, clone-from, services
3. Encrypted fields preserved bit-for-bit
4. Write YAML, commit to git
```

## Display Summary

```python
def deployments_summary(data: dict) -> str:
    deployments = get_value(data, "deployments") or []
    if not deployments:
        return "Geen deployments"
    clusters = {d.get("cluster", "") for d in deployments if isinstance(d, dict)}
    count = len(deployments)
    return f"{count} deployment{'s' if count != 1 else ''} op {', '.join(clusters)}"
```

## Acceptance Criteria

- [ ] Deployments render as collapsible cards in a sequence
- [ ] Editable fields (cluster, subdomain, base-domain, images) render with correct widget types
- [ ] Encrypted configuration renders as `display-card` with "Versleuteld opgeslagen"
- [ ] Clone-from renders as `display-card` with completion status
- [ ] Component reference select shows project component names (cross-part filtering)
- [ ] Repository select shows project repository names (cross-part filtering)
- [ ] Saving preserves encrypted fields exactly
- [ ] Add/remove deployments works with confirmation
- [ ] Part does not appear in create wizard
- [ ] Nested sequence path resolution works: `deployments[0]/components[1]/image`
