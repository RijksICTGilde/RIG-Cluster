# Service-owned blocks on the project-details page

A service owns its own blocks on the project-details page: the HTML, the data behind it,
who may see it, whether it shows at all, and the endpoints that fill it. The general
template loops over what the project's services deliver instead of naming services and
deriving conditions itself.

## Why

A block that lives in the general page fails silently. When RC-5 moved the Keycloak
config into the service, the realm block kept reading the old project-level location and
simply stopped rendering -- and nobody noticed, because an empty section looks exactly
like a project without Keycloak. Once the service owns the block, a move breaks where it
belongs, next to the data.

The same reasoning applies to visibility. The page used to carry lines like

```jinja
{% if "attachments" in (project.services | ... ) %}
```

which is service knowledge in the general layer, and it was applied inconsistently: the
backups block appeared for every project with a deployment, whether or not it had
anything to back up.

## The four hooks

| Hook | What it delivers | Collector |
|---|---|---|
| `@on(UIEvent.PROJECT_SECTIONS)`, payload `ProjectPageContext` | a block about the project | `collect_detail_page_sections()` |
| `@on(UIEvent.DEPLOYMENT_SECTIONS)`, payload `DeploymentPageContext` | a block about ONE deployment | `collect_deployment_page_sections()` |
| `definition.actions_provider(project_data, deployment_name)` | action buttons on a deployment | `collect_deployment_actions()` |
| `web_routers()` | the endpoints those blocks need | `collect_service_routers()` |

Only services the project actually uses (selected at project level or referenced by a
component) are asked for sections, so "does this block apply" needs no condition in the
template. A service that has nothing to show returns `[]`.

Both section events are UI events (RC-39): a service declares its handler with `@on(...)`
on the method that builds the block, and `registry.listeners(event)` is the only index of
who contributes -- see `features/service-event-hooks.md`. Both handlers return
`DetailPageSection(template=..., context=...)`. Templates live
next to the service (`opi/services/catalog/<svc>/`, addressed as `<svc>/<file>`) and read
their data from `section.context`.

`DeploymentPageContext` carries what a deployment block needs beyond the project dict:
the deployment, the managed cluster, and `backend_available` -- the availability of
optional back-ends (`prometheus`, `backups`) that the view already probed, because a
service must never call a connector itself.

## Who owns what today

| Block | Owner |
|---|---|
| Keycloak realm admin details | `keycloak` |
| Uitnodigingen | `invite` |
| Bijlagen | `attachments` |
| Resource Metrics (per deployment) | `metrics-scraper` |
| Backups (per deployment) + its `hx-get` fragment | every service with a `backup_label` |
| Databaseconsole + Job uitvoeren modals and their routes | both PostgreSQL services |

Header, team, components, repositories, ArgoCD status, environment variables, the Taken
table and the danger zone are genuinely general and stay in
`opi/templates/project-details/`.

## Blocks with more than one owner

Backups are not a service: a project can back something up exactly when it uses a service
that declares a `backup_label`. Such a block is delivered by each owner through a shared
mixin in `catalog/shared/`, and the collectors keep one copy:

- sections dedupe on template name,
- actions on (label, endpoint),
- routers on object identity -- so every owner must return the **same** router object.

Page mixins no longer have to cooperate through `super()` (RC-39): a service carries every
handler it inherits and the dispatch concatenates their contributions, so the PostgreSQL
services are backupable *and* bring the console/job modals without either mixin knowing
about the other. A mixin that forgot to chain used to swallow the other one's block.

## Endpoints travel with the block

The backups block lazy-loads its rows over `hx-get`; the console and the job runner are
modals with start/status/stop routes. Declaring them through `web_routers()` keeps them
with the service instead of leaving half the block behind in the general router. Import
the route module *inside* `web_routers()`: those modules import managers, which the
catalog itself must not do.

A modal button is a `DeploymentAction` with `modal_endpoint` + `modal_title` instead of
`endpoint`; the shared modal shell loads that URL (`openServiceModal` in
`project-details.html.j2`). Exactly one of the two, enforced in `__post_init__`.

## Adding a block to your service

1. Write the template in your service package and read `section.context`.
2. Put `@on(UIEvent.PROJECT_SECTIONS)` or `@on(UIEvent.DEPLOYMENT_SECTIONS)` on a method
   that takes the matching payload, returning `[]` when the block does not apply (role,
   missing data, wrong cluster). The method's name is free -- name it after the block.
3. Need an endpoint? Put the router in your package and return it from `web_routers()`.
4. Write both guards: the section **appears** for a project that uses the service and
   **stays away** from one that does not. A missing block is invisible otherwise; that is
   the regression these tests exist for (`tests/test_service_detail_sections.py`,
   `tests/test_service_deployment_sections.py`).
