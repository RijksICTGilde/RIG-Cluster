# Keycloak RIG Metrics

Custom Prometheus metrics for RIG Keycloak providing realm-specific user counts and authentication activity.

## Overview

The RIG Keycloak plugin extends the standard Keycloak metrics with:
- **User counts per realm** - Total users and breakdown by identity provider type (local, SAML, OIDC)
- **Authentication events** - Login attempts, successes, failures, registrations, logouts
- **JVM metrics** - Memory, threads, uptime

These metrics complement Keycloak's built-in metrics (available on port 9000) with realm-specific information that Keycloak doesn't expose by default.

## Endpoint

```
GET /realms/master/rig-metrics
```

The endpoint is accessible via any realm path but returns metrics for **all realms**. Using `/realms/master/rig-metrics` is recommended for consistency.

**Port**: 8080 (main Keycloak port, not the management interface)

**Content-Type**: `text/plain; version=0.0.4; charset=utf-8` (Prometheus format)

## Het endpoint eist het scrape-token

De poort 8080 is dezelfde poort die de Ingress met `path: /` publiceert, en de uitvoer bevat
gebruikersaantallen en loginactiviteit per realm. Het endpoint antwoordt daarom alleen met
inhoud bij de header `Authorization: Bearer <token>`, met hetzelfde token als OPI op zijn
eigen `/metrics` controleert: het secret `prometheus-metrics-auth` (sleutel `token`), dat de
pod als `RIG_METRICS_TOKEN` leest.

| situatie | antwoord |
|---|---|
| geen `Authorization`-header | 401 |
| header zonder `Bearer `-voorvoegsel | 401 |
| `Bearer` met fout token | 401 |
| `RIG_METRICS_TOKEN` niet gezet | 503, het endpoint is dan dicht, niet open |
| `Bearer` met het goede token | 200 met de metrieken |

Bij 401 en 503 komt er geen enkele metriek terug en staat er geen hint over het token in het
antwoord. Een mislukte poging komt als WARN in de log, zonder de headerwaarde. De vergelijking
loopt via `MessageDigest.isEqual`, dus de responstijd verraadt het token niet.

Een geweigerd antwoord draagt `Content-Type: text/plain`, ook zonder body. Keycloaks
`DefaultSecurityHeadersProvider` breekt een antwoord zonder mediatype af, en dan komt de 503
bij de client aan als een 500 met een JSON-foutpagina. Gemeten op de sandbox.

Met de hand opvragen:

```bash
kubectl exec -n rig-prd-operations deploy/keycloak -- \
  sh -c 'curl -s -H "Authorization: Bearer $RIG_METRICS_TOKEN" localhost:8080/realms/master/rig-metrics | head'
```

Het token los, bijvoorbeeld om van buiten de pod te toetsen:

```bash
TOKEN=$(kubectl get secret -n rig-prd-operations prometheus-metrics-auth -o jsonpath='{.data.token}' | base64 -d)
```

Op de sandbox is de namespace `rig-system`.

## Metrics

### JVM Metrics

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `rig_keycloak_jvm_memory_bytes` | gauge | area, type | JVM memory usage |
| `rig_keycloak_jvm_threads_current` | gauge | - | Current thread count |
| `rig_keycloak_jvm_uptime_seconds` | gauge | - | JVM uptime |

### User Metrics (Gauges)

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `rig_keycloak_realms_total` | gauge | - | Total realms (excluding master) |
| `rig_keycloak_users_total` | gauge | realm | Total users per realm |
| `rig_keycloak_users_by_idp_total` | gauge | realm, idp_type | Users per realm by IDP type |
| `rig_keycloak_metrics_collection_error` | gauge | realm, part | 1 als die telling faalde, 0 als hij lukte |

The `idp_type` label has values: `local`, `saml`, `oidc`

De foutgauge bestaat omdat `users_by_idp` op een private API van `keycloak-model-jpa`
telt. Tot RC-411 viel een mislukte telling terug op "alle gebruikers zijn local" en gaf
het endpoint 200, en dan is een kapotte query niet te onderscheiden van een realm zonder
federatie. `part` is `users_by_idp` (met de realm erbij) of `gauges` (de hele sectie, dan
is de realm leeg). Een reeks die ontbreekt is nooit het enige signaal: bij succes staat de
gauge op 0.

### Authentication Metrics (Counters)

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `rig_keycloak_login_attempts_total` | counter | realm, idp_type, client_id | All login attempts |
| `rig_keycloak_logins_total` | counter | realm, idp_type, client_id | Successful logins |
| `rig_keycloak_login_errors_total` | counter | realm, idp_type, error, client_id | Failed logins |
| `rig_keycloak_registrations_total` | counter | realm, idp_type, client_id | User registrations |
| `rig_keycloak_logouts_total` | counter | realm | User logouts |

## Auto-Registration

The metrics event listener (`rig-metrics-listener`) is **automatically enabled** on:
- All existing realms at Keycloak startup
- Newly created realms

No manual configuration is required.

## Example Output

```
# HELP rig_keycloak_jvm_memory_bytes JVM memory usage in bytes
# TYPE rig_keycloak_jvm_memory_bytes gauge
rig_keycloak_jvm_memory_bytes{area="heap",type="used"} 268435456
rig_keycloak_jvm_memory_bytes{area="heap",type="max"} 536870912

# HELP rig_keycloak_realms_total Total number of realms (excluding master)
# TYPE rig_keycloak_realms_total gauge
rig_keycloak_realms_total 3

# HELP rig_keycloak_users_total Total users per realm
# TYPE rig_keycloak_users_total gauge
rig_keycloak_users_total{realm="project-alpha"} 25
rig_keycloak_users_total{realm="project-beta"} 12

# HELP rig_keycloak_users_by_idp_total Total users per realm and identity provider type
# TYPE rig_keycloak_users_by_idp_total gauge
rig_keycloak_users_by_idp_total{realm="project-alpha",idp_type="local"} 5
rig_keycloak_users_by_idp_total{realm="project-alpha",idp_type="saml"} 20
rig_keycloak_users_by_idp_total{realm="project-alpha",idp_type="oidc"} 0

# HELP rig_keycloak_logins_total Total successful logins
# TYPE rig_keycloak_logins_total counter
rig_keycloak_logins_total{realm="project-alpha",idp_type="saml",client_id="my-app"} 150
rig_keycloak_logins_total{realm="project-alpha",idp_type="local",client_id="my-app"} 30
```

## Prometheus Configuration

### Scrape Config

```yaml
scrape_configs:
  - job_name: 'keycloak-rig-metrics'
    metrics_path: '/realms/master/rig-metrics'
    authorization:
      type: Bearer
      credentials_file: /etc/prometheus/secrets/metrics-auth/token
    static_configs:
      - targets: ['keycloak.rig-system.svc:8080']
```

Zonder dat `authorization`-blok strandt de scrape op een 401. Prometheus rekent een
niet-2xx-antwoord als een mislukte scrape, dus de target staat dan op `down` met
"server returned HTTP status 401 Unauthorized" en er zijn geen metrieken.

### Useful Queries

**Total users across all realms:**
```promql
sum(rig_keycloak_users_total)
```

**Users per realm:**
```promql
rig_keycloak_users_total
```

**Users by IDP type (pie chart):**
```promql
sum(rig_keycloak_users_by_idp_total) by (idp_type)
```

**Login rate per realm (last hour):**
```promql
sum(increase(rig_keycloak_logins_total[1h])) by (realm)
```

**Failed login rate (potential security issue):**
```promql
sum(rate(rig_keycloak_login_errors_total[5m])) by (realm) * 60 > 10
```

**Login success rate per realm:**
```promql
sum(rate(rig_keycloak_logins_total[1h])) by (realm)
/
sum(rate(rig_keycloak_login_attempts_total[1h])) by (realm)
* 100
```

## Alerting Examples

**High failed login rate:**
```yaml
alert: KeycloakHighFailedLogins
expr: sum(rate(rig_keycloak_login_errors_total[5m])) by (realm) * 60 > 50
for: 5m
labels:
  severity: warning
annotations:
  summary: "High failed login rate in realm {{ $labels.realm }}"
```

**New realm with many users (growth tracking):**
```yaml
alert: RealmUserGrowth
expr: increase(rig_keycloak_users_total[24h]) > 100
for: 1h
labels:
  severity: info
annotations:
  summary: "Realm {{ $labels.realm }} gained many users"
```

## Implementation Details

The metrics are implemented as a Keycloak SPI provider in the `keycloak-saml-nameid-mapper` JAR:

- `PrometheusExporter` - Singleton managing counters and gauge export
- `RigMetricsEventListener` - Captures login/logout/registration events
- `RigMetricsEndpoint` - JAX-RS endpoint at `/realms/{realm}/rig-metrics`
- `MetricsTokenGuard` - de tokencontrole, los van Keycloak te toetsen

**Design decisions:**
- No external dependencies (no Prometheus client library) - uses simple `AtomicLong` counters and string formatting
- User counts use efficient JPA/SQL queries (not iteration) for O(1) database performance
- Event listener auto-registers on all realms at startup and on new realm creation

## Comparison with Built-in Metrics

| Feature | Built-in (port 9000) | RIG Metrics (port 8080) |
|---------|---------------------|------------------------|
| JVM metrics | Yes (detailed) | Yes (basic) |
| HTTP metrics | Yes | No |
| User count per realm | No | Yes |
| Users by IDP type | No | Yes |
| Login counts per realm | No | Yes |
| Realm count | No | Yes |

Use both endpoints for comprehensive monitoring:
- Port 9000 `/metrics` for JVM/HTTP performance
- Port 8080 `/realms/master/rig-metrics` for realm/user metrics

## Bij de upgrade naar Keycloak 26

De resourceklasse draagt `@Provider` omdat RESTEasy in de Quarkus-distributie de
JAX-RS-annotaties anders stil overslaat (keycloak#25882). In een discussie bij dat issue staat
dat die workaround vanaf 26.2.0 niet meer werkt. Breekt dat, dan geeft het endpoint 404 of 405
en komt de tokencontrole er niet eens aan te pas, dus de meting na de upgrade is een 401
zonder header EN een 200 met header. Zie
`plans/de-keycloak-gaat-naar-26-en-de-providers-moeten-mee.md`.

## Future Enhancements

- [ ] Expose on management interface (port 9000) at `/rig-metrics` path
- [ ] Active sessions per realm
- [ ] Client-specific metrics
- [ ] Integration with Operations Manager dashboard
