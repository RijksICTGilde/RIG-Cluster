# Service API actions: define, use, bind

A platform service exposes its configuration through generated REST endpoints (see
`service-config-api.md`). That covers "configure this service here" and is generated from
the fields the wizard already declares. It could not cover everything a service can do,
and attachments showed exactly where it broke: an API client could point a component at
attachment `my-cert` while having **no way to put `my-cert` in the project**, because an
attachment's content is a file, it arrives as multipart, and it is not a config block.

Two things close that: a vocabulary for what config at a layer *is*, and a way for a
service to declare actions of its own.

## The three kinds of config

`ConfigRole` (`opi/services/catalog/base.py`), answered per layer by
`Service.config_roles(layer)`:

| Role | Means | Where it lives in the project file |
|---|---|---|
| `DEFINE` | put something into the project that nothing uses yet | `data` on the project-level service entry |
| `USE` | this component/deployment uses this service, this thing | `config` |
| `BIND` | how the used thing reaches the workload | `config`, next to the use |

The default for the whole catalog is `USE` on every layer a service carries config on:
there is nothing to define, and the binding is implied by the service. Attachments is the
first service where they come apart:

```yaml
services:
  - attachments:
      data:                       # DEFINE: the catalog
        - id: server-cert
          filename: server.pem
          content: |
            -----BEGIN AGE ENCRYPTED FILE-----
            ...

components:
  - name: backend
    services:
      - attachments:
          config:                 # USE + BIND
            - reference: server-cert
              provide-as: file
              path: /etc/ssl/certs/server.pem
```

A DEFINE layer has its own model, `data_model_for(layer)`, and `validate_service_configs`
validates the `data` block against it. That is new: the catalog previously sat under
`data`, the validation walk only looked at `config`, and the shape was guarded by nothing.

## Declared actions

`Service.api_actions()` returns `ServiceAction` declarations
(`opi/services/catalog/actions.py`); attachments keeps its in
`opi/services/catalog/attachments/api.py`, next to its model and its editables.

One declaration carries everything the endpoint needs:

| Part | What it says |
|---|---|
| `layer` + `roles` | where it acts and what it does there |
| `fields` | each field's name, meaning (into the OpenAPI document), the shared `Editable` that validates it, whether it is a text field or an upload, and which verbs require it |
| `verbs` | which of create / update / upsert it supports |
| `combinations` | which fields go together, plus a dotted path to where that rule is *already* enforced |
| `disjunctions` | which fields are an either/or ("send A or B"), plus that same dotted path |
| `example` | a curl line that works |
| `handler` | the async function that does the work |

Route, multipart signature, per-field documentation and the OpenAPI description are
generated from it (`_register_service_action_routes` in `opi/api/v2/router.py`). Nothing
in the router names a service.

### Fields reuse editables

A field points at the shared `Editable` the wizard renders and never restates its rule --
the same move `opi/api/validation.py` made for the component endpoints. Only "may this be
left out" is the endpoint's business, and `editables_for(verb)` sets exactly that.

A field with genuinely no editable states why (`no_editable_reason`). There is one today:
the uploaded file. Its bytes are not a form field; the only rule that applies is its size,
and that lives once in `catalog/attachments/catalog_model.MAX_ATTACHMENT_BYTES`.

### Combinations and disjunctions

Two shapes of rule, both **documentation with a pointer** and neither of them a second
implementation:

| Declaration | Says | Example |
|---|---|---|
| `FieldCombination` | *if* this, *then* that is required | when `provide-as=file`, `path` is required |
| `FieldDisjunction` | exactly one of these is given | `file` **or** `reference` |

An implication cannot express an either/or: written from both sides it is two rules, and
neither of them says that giving both is wrong or that giving neither is. So the
disjunction is its own declaration -- and it reaches the OpenAPI document as `oneOf`, so a
client reads the rule off the spec instead of discovering it at the 422.

`enforced_by` is a dotted path to the code that actually refuses the request, and
`tests/test_service_actions.py` resolves every one of them. The declaration itself
validates nothing: a rule that lived in both places would drift, which is the whole reason
it points instead of repeats.

### The verbs

| Verb | HTTP | Id already exists | Id absent |
|---|---|---|---|
| create | `POST` | 409, refuse | create (201) |
| update | `PUT` | replace (200) | 404, refuse |
| upsert | `PUT ?upsert=true` | replace, without asking | create |

Replacing on id without warning is only ever what an upsert does, and the caller has to
ask for it. A `POST` that quietly overwrote would lie about what it did, and the owner
would find out when the old file was gone. Whether replacing was intended is the caller's
business -- which is why the caller states it.

## The attachment endpoints

```
POST /api/v2/projects/{project}/services/attachments/attachment
PUT  /api/v2/projects/{project}/services/attachments/attachment/{attachment_id}[?upsert=true]

POST /api/v2/projects/{project}/services/attachments/component/{component}/attachments
PUT  /api/v2/projects/{project}/services/attachments/component/{component}/attachments/{attachment_id}[?upsert=true]
```

All four are multipart and authenticate with the project's `X-API-Key`.

Define only:

```bash
curl -X POST -H "X-API-Key: $KEY" \
  -F attachment_id=server-cert -F file=@server.pem \
  https://zad.rijksapps.nl/api/v2/projects/my-project/services/attachments/attachment
```

Define, use and bind in one request:

```bash
curl -X POST -H "X-API-Key: $KEY" \
  -F attachment_id=server-cert -F file=@server.pem \
  -F provide-as=file -F path=/etc/ssl/certs/server.pem \
  https://zad.rijksapps.nl/api/v2/projects/my-project/services/attachments/component/backend/attachment
```

as an environment variable instead:

```bash
curl -X POST -H "X-API-Key: $KEY" \
  -F attachment_id=api-token -F file=@token.txt \
  -F provide-as=env-var -F env-name=API_TOKEN \
  https://zad.rijksapps.nl/api/v2/projects/my-project/services/attachments/component/backend/attachment
```

Couple an attachment that is **already** in the catalog, without uploading anything (this
is the `reference` side of the disjunction -- `file` or `reference`, never both and never
neither):

```bash
curl -X POST -H "X-API-Key: $KEY" \
  -F reference=server-cert \
  -F provide-as=file -F path=/etc/ssl/certs/server.pem \
  https://zad.rijksapps.nl/api/v2/projects/my-project/services/attachments/component/backend/attachment
```

On a `PUT` the path already names the attachment, so there is no choice to make: a `file`
replaces its content, and leaving it out rewrites only the coupling.

Replace an existing attachment, keeping its couplings:

```bash
curl -X PUT -H "X-API-Key: $KEY" -F file=@server-2027.pem \
  https://zad.rijksapps.nl/api/v2/projects/my-project/services/attachments/attachment/server-cert
```

Responses: `201` created, `200` replaced, `409` id taken (or the project has no encryption
key), `404` unknown attachment or component, `413` file too large, `422` a field or field
combination the rules refuse.

The coupling fields follow the component config exactly: `provide-as: file` needs `path`,
`provide-as: env-var` needs `env-name`. That rule lives in `AttachmentUse` and is run here
so the caller hears it at once. The "content or reference" rule lives once too, in
`check_attachment_source` in the service's `api.py`, and both the disjunction and the
`file` -> `attachment_id` combination point at it.

## The 64 KB limit

`MAX_ATTACHMENT_BYTES` in `opi/services/catalog/attachments/catalog_model.py`, with the
reason next to it. Attachments are meant for small files -- a certificate, a key -- and
each one is stored inline in the project YAML, so every upload makes that file
permanently bigger for everyone who reads it.

It is a choice, not a law of nature: it is low because the project file has no import
mechanism yet. If attachments ever move to their own files, raising it is a decision
someone makes at that constant, not something they discover.

It holds on **every** road in: the API upload (413) and the wizard upload (an error in the
form). A limit only one road honours just moves the problem to the other one.

## Adding an action to another service

1. Write `api.py` in the service's package.
2. Declare the fields, pointing each at the shared editable that already validates it.
3. Pick the verbs; the id semantics come with them.
4. Write the handler: it gets an `ActionContext` and returns an `ActionResult` (status +
   body), so a service never imports the web framework to say "no".
5. Return the declarations from `api_actions()`.

`tests/test_service_actions.py` holds every declaration to being honest: a field reuses an
editable or writes down why it cannot, a combination and a disjunction point at code that
resolves, and every action carries an example.

## Related

- `features/service-config-api.md` -- the generated config endpoints
- `features/attachments-getting-started.md` -- attachments from the portal
- `instructions/services.md` -- the service contract, config layers and roles
