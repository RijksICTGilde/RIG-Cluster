# Publish-on-web: TLS modes (certificate handling)

How a published component's TLS certificate is handled. Three modes, configurable at
three levels with a cascade. Locked at the YAML-schema level; implementation is
incremental (see Status).

## Modes

- **`standard`** (default): the platform issues the certificate (cert-manager /
  Let's Encrypt) for the component's web address. No extra config.
- **`passthrough`**: the ingress passes TLS through untouched; the pod presents its
  own certificate. The ingress "does nothing" beyond passthrough. The certificate is
  the customer's, delivered to the pod **separately** as an attachment coupling
  (mounted at a path), not referenced here. Routing is normal: ingress -> Service ->
  any replica, all replicas mount the same cert. Used for FSC/mTLS (identity is
  cert-based, not domain-based).
- **`provided`**: the customer's own certificate, terminated **on the ingress** (e.g.
  a wildcard `*.mijndomein.ext`). Reference the catalog attachment (a PEM with cert +
  key) via `attachment`; the platform splits it into a `kubernetes.io/tls` Secret and
  the ingress uses it. No cert-manager issuance.

## Config shape

Lives in the `publish-on-web` service entry's `config`:

```yaml
publish-on-web:
  config:
    tls: standard | passthrough | provided   # default: standard
    attachment: <attachment-id>              # required iff tls: provided
```

`attachment` references an id in the project attachments catalog
(`services: - attachments: data: [{id, ...}]`), the same way couplings reference one.
`attachment` is only used by `provided`. For `passthrough` the customer couples the
cert themselves (a separate `attachments` coupling with a mount `path`); this is a bit
double today, but the wizard may later show both together.

## Three levels + cascade

The same config can appear at, in increasing precedence:

1. **Root** (project services) - global default for all components:
   ```yaml
   services:
     - publish-on-web:
         config: { tls: standard }
   ```
2. **Per component** (component services) - overrides root:
   ```yaml
   components:
     - name: api
       services:
         - publish-on-web:
             config: { tls: provided, attachment: wildcardcert }
   ```
3. **Per component per deployment** (override) - overrides component. Under the
   deployment component's `services` map (which is keyed by service name; the override
   sits next to the system revision-map entries like `persistent-storage`):
   ```yaml
   deployments:
     - name: prod
       components:
         - reference: api
           services:
             publish-on-web:
               config: { tls: provided, attachment: prodwildcard }
   ```
   The per-deployment attachment coupling lives the same way, under
   `services.attachments.config`.

**Resolution: deployment-component > component > root > built-in `standard`.**

The winning level supplies the **whole** block, mode *and* certificate, not a merge of
the levels. Two consequences worth knowing before you set an override:

- an override can switch `provided` **off**: `tls: standard` on a deployment component
  really does put that deployment back on the platform certificate, even when the
  component supplies its own;
- an override to `provided` must name its **own** `attachment`; the component's is not
  inherited along with a mode the override replaced. The model refuses `provided`
  without one.

Leaving the override empty is not "no TLS" -- it means "follow the component", and it is
also how you remove an override: emptying the field deletes the whole `publish-on-web`
key from the deployment component rather than storing an empty value.

The per-deployment variation of the *certificate itself* (which file) is handled by
the attachment (base + per-deployment attachment override), not by the mode.

## Constraint (passthrough)

Passthrough routes by host (the path is encrypted), so a component's address must
resolve to its own host: use a domain-format that includes the component, or have a
single published component. Components sharing a host by path cannot each be
passthrough.

## Schema

`$defs.publish-on-web-config` in `opi/schemas/project_v2.json` (tls enum + attachment,
`attachment` required when `tls: provided`). Referenced by the deployment-component's
`services.publish-on-web.config` (a named property on the deployment `services` map,
alongside `services.attachments.config`); at root/component it rides the generic
service-entry and is validated in Python.

## Where you set it (per deployment)

Two places, one definition. The fields are declared once by the service
(`opi/services/catalog/publish_on_web/editables.py`, layer `DEPLOYMENT_COMPONENT`) and
every form that shows the deployment-component layer picks them up from the registry
hook (`deployment_component_service_visualizers()`):

- **Deployment bewerken** (`modal-edit-deployment-<n>`): per component, in the fieldset
  "Certificaat (alleen voor deze deployment)", next to image and environment variables.
- **Webadres bewerken** (`modal-edit-domain-<n>`), step 2 "Certificaten per component":
  the same two fields on a read-only component list.

The TLS select leads with an inherit option that **names the mode it would fall back
to** ("Erven van het component: standaard certificaat ..."), so an empty field reads as
an inheritance rather than as an absence.

Adding a deployment-component field for another service needs no change to any form: the
service declares `config_editables(ConfigLayer.DEPLOYMENT_COMPONENT)`,
`config_deployment_component_visualizers()` and `config_deployment_component_layout()`,
and the deployment form gathers it.

## Status

- **Done**: `passthrough` at the component level + cert suppression + the inline
  TLS-modus editable (c1edb128, 2de5f381). Schema for all 3 modes + 3-level cascade
  (d605ed24). Resolution cascade (deployment > component > root > standard) wired into
  ingress generation; the per-deployment override wizard step (modal-edit-domain step
  2, read-only component list, `provided` + cert-attachment picker); the component-level
  select gained `provided` + its picker; `add_remove=False` on sequences (601face0).
- **Done (`provided`)**: the referenced PEM attachment is AGE-decrypted, split into a
  cert chain + key, and rendered as a SOPS-encrypted `kubernetes.io/tls` Secret; the
  ingress uses it via `tls.secretName` with cert-manager suppressed. PEM validation
  (>=1 certificate + exactly one private key). Reuses the binary-secret template
  (`secret_k8s_type`).
- **Done (RC-78)**: the per-deployment override is declared by the service at the
  `DEPLOYMENT_COMPONENT` layer, so it also appears in **Deployment bewerken** and not only
  in the domain wizard's certificate step; the inherit option names the inherited mode.
  The forms layer no longer defines these fields itself.
- **Next / derivable now**: the same per-component step in the Create wizard (after the
  web address step), writing the component-level definition.
- **Deferred**: a MetalLB `type: LoadBalancer` service for FSC raw-TCP / port 8443.
