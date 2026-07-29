# Service Vertical Slice — approval + persistence owned by the service

Status: Design / inventory (2026-07-25). Not implemented. Extends
`features/futures/service-owned-config-fields.md` and the delivered
`features/service-provider-registry.md`. Captures two further concerns a
self-contained service should own — **approval** and **persistence** — surfaced while
migrating publish-on-web.

## The idea

We already drive config UI, provisioning, cleanup and manifest generation off the
service catalog (each `Service` in `opi/services/catalog/<service>.py` declares its
contribution; generic code iterates). The same "the service owns it, generic code
drives it" pattern extends to two more concerns:

| Concern | Mechanism |
|---|---|
| config UI | `config_editables` / `config_form_section` / `config_component_layout` (built) |
| provisioning / cleanup / manifest | `provision` / `handle_service_removal` / `contribute_manifest_context` (built) |
| **approval** | `ApprovalSpec` per service + one generic approval interface (this doc) |
| **persistence** | service-owned SQLAlchemy model + repository; migration auto-generated centrally (this doc) |

## 1. Approval as a generic service capability

The domain-approval subsystem today (`router_subdomain_admin`, the enforcer's
request/warn/deny gating, the root `domains:` state) is really an instance of a
generic pattern: **approve one-or-more fields of part of a dict, via an interface.**
It will recur — e.g. a future service granting cross-namespace access that a project
admin/member approves.

Generalize it:
- A service declares approval requirements: `ApprovalSpec { field_path(s),
  approver_role: PLATFORM_ADMIN | PROJECT_ADMIN | PROJECT_MEMBER, scope }`.
- Approval **state** (requested -> approved/denied + history) lives with the
  field/service, not as a root special-case.
- One **generic approval interface** (router/flow) iterates the catalog's
  `ApprovalSpec`s + the stored state -> lists pending items for the right approver,
  records the verdict. Replaces the domain-specific `router_subdomain_admin` with a
  catalog-driven generic one.
- The approver **scope** is what makes it reusable: domains = PLATFORM_ADMIN
  (cross-project); cross-namespace access = PROJECT_ADMIN/MEMBER (within a project).

### Delivered — the declare + check slice (grounded in domains)

The first, load-bearing slice is built (`opi/services/catalog/approval.py`), so the
shape is concrete rather than speculative. It answers the two questions a service asks:

1. **"dit heeft approval nodig" (the DEFINITION)** — a service returns `ApprovalSpec`s
   from `config_approvals(layer)`. An `ApprovalSpec` is pure data plus one rule callback
   (`key`, `label`, `approver: ApproverScope`, `status_of`). publish-on-web declares
   `domain` + `subdomain` at the `DEPLOYMENT` layer, both `PLATFORM_ADMIN`.
2. **"is dit veld approved?" (the CHECK)** — `spec.status(project_data, value) ->
   ApprovalStatus` (`NONE | REQUESTED | APPROVED | DENIED`), with `is_approved(...)` as
   the gating shortcut. The `value` is opaque to the generic layer (a domain string; a
   `(domain, subdomain)` pair) — its shape is the service's business.

`Service.get_approval(key)` looks a spec up across layers. `status_of` **reuses the
existing pure predicates** in `connectors/subdomain.py`
(`get_project_allowed_domain_config`, `get_subdomain_status`) — no domain rules were
duplicated or moved. The state still lives in the root `domains:` block; the spec just
reads it where it is. `ApprovalStatus`' three non-`NONE` values equal the persisted
status strings, so a stored verdict maps straight onto the enum.

### Delivered — the generic, catalog-driven approver interface

The router no longer hard-codes the domains subsystem. `ApprovalSpec` gained two more
callbacks alongside `status_of`:
- `list_items(project_data) -> list[ApprovalItem]` (LIST) — enumerate the approvable
  items this spec currently has in a project.
- `record(project_data, item, history_entry)` (RECORD) — persist one approver verdict.

publish-on-web owns the domain/subdomain LIST + RECORD logic (moved out of the router
and the wizard section). `opi/services/approvals.py` is the generic layer:
`collect_approval_items` iterates `approval_services()` and tags each item with its
owning `service` + spec `key`; `apply_approval_verdicts` builds the uniform history
entry once and routes each decided item back to the owning spec's `record` (falling
back to spec-key routing for an untagged in-flight item). `router_subdomain_admin` and
the approval section's `post_merge` now just call these — no domain/subdomain switch
left in generic code. The approval editables/visualizers (`fields/approval.py`) are the
UI wire contract; the hidden `service` field was added to `approval_items.html.j2` so
the owner tag survives the form round-trip. `Service.approval_specs()` +
`registry.approval_services()` are the catalog-wide entry points.

Still **future** (deliberately not yet, each is real work with blast radius): moving the
approval **state** from root `domains:` under publish-on-web (schema + data migration —
the next step, to be verified against the sandbox cluster); the **persistence**
ownership below. The `router_subdomain_admin` module + URL prefix keep the domain name
for now; renaming it to a generic `/admin/approvals` is cosmetic and deferred.

## 2. Domains are misplaced at root — decompose into three concerns

`domains: {allowed-domains, allowed-subdomains}` at the project root
(`project_v2.json`) conflates three things that belong in different places:

| Currently at root/domain | Belongs to |
|---|---|
| approval **state** (`allowed-domains/subdomains`, requested/approved/denied) | **publish-on-web** service (via the generic approval capability) |
| global subdomain **registry** (`connectors/subdomain.py`, DB table unique across ALL projects) | **publish-on-web** service *logic* (see persistence below) — the DATA is cross-project, the ownership/logic is the service's |
| admin approve/deny **UI** (`router_subdomain_admin`) | the **generic approval interface** |

Moving the approval state from root to under the publish-on-web service is a
schema + data migration (touches the enforcer, the approval predicates in
`connectors/subdomain.py:92-422`, `router_subdomain_admin`, and existing project
files) — real work, not trivial.

## 3. A service owns its persistence (incl. its table)

A service that needs a database table should own that concern too — the subdomain
registry (`connectors/subdomain.py`, table `subdomain_registry`) is publish-on-web's,
even though its rows span all projects (global uniqueness is exactly the point).

Clean shape (avoids the "weird per-service migration script" problem):
- The service owns the **schema as code**: its SQLAlchemy model (table definition)
  lives in the service module.
- Alembic **auto-generates the migration centrally** from the union of all
  service-owned models (`alembic revision --autogenerate` reads the metadata). So the
  table *definition* is service-owned; the migration *mechanism* stays central and
  ordered. No per-service migration files.
- The service owns the **repository/connector logic** on top (queries, register/
  reserve, uniqueness enforcement, reserved-name policy).

## Why this fits

It is the same open-closed, catalog-driven principle as the rest of RC-5: the generic
code (registry loops, a generic approval router, Alembic autogenerate over collected
models) iterates the catalog; each service supplies its declarations + logic. The end
state is a true vertical slice: one module per service owning config + provisioning +
cleanup + manifest + approval + persistence.

## Sequencing (updated 2026-07-25)

The generic **approval** capability is now a candidate to build *soon*, grounded in the
single real case (domains) rather than speculatively: refactor the domain approval into
the first `ApprovalSpec` + a catalog-driven approval interface, which also naturally
does the "domain approval state -> publish-on-web" and "subdomain-registry logic ->
publish-on-web" moves. **Persistence** ownership rides along (the service owns the
SQLAlchemy model; Alembic autogenerates the migration centrally).

Remaining **config** migrations are quick and finish the config-ownership goal:
attachments (a component-uses Sequence like storage + a project "Bijlagen" section with
the existing upload partial via `config_form_section(PROJECT)` — NOT a hard case) and
keycloak (nested additional-clients/realm-roles stay hand-authored on the service).

## Related

- `features/futures/service-owned-config-fields.md` — the config-field ownership +
  the publish-on-web config boundary this builds on.
- `features/service-provider-registry.md` — the delivered catalog / generic-dispatch
  foundation.
