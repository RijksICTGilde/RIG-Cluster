# Security Headers (internet.nl compliance)

## What it does

Adds HTTP security headers to all ZAD responses for internet.nl compliance. Works on both NGINX (sandbox) and HAProxy (production) because headers are set at the application level via FastAPI middleware, not just ingress annotations.

## Headers set

| Header | Value | Purpose |
|--------|-------|---------|
| Strict-Transport-Security | `max-age=31536000; includeSubDomains; preload` | Force HTTPS for 1 year |
| X-Content-Type-Options | `nosniff` | Prevent MIME type sniffing |
| X-Frame-Options | `DENY` | Prevent clickjacking |
| Referrer-Policy | `strict-origin-when-cross-origin` | Limit referrer leakage |
| Permissions-Policy | `geolocation=(), microphone=(), camera=()` | Disable unused browser APIs |
| Content-Security-Policy | See below | Restrict resource loading |

### Content-Security-Policy

```
default-src 'self';
script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net;
style-src 'self' 'unsafe-inline';
img-src 'self' data:;
font-src 'self';
connect-src 'self' <keycloak-origin>;
form-action 'self' <keycloak-origin>;
frame-ancestors 'none';
base-uri 'self'
```

- `unsafe-inline` for scripts: required by HTMX inline event handlers
- `unsafe-inline` for styles: required by ROOS component style attributes
- `cdn.jsdelivr.net`: Chart.js on the project-details page
- Keycloak origin: dynamically derived from `KEYCLOAK_URL` setting

## security.txt

Served at `/.well-known/security.txt` with `Content-Type: text/plain` per RFC 9116.

## How it works

- `SecurityHeadersMiddleware` in `opi/middleware/security_headers.py` runs on every response
- Uses `setdefault` so it won't override headers already set by NGINX or the application
- HSTS is only set when `request.url.scheme == "https"` (safe for local dev)
- HAProxy HSTS is also set via `haproxy.router.openshift.io/hsts_header` annotation on production ingresses

## Files

- `opi/middleware/security_headers.py` — the middleware
- `opi/server.py` — middleware registration + security.txt route
- `bootstrap/.../odcn-production/ingress-rijksapp.yaml` — HAProxy HSTS annotation
- `bootstrap/.../odcn-production/ingress.yaml` — HAProxy HSTS annotation
- `tests/test_security_headers.py` — 15 tests

## Not covered

- **IPv6 reachability** — ODC-Noord infrastructure
- **CAA DNS records** — manual action in TransIP
- **User application headers** — on production (HAProxy), user apps must set their own security headers; the ingress can only set HSTS
