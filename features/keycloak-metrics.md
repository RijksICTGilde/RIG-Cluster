# Keycloak Metrics

This document describes the Keycloak metrics integration with Prometheus for monitoring authentication and user activity.

## Overview

Keycloak exposes Prometheus-compatible metrics that provide insights into:
- User authentication activity (logins, failures, registrations)
- Token operations (refresh, client credentials)
- Request performance and latency
- Active sessions

## Configuration

Keycloak metrics are enabled via the following configuration in the deployment:

```yaml
env:
  - name: KC_METRICS_ENABLED
    value: "true"
  - name: KC_HEALTH_ENABLED
    value: "true"
```

**Important**: Keycloak exposes metrics on a separate **management interface** running on **port 9000** (not the main port 8080). This provides security isolation between the main application and observability endpoints.

Prometheus scrapes metrics via pod annotations pointing to the management port:

```yaml
annotations:
  prometheus.io/scrape: "true"
  prometheus.io/port: "9000"
  prometheus.io/path: "/metrics"
```

### Additional Configuration Options

| Option | Environment Variable | Default | Description |
|--------|---------------------|---------|-------------|
| `metrics-enabled` | `KC_METRICS_ENABLED` | `false` | Enable metrics endpoint |
| `http-management-port` | `KC_HTTP_MANAGEMENT_PORT` | `9000` | Management interface port |
| `cache-metrics-histograms-enabled` | `KC_CACHE_METRICS_HISTOGRAMS_ENABLED` | `false` | Enable cache histograms |
| `http-metrics-histograms-enabled` | `KC_HTTP_METRICS_HISTOGRAMS_ENABLED` | `false` | Enable HTTP request histograms |
| `http-metrics-slos` | `KC_HTTP_METRICS_SLOS` | - | Custom SLO buckets (ms) |

## Available Metrics

### Authentication Metrics

| Metric | Description | Labels |
|--------|-------------|--------|
| `keycloak_logins` | Total successful logins | realm, provider, client_id |
| `keycloak_failed_login_attempts` | Failed login attempts | realm, provider, error |
| `keycloak_registrations` | User registrations | realm, provider, client_id |
| `keycloak_refresh_tokens` | Token refresh operations | realm, provider, client_id |
| `keycloak_client_logins` | Client credential grants | realm, client_id |

### Request Metrics

| Metric | Description | Labels |
|--------|-------------|--------|
| `keycloak_request_duration_seconds` | Request latency histogram | method, route, status |
| `http_server_requests_seconds` | HTTP request metrics | method, uri, status |

## Prometheus Queries

### Login Activity

**Total logins per realm (last 24h):**
```promql
sum(increase(keycloak_logins_total[24h])) by (realm)
```

**Login rate per minute:**
```promql
sum(rate(keycloak_logins_total[5m])) by (realm) * 60
```

**Logins by authentication provider:**
```promql
sum(increase(keycloak_logins_total[24h])) by (realm, provider)
```

### Failed Logins

**Failed login attempts per realm (last 24h):**
```promql
sum(increase(keycloak_failed_login_attempts_total[24h])) by (realm)
```

**Failed login rate (potential brute force detection):**
```promql
sum(rate(keycloak_failed_login_attempts_total[5m])) by (realm) * 60 > 10
```

**Login success rate:**
```promql
sum(rate(keycloak_logins_total[1h])) by (realm)
/
(sum(rate(keycloak_logins_total[1h])) by (realm) + sum(rate(keycloak_failed_login_attempts_total[1h])) by (realm))
* 100
```

### User Registrations

**New registrations per realm (last 24h):**
```promql
sum(increase(keycloak_registrations_total[24h])) by (realm)
```

**Registration rate per day:**
```promql
sum(increase(keycloak_registrations_total[24h])) by (realm)
```

### Token Operations

**Token refresh rate:**
```promql
sum(rate(keycloak_refresh_tokens_total[5m])) by (realm) * 60
```

**Client credential grants:**
```promql
sum(increase(keycloak_client_logins_total[1h])) by (realm, client_id)
```

### Performance Metrics

**Average request latency:**
```promql
histogram_quantile(0.95, sum(rate(keycloak_request_duration_seconds_bucket[5m])) by (le, method))
```

**Request rate by endpoint:**
```promql
sum(rate(http_server_requests_seconds_count[5m])) by (uri)
```

## Grafana Dashboard Examples

### Authentication Overview Panel

```promql
# Logins today
sum(increase(keycloak_logins_total[24h]))

# Failed logins today
sum(increase(keycloak_failed_login_attempts_total[24h]))

# New users today
sum(increase(keycloak_registrations_total[24h]))
```

### Per-Realm Statistics

```promql
# Active realms with login activity
count(sum(rate(keycloak_logins_total[1h])) by (realm) > 0)

# Most active realm
topk(5, sum(increase(keycloak_logins_total[24h])) by (realm))
```

### Alerting Rules

**High failed login rate (potential attack):**
```yaml
alert: KeycloakHighFailedLogins
expr: sum(rate(keycloak_failed_login_attempts_total[5m])) by (realm) * 60 > 50
for: 5m
labels:
  severity: warning
annotations:
  summary: "High failed login rate in realm {{ $labels.realm }}"
  description: "More than 50 failed logins per minute detected"
```

**Low success rate:**
```yaml
alert: KeycloakLowLoginSuccessRate
expr: |
  sum(rate(keycloak_logins_total[1h])) by (realm)
  /
  (sum(rate(keycloak_logins_total[1h])) by (realm) + sum(rate(keycloak_failed_login_attempts_total[1h])) by (realm))
  < 0.9
for: 15m
labels:
  severity: warning
annotations:
  summary: "Low login success rate in realm {{ $labels.realm }}"
```

## Accessing Metrics

Once deployed, metrics are available at:

- **Management interface (internal)**: `http://keycloak:9000/metrics`
- **Via Prometheus**: Query through the Prometheus API or Grafana
- **Operations Manager**: Future integration via the metrics dashboard

**Note**: The metrics endpoint is only accessible on the management port (9000), not on the main HTTP port (8080). This is by design for security reasons - the management interface is not exposed via ingress.

## Troubleshooting

### Metrics not appearing

1. Verify `KC_METRICS_ENABLED=true` is set in the deployment
2. Check pod annotations are present: `kubectl get pod -n rig-system -l app=keycloak -o yaml`
3. Test metrics endpoint directly on the management port:
   ```bash
   kubectl exec -n rig-system deployment/keycloak -- curl -s localhost:9000/metrics | head -20
   ```
4. Check Prometheus targets: Access Prometheus UI and verify keycloak target is UP
5. Verify the management interface is running:
   ```bash
   kubectl exec -n rig-system deployment/keycloak -- curl -s localhost:9000/health
   ```

### Missing realm metrics

Realm-specific metrics only appear after authentication activity occurs in that realm. New realms will show metrics once users start logging in.

## Future Enhancements

- Integration with Operations Manager dashboard
- Per-project realm metrics in project details page
- Automated alerting for suspicious activity
- User count metrics (requires custom metric or API query)
