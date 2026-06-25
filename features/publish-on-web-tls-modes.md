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
3. **Per component per deployment** (override) - overrides component. A dedicated
   `publish-on-web` key on the deployment component (its `services` is the
   system service-revision map, so it cannot be a services-list entry there):
   ```yaml
   deployments:
     - name: prod
       components:
         - reference: api
           publish-on-web:
             config: { tls: provided, attachment: prodwildcard }
   ```

**Resolution: deployment-component > component > root > built-in `standard`.**

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
`publish-on-web.config`; at root/component it rides the generic service-entry and is
validated in Python.

## Status

- **Done**: `passthrough` at the component level + cert suppression + the inline
  TLS-modus editable (c1edb128, 2de5f381). Schema for all 3 modes + 3-level cascade
  (d605ed24). Resolution cascade (deployment > component > root > standard) wired into
  ingress generation; the per-deployment override wizard step (modal-edit-domain step
  2, read-only component list, `provided` + cert-attachment picker); the component-level
  select gained `provided` + its picker; `add_remove=False` on sequences (601face0).
- **Next**: the `provided` manifest, take a referenced PEM attachment, split into a
  `kubernetes.io/tls` Secret, and have the ingress use it (`tls.secretName`) with
  cert-manager suppressed. Until then the wizard saves `provided` but the manifest
  treats it as standard.
- **Also derivable now**: the same per-component step in the Create wizard (after the
  web address step), writing the component-level definition.
- **Deferred**: a MetalLB `type: LoadBalancer` service for FSC raw-TCP / port 8443.
