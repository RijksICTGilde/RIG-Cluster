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
  TLS-modus editable (commits c1edb128, 2de5f381). Per-deployment attachment override
  exists (the cert for passthrough).
- **Schema-locked (this note)**: the full 3-mode, 3-level cascade model + `provided`.
- **Next**: the resolution helper (deployment > component > root), wiring root +
  deployment-override into manifest generation, the `provided` mode (PEM -> tls
  Secret), and the wizard options (mode select at the three levels + a cert-attachment
  picker shown for `provided`).
- **Deferred**: a MetalLB `type: LoadBalancer` service for FSC raw-TCP / port 8443.
