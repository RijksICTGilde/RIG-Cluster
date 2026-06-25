# Publish-on-web: passthrough TLS

Lets a published component present **its own TLS certificate end-to-end** instead of
the platform issuing one. The ingress passes the encrypted stream through untouched
and the pod terminates TLS itself. This is the network half of FSC/mTLS, where the
peer must see the pod's own (e.g. PKIoverheid) certificate, not a platform cert.

## How to use

On a component that has the `publish-on-web` service, set the TLS mode in the wizard
("Publicatie op het web" -> TLS-modus), or in the project file:

```yaml
components:
  - name: api
    services:
      - publish-on-web:
          config:
            tls: passthrough        # default: standard
      - attachments:
          config:
            - reference: fsccert
              provide-as: file
              path: /etc/tls/server.pem
```

- `standard` (default): the platform issues the certificate (cert-manager / Let's
  Encrypt). Unchanged behaviour.
- `passthrough`: no platform certificate is issued for this ingress; the pod presents
  its own. Supply that certificate to the pod as an **attachment** (mounted file) and
  have the app terminate TLS on its published port.

## What it generates

For a `passthrough` component's ingress:
- `route.openshift.io/termination: passthrough` (OpenShift Router / ODCN) and
  `nginx.ingress.kubernetes.io/ssl-passthrough: "true"` (nginx / sandbox-local).
- **No** `cert-manager.io/*` annotation and **no** `spec.tls[].secretName`, so no
  certificate is requested. The `spec.tls` stanza stays present (signals TLS) as
  `- {}`. Hostname and external-dns wiring are unchanged.

## Constraint (current)

Passthrough terminates TLS at the pod, so the router cannot route by path (the path
is encrypted) and the whole hostname goes to one backend pod. It therefore only works
when this component has its **own hostname**, i.e. a domain-format that includes the
component (so each component gets a distinct host -> its own ingress), or when it is
the only published component. Components that merely differ by path on a shared
hostname cannot each be passthrough. A richer per-component web-address model may lift
this later.

## Dependencies / related

- The certificate itself is the customer's; the platform does not manage it. Deliver
  it via the [attachments feature](./fsc-mtls-attachments.md) mounted into the pod.
- The pod's published port must be the TLS-listening port.
- No ODCN ip_whitelist change or extra NetworkPolicy is needed: OPI ingresses are
  already fully whitelisted and edge publishing already works.

## Deferred

- "Own certificate on the ingress" mode (upload a PEM -> `kubernetes.io/tls` Secret,
  edge-terminated with the customer's cert).
- A dedicated MetalLB `type: LoadBalancer` service for raw-TCP / FSC management port
  8443 (separate from this 443 ingress).
