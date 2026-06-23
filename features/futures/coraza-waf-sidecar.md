# Coraza WAF Sidecar (block scanner / unwanted traffic before OPI)

**Status:** Future / proposed — not yet implemented.

## Problem

OPI's logs are full of internet background-radiation from automated vulnerability
scanners hitting random paths:

```
INFO: 89.187.173.180:0 - "GET /sites/default/settings.local.php HTTP/1.1" 302 Found
```

These are harmless (302/404, and OPI already has API-key auth), but they're noise,
and we'd rather drop as much unwanted traffic as possible *before* it reaches the app.

A hand-maintained path blocklist (in code or a dumb proxy) is the thing we want to
avoid — it's exactly what a WAF + ruleset exists to save us from.

## Decision

Use **OWASP Coraza + Core Rule Set (CRS)** as a **sidecar reverse-proxy container**
in front of OPI, via the **CRS-team-maintained image**:

- Image: `ghcr.io/coreruleset/coraza-crs:nginx` (the
  [coraza-crs-docker](https://github.com/coreruleset/coraza-crs-docker) project,
  maintained by the OWASP CRS team; latest release April 2026).
- **nginx flavor** (port 8081) — chosen over the Caddy/Apache flavors only because we
  already run nginx everywhere, so the one piece we might ever debug is in familiar
  syntax. The embedded server is purely internal to the sidecar; it adopts no new
  stack and owns no config outside the pod.

### Why this and not the alternatives

| Option | Verdict |
|---|---|
| Hand-rolled path blocklist / dumb proxy sidecar | Rejected — we'd maintain the list forever; barely better than a log filter. |
| CrowdSec at the ingress | Powerful (community IP blocklists, cluster-wide) but most moving parts (LAPI + agent + bouncer, cross-namespace secrets). Overkill for log noise. |
| **Coraza + CRS sidecar** | **Chosen** — self-contained, smart signature-based blocking, OWASP CRS blocks the scanner probes out of the box, no cluster-wide infra. |
| Just a uvicorn log filter | The cheapest noise fix (~15 lines, no infra); still a valid fallback if we decide blocking isn't worth the operational cost. |

The scanner probes that triggered this (`settings.local.php`, `.env`, `wp-*`,
path-traversal) are blocked by CRS **out of the box at paranoia 1** — zero custom
rules needed for the original complaint.

## Deployment shape

**Sidecar in the OPI pod** (not a standalone Deployment):

```
ingress (TLS) ──▶ OPI Service ──▶ [ coraza-crs:nginx sidecar :8081 ] ──▶ localhost:<opi-port> (OPI)
```

- The sidecar becomes the pod's entry port; the OPI **Service** `targetPort` retargets
  to the sidecar.
- Scales 1:1 with OPI, no extra Deployment/Service. (A standalone `opi-waf`
  Deployment was considered and rejected — it only buys independent scaling we don't
  need. YAGNI.)

## Configuration (env vars)

| Env var | Value | Purpose |
|---|---|---|
| `BACKEND` | `localhost:<opi-port>` | upstream = OPI in the same pod |
| `CORAZA_RULE_ENGINE` | `DetectionOnly` → `On` | **start in DetectionOnly**, flip to `On` after tuning |
| `PARANOIA` | `1` | CRS strictness (start low) |
| `ANOMALY_INBOUND` | default | block threshold |

### Adding custom blocked paths

Not a CSV env var — mount a SecLang rule file into `/opt/coraza/rules.d/`
(a ConfigMap in k8s). This is strictly better than an env list: full regex/method
matching. For the original scanner noise this isn't even needed (CRS covers it).

## ⚠️ Critical rollout caveat

A WAF in **blocking** mode in front of OPI **will produce false positives** on
legitimate traffic — htmx POSTs, API payloads, SOPS/secret blobs, wizard forms. CRS
doesn't know our app. Skipping the tuning step is how you take your own app down with
a WAF.

**Mandatory sequence: `DetectionOnly` → observe would-block logs → add exceptions →
`On`.**

## Implementation plan (sandbox first, then prod via Git→ArgoCD)

1. Add the Coraza sidecar to the OPI Deployment base, `CORAZA_RULE_ENGINE=DetectionOnly`,
   `BACKEND=localhost:<opi-port>`.
   → **verify:** pod runs, traffic still flows, OPI fully works.
2. Retarget the OPI Service `targetPort` to the sidecar; set nginx `trusted_proxies`
   so the real client IP comes from the ingress `X-Forwarded-For`.
   → **verify:** logs show real client IPs, not the ingress IP.
3. Fire the scanner paths at sandbox (`curl .../sites/default/settings.local.php`, etc.).
   → **verify:** CRS *logs* them as would-block; confirm zero false-positives on a real
   wizard/API run.
4. Flip sandbox to `CORAZA_RULE_ENGINE=On`.
   → **verify:** junk gets 403, OPI still 100% functional.
5. Promote to the `odcn` overlay via commit → ArgoCD (prod-changes-via-Git rule), same
   DetectionOnly→tune→On dance in prod.

## Dependencies

- `ghcr.io/coreruleset/coraza-crs:nginx`
- OPI Deployment + Service manifests (sidecar container + `targetPort` change)
- ConfigMap for any custom SecLang rules (optional)

## References

- [coraza-crs-docker](https://github.com/coreruleset/coraza-crs-docker) (the image)
- [Coraza introduction](https://www.coraza.io/docs/tutorials/introduction/)
- [coraza-caddy](https://github.com/corazawaf/coraza-caddy) (alternate flavor)
