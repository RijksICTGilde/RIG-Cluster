# Component-level image field: clarify purpose

## Status: TODO - needs decision

## Problem

Top-level component definitions (`components[*]`) have an `image` field, but its purpose is unclear.
In production project files (e.g. `regel-k4c.yaml`), most components have `image: ''`, but at least one
(`enrichworker`) has a real image URL (`ghcr.io/minbzk/regelrecht-enrich-worker:latest`).

The actual image used for manifest generation is always read from **deployment component references**
(`deployments[*]/components[*]/image`), never from the top-level component. So the top-level `image`
field is currently dead data at runtime.

## Where it lives in code

- **Editable field definition**: `opi/forms/editables/fields/components.py` — `COMPONENT_IMAGE_EDITABLE`
- **Self-service form model**: `opi/forms/models/project.py` — `ComponentFormModel.image` (default: `nginx:latest`)
- **Wizard propagation**: `opi/forms/visualizers/wizard_sections.py` — reads `component.get("image")` and copies it to the deployment ref when distributing a component to deployments
- **Manifest generation**: `opi/manager/project_manager.py` — only reads image from deployment component refs, ignores top-level

## Current behavior

1. Self-service form collects an image per component via `ComponentFormModel`
2. On submit, the image is used to create deployment component refs (`project_utils.py`)
3. The wizard also reads `components[*]/image` when distributing a new component to deployments
4. Manifest generation only uses `deployments[*]/components[*]/image`

The top-level `image` field is written but never consumed for actual deployment.

## Possible intentions

1. **Default image**: The component-level image could serve as a default/starting image that gets copied
   into new deployment refs. This would be useful for components that always start from the same base
   image (e.g. a shared worker image), so users don't have to re-enter it per deployment.

2. **Form-only transient field**: It was only meant to exist during form submission and shouldn't be
   persisted to the YAML at all. The fact that it shows up in project files is a bug.

3. **Planned feature**: There may have been a plan to use the component-level image as a fallback in
   manifest generation when a deployment ref has no image set.

## Decision needed

- Should `components[*]/image` be a **default image** that new deployment refs inherit? If so, formalize
  this: document it, add fallback logic in manifest generation, and make it editable in the UI.
- Or should it be **removed** from top-level components entirely? If so, strip it from project files,
  remove `COMPONENT_IMAGE_EDITABLE`, and clean up the wizard propagation code.
- Or is the current behavior correct (form transient only), and we just need to ensure it doesn't get
  persisted to YAML?
