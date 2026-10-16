# Metrics Endpoint Security

## Problem Statement

Prometheus metrics endpoints (`/metrics`) can expose sensitive operational information:
- Internal service names and topology
- Request rates and patterns
- Error rates and types
- Resource usage details
- Authentication statistics (login attempts, failures)

These endpoints should **never** be publicly accessible and must be restricted to internal cluster traffic only (primarily Prometheus scraping).

## Affected Components

### Current
- **Keycloak** - `/metrics` endpoint on management port 9000 (separate from public UI on 8080) ✅ Secured by default
- **Operations Manager (OPI)** - `/metrics` on port 8000, afgeschermd met het scrape-token (zie hieronder) ✅

### Future (applications deployed via platform)
- User applications with Prometheus metrics
- Sidecar containers exposing metrics
- Service meshes with metrics endpoints

## OPI's eigen /metrics

`GET /metrics` op de Operations Manager eist dezelfde header als de projectapps met de dienst
`metrics-scraper`: `Authorization: Bearer <PROMETHEUS_METRICS_AUTH_TOKEN>`. Het token komt uit
het secret `prometheus-metrics-auth` (sleutel `token`) en onze Prometheus stuurt het al mee bij
de job `kubernetes-pods`, dus de scrape hoefde er niet voor te veranderen.

De controle zit in de route zelf (`validate_metrics_scrape_token` in
`opi/api/endpoint_util.py`), niet in de SSO-middleware: Prometheus heeft geen sessie, dus die
middleware blijft `/metrics` overslaan.

| situatie | antwoord |
|---|---|
| geen `Authorization`-header | 401 |
| header zonder `Bearer `-voorvoegsel | 401 |
| `Bearer` met fout token | 401 |
| `PROMETHEUS_METRICS_AUTH_TOKEN` niet gezet | 501, het endpoint is dan dicht, niet open |
| `Bearer` met het goede token | 200 met de metrieken |

Bij 401 en 501 komt er geen enkele metriek terug en staat er geen hint over het token in het
antwoord. Een mislukte poging komt als WARNING in de log, zonder de headerwaarde.

Met de hand opvragen:

```bash
kubectl exec -n rig-prd-operations deploy/operations-manager -- \
  sh -c 'curl -s -H "Authorization: Bearer $PROMETHEUS_METRICS_AUTH_TOKEN" localhost:8000/metrics | head'
```

Het token los nodig, bijvoorbeeld om van buiten de pod te toetsen:

```bash
TOKEN=$(kubectl get secret -n rig-prd-operations prometheus-metrics-auth -o jsonpath='{.data.token}' | base64 -d)
```

## Security Concerns

1. **Information disclosure**: Attackers can learn about system architecture, load patterns, and potential vulnerabilities
2. **Enumeration**: Metrics can reveal internal service names, database connections, and API endpoints
3. **Timing attacks**: Request latency metrics could be used to infer system state
4. **Credential exposure**: Some poorly configured metrics may leak secrets or tokens

## Solution Options

### Option 1: Ingress Path Blocking (Recommended for Keycloak)

Block `/metrics` path at the ingress level so it's never routed externally.

```yaml
# In ingress configuration
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: keycloak
  annotations:
    nginx.ingress.kubernetes.io/server-snippet: |
      location /metrics {
        deny all;
        return 403;
      }
```

**Pros**: Simple, works with existing setup
**Cons**: Requires ingress-level configuration per service

### Option 2: Separate Metrics Port

Configure services to expose metrics on a different port that's not exposed via ingress.

```yaml
# Keycloak example
env:
  - name: KC_HTTP_MANAGEMENT_PORT
    value: "9000"
```

Then only expose port 8080 via ingress, while Prometheus scrapes port 9000 internally.

**Pros**: Clean separation, no ingress rules needed
**Cons**: Requires application support, more complex configuration

### Option 3: Network Policies

Use Kubernetes NetworkPolicies to restrict access to metrics endpoints.

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: metrics-access-policy
  namespace: rig-system
spec:
  podSelector:
    matchLabels:
      app: keycloak
  policyTypes:
    - Ingress
  ingress:
    # Allow metrics only from Prometheus
    - from:
        - namespaceSelector:
            matchLabels:
              name: rig-system
          podSelector:
            matchLabels:
              app: prometheus
      ports:
        - protocol: TCP
          port: 8080
          # Note: This blocks ALL traffic on 8080 except from Prometheus
          # which is too restrictive for Keycloak's main UI
```

**Pros**: Kubernetes-native, fine-grained control
**Cons**: Complex to configure per-path, may conflict with legitimate traffic

### Option 4: Service Mesh (Istio/Linkerd)

Use a service mesh to control access to specific paths.

```yaml
# Istio AuthorizationPolicy
apiVersion: security.istio.io/v1
kind: AuthorizationPolicy
metadata:
  name: deny-metrics-external
spec:
  selector:
    matchLabels:
      app: keycloak
  action: DENY
  rules:
    - to:
        - operation:
            paths: ["/metrics", "/metrics/*"]
      from:
        - source:
            notNamespaces: ["rig-system"]
```

**Pros**: Powerful, application-agnostic
**Cons**: Requires service mesh infrastructure

## Recommended Approach

### Phase 1: Immediate (Keycloak)

Use **Option 1 (Ingress Path Blocking)** for Keycloak:

```yaml
# infrastructure/bootstrap/infrastructure/keycloak/controller/base/ingress.yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: keycloak
  annotations:
    nginx.ingress.kubernetes.io/server-snippet: |
      location = /metrics {
        return 403;
      }
      location = /health {
        return 403;
      }
      location = /health/ready {
        return 403;
      }
      location = /health/live {
        return 403;
      }
```

### Phase 2: Platform Standard (Future Applications)

For applications deployed via the platform:

1. **Document best practices** for application developers:
   - Use separate metrics port when possible
   - Never expose metrics on public-facing ports

2. **Platform-level enforcement**:
   - Automatically inject ingress rules blocking `/metrics`
   - Provide sidecar option for metrics collection on separate port

3. **Network policy templates**:
   - Provide ready-to-use NetworkPolicy templates
   - Auto-generate policies during deployment

## Implementation Checklist

- [ ] Add ingress rule to block `/metrics` on Keycloak
- [ ] Add ingress rule to block `/health/*` endpoints on Keycloak (optional, less sensitive)
- [ ] Document metrics security requirements for application developers
- [ ] Create NetworkPolicy template for project deployments
- [ ] Consider adding metrics port configuration to deployment templates
- [ ] Add security scanning to detect exposed metrics endpoints

## Testing

After implementing, verify metrics are blocked externally:

```bash
# Should return 403
curl -I https://keycloak.example.com/metrics

# Should still work internally (from within cluster)
kubectl exec -n rig-system deployment/prometheus -- \
  curl -s http://keycloak.rig-system.svc:8080/metrics | head -5
```

## References

- [Keycloak Metrics Documentation](https://www.keycloak.org/server/configuration-metrics)
- [NGINX Ingress Annotations](https://kubernetes.github.io/ingress-nginx/user-guide/nginx-configuration/annotations/)
- [Kubernetes Network Policies](https://kubernetes.io/docs/concepts/services-networking/network-policies/)
