# API Editable Validation

## What it is

Reuses the editable validation system (`opi/forms/editables/`) for API endpoint input validation. This ensures that API endpoints enforce the same business rules (field patterns, allowed values, data conversion) as the web form pipeline.

## How it works

### Data-only Pipeline (`opi/forms/editables/pipeline.py`)

Operates on raw `Editable` objects without any UI dependency:

- **`validate_field(editable, value)`** - runs required check + validator for a single field
- **`validate_fields(field_map, editables)`** - validates a dict of `{field_name: value}` against matching editables
- **`convert_fields(field_map, editables)`** - applies `converter.write()` to each value
- **`enforce_rules(data, enforcers, context)`** - runs business rule enforcers

### API Validation Profiles (`opi/api/validation.py`)

Maps API operations to their relevant editables:

| Profile | Fields Validated |
|---------|-----------------|
| `ADD_COMPONENT_VALIDATORS` | name, image, path, cpu_limit, memory_limit, env_vars |
| `ADD_COMPONENT_TO_DEPLOYMENT_VALIDATORS` | component_name, image |
| `UPDATE_IMAGE_VALIDATORS` | newImageUrl |
| `UPSERT_DEPLOYMENT_VALIDATORS` | deploymentName |

The `validate_api_payload()` helper validates a Pydantic model dict and raises `HTTPException(422)` with structured field errors on failure.

### Error Response Format

```json
{
  "detail": {
    "field_errors": {
      "name": ["Moet beginnen met een kleine letter en mag alleen kleine letters en cijfers bevatten"],
      "image": ["Container image moet volledig in kleine letters zijn"]
    },
    "errors": ["Business rule violated"]
  }
}
```

## Integrated Endpoints

Validation is applied in both v1 (`/api/`) and v2 (`/api/v2/`) routers:

- `POST /projects/{name}/components` - add_component
- `POST /projects/{name}/deployments/{dep}/components` - add_component_to_deployment
- `POST /projects/{name}/:upsert-deployment` - upsert_deployment
- `PUT /projects/{name}/deployments/{dep}/image` - update_image

## What it validates that Pydantic alone does not

| Field | Pydantic | Editable Validator |
|-------|----------|-------------------|
| Component name | max_length=63 | Max 12 chars, lowercase letters + digits only, starts with letter |
| Image URL | max_length=512 | Must be fully lowercase, no spaces |
| Path | max_length=256 | Must start with `/`, no spaces |
| CPU limit | max_length=16 | Must be one of `500m`, `1` |
| Memory limit | max_length=16 | Must be one of `512Mi`, `768Mi`, `1Gi` |
| Deployment name | max_length=63 | Must be valid slug format |
| Env vars | max_length=65536 | Must be valid `KEY=value` format |
