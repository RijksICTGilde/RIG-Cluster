# How the API documents itself

The OpenAPI document at `/openapi.json` (rendered at `/docs`) is what an API client reads
and what a generated client is built from. Three rules keep it readable; each one is held
by a test, because all three run back silently.

## One group per operation

Swagger UI groups on tag and shows an operation under **every** tag it carries. A second
tag is therefore not extra information, it is a second copy of the endpoint on the page.

Measured on 6 August 2026: 101 operations rendered as 245 lines. A service endpoint
carried four tags -- `v2` twice (once from the router, once from the route), `services`,
and the service's own name -- so the same upload occupied four places.

The rule: **an operation carries exactly one tag, the group it belongs to.**

- The version is not a group. `/api/v2/...` already says it, and a `v2` tag with 106
  members collects everything under one heading instead of grouping anything.
- A service endpoint is grouped under its service (`attachments`, `redis`), not also under
  a generic `services`.

`tests/test_openapi_grouping.py` asserts that rendered lines equal operations (101 = 101),
that no operation repeats a tag, and that no version tag exists.

## A request body has a name

Loose multipart form fields make FastAPI invent a body model named after the route's
unique id:

```
Body_create_attachments_component_api_v2_projects__project_name__services_attachments_component__component_name__attachments_post
```

Four of those, differing only in layer and verb, sat in the schema list next to
`AttachmentUse` and `AttachmentsConfig` and read as duplicates. The declared action routes
now build a named model instead -- `AttachmentsComponentCreateRequest` -- from the action,
the layer and the verb, which are the only things that distinguish them.

Same fields, same descriptions, same `multipart/form-data`. Only the name changed.

## An operation says what it does

A summary names the endpoint; a description tells the caller what they cannot guess. For
the generated per-service config endpoints that is:

- where the value lands -- the project's YAML file in `zad-projects`, and at which layer;
- that configuring at component or deployment level also selects the service at project
  level;
- that a change which reaches the file **is rolled out** (the project is processed again,
  manifests regenerated, ArgoCD applies) -- these are not save-only endpoints;
- that the response is 202 with a task id to poll at `/api/tasks/{task_id}`;
- for a clear: that clearing config which is not there changes nothing and is still a
  success.

`tests/test_openapi_grouping.py` fails on any service operation without a description, and
on a description that merely repeats its summary. It enumerates the operations from the
live spec, so an endpoint added later is covered without touching the test.

## Related

- `features/service-api-actions.md` -- declared actions, and the field rules (`combinations`,
  `disjunctions`) that reach the spec
- `features/service-config-api.md` -- the generated per-service config endpoints
