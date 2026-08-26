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

### `provided` needs a certificate to exist first (RC-132)

`provided` terminates a PEM you supply as an attachment, so a project with an **empty
attachment catalogue** has no value that can satisfy it: the model refuses `tls:
provided` without an `attachment`, and the attachment picker next to it offers only
"Geen bijlagen geüpload". Choosing the mode there was a screen you could not save and
could not repair.

The mode is therefore offered **disabled, with the reason in its label** ("Eigen
certificaat op de ingress - upload eerst een certificaat bij Bijlagen") while the
project has no attachments -- not removed, because an option that quietly disappears is
its own puzzle for whoever comes looking for it. One helper
(`providers.publish_tls_mode_options`) does this for the component select and the
per-deployment override alike, so neither layer is a way round the other. Upload a
certificate on the Bijlagen section and the option is a normal choice again.

This is a form gate, not the security boundary: the model keeps refusing the
combination, so an API call or a crafted post is rejected exactly as before -- now with
a message that says what to do ("kies 'Standaard certificaat', of upload eerst een
certificaat bij Bijlagen en kies die hier") instead of raw pydantic output.

Adding a deployment-component field for another service needs no change to any form: the
service declares `config_editables(ConfigLayer.DEPLOYMENT_COMPONENT)`,
`config_deployment_component_visualizers()` and `config_deployment_component_layout()`,
and the deployment form gathers it.

## Measured end to end (RC-96)

The per-deployment override was walked on the sandbox on 13 August 2026 with the
certificate established **on the connection** (`openssl s_client` with SNI), not read out
of the project file: see `docs/doorloop-tls-override-2026-08-13.md` and the guardrail
`tests/e2e/test_sandbox_tls_override.py`. Empty really does inherit, one deployment serves
its own certificate while the other stays on the platform's, an override switches
`provided` off on a running deployment, a certificate an override uses cannot be deleted
(not even with `confirm_in_use`), and a reprocess re-emits the same certificate.

Two things to know before measuring it again:

- **There is no API route for this layer.** The generic config routes are generated from
  `_CONFIG_WRITE_LAYERS` (project, component, deployment), so the override is a UI-only
  setting today; `GET /api/v2/services/publish-on-web` states this itself with
  `config_endpoint: null` for `deployment-component`. A CLI cannot set it.
- **Measure on the ingress port.** On the shared dev server Caddy owns 443 and Kind
  publishes the ingress on 8843; Caddy terminates TLS with the same wildcard, so a
  handshake on 443 shows the platform certificate for every host, including one that
  demonstrably serves its own.

## Wanneer `standard` niets oplevert: een eigen domein op een cluster zonder uitgifte

`standard` betekent "het platform regelt het certificaat", en dat lukt niet overal. Voor de
domeinen die het cluster zelf aanbiedt (`nice_url.supported_domains`) is er een
platformcertificaat en is er niets te regelen. Voor een **eigen domein** moet cert-manager
er een halen, via een ACME HTTP-01-uitdaging die van buiten bereikbaar moet zijn -- en dat
kan niet op elk cluster.

In de sandbox kan het niet, en het is er ook niet aan te zien. `task sandbox:setup` zet een
**nep** cert-manager-CRD neer (`infrastructure/bootstrap/infrastructure/cert-manager/fake-crd/overlays/sandboxed-local`) zonder
controller erachter, zodat de Issuer-manifesten gewoon toegepast kunnen worden. Gevolg: de
Issuer wordt aangemaakt, meldt `Ready` (de nep-CRD zet die conditie als default), ArgoCD
meldt Synced, de deployment meldt Healthy -- en er wordt nooit een certificaat uitgegeven.
Er blijft ook geen Certificate hangen om naar te kijken, want die CRD bestaat er niet eens.
De bezoeker krijgt het fallback-certificaat van de ingress en dus een certificaatfout.

Dat valt bovendien precies samen met het moment dat de goedkeuringsmelding verdwijnt:
zolang het domein niet is goedgekeurd publiceert de deployment op het clusteradres
(`apply_domain_approval_fallback`) en is er niets mis. Het gaat pas mis nadat een beheerder
het domein heeft goedgekeurd, en dan staat het niet meer in `approvals`.

### Hoe het cluster dit zegt

Een cluster verklaart het met `supports_custom_domain_certificates` in `CLUSTER_CONFIG`:

| Cluster | Waarde | Waarom |
|---|---|---|
| `odcn-production` | `True` | Bereikbaar vanaf internet, echte cert-manager |
| `sandboxed-local` | `False` | Kind, niet bereikbaar, nep cert-manager-CRD zonder controller |
| `local` | *afwezig* | Draait een eigen CA (`kind-ca-issuer`); of die ook een eigen domein tekent is niet nagemeten |

Afwezig betekent `True`, dus zwijgen. Een waarschuwing over een cluster waarvan het
platform het niet weet, is een gok over andermans cluster.

### Waar je het te horen krijgt

De zin staat één keer, bij de dienst die zowel het `base-domain` als de `tls`-modus bezit
(`custom_domain_certificate_note` in `catalog/publish_on_web/domain_config.py`), en komt op
vier plekken terug:

1. **Bij het zetten in het formulier** -- `DomainConfigEnforcer` geeft een veldwaarschuwing
   op het domeinveld. Staat het domein óók nog op goedkeuring, dan zitten beide hindernissen
   in één melding: er kan maar één waarschuwing uit de enforcer komen, en de tweede horen
   nadat de eerste is opgelost is precies het probleem.
2. **Bij het zetten via de API** -- `warnings` op het antwoord van de deployment-schrijfactie.
   Bewust niet onder `approvals`: dat veld is voor wat op een beheerder wacht, en dit wacht
   op niemand.
3. **Vóór het zetten** -- `custom-domain-certificates` op `GET /api/v2/projects/{p}/clusters`,
   naast de `base-domains` die het cluster wél aanbiedt.
4. **In het OpenAPI-document** -- de beschrijving van `base-domain` (met een verwijzing naar
   dat endpoint) en van `tls` (dat `provided` daar de weg is).

### De uitweg

`tls: provided` met een eigen certificaat als attachment. Dan wordt er niets uitgegeven en
termineert de ingress op het meegeleverde certificaat, wat op elk cluster werkt.

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
