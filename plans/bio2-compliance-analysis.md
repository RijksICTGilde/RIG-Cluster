# BIO2 Compliance Analysis - Operations Manager (OPI)

## 1. What is BIO2?

The **Baseline Informatiebeveiliging Overheid 2 (BIO2)** is the mandatory information security standard for all Dutch government organizations. It replaces BIO 1.04zv and is structured according to:

- **Part 1 (BIO2-Kader)**: Based on NEN-EN-ISO/IEC 27001:2023
- **Part 2 (BIO-overheidsmaatregelen)**: Based on NEN-EN-ISO/IEC 27002:2022

As of **5 March 2026**, BIO2 v1.3 is published in the Staatscourant and legally binding via the **Cyberbeveiligingswet (Cbw)** - the Dutch NIS2 implementation.

### BIO2 Control Domains (ISO 27002:2022 structure)

| Domain | Controls | Description |
|--------|----------|-------------|
| **A5 - Organizational** | 37 controls | Policies, roles, asset management, supplier relations, ISMS |
| **A6 - People** | 8 controls | Screening, awareness, training, disciplinary process |
| **A7 - Physical** | 14 controls | Secure areas, equipment, cabling, clear desk |
| **A8 - Technological** | 34 controls | Access control, crypto, network security, secure development, logging |

---

## 2. Current OPI Security Posture

### Strengths (Already Compliant Areas)

| BIO2 Area | OPI Implementation | Status |
|-----------|-------------------|--------|
| **A5.15 Access Control** | Keycloak SSO (OAuth2/OIDC), email allowlist, multi-level API keys (project/admin/master) | Good |
| **A5.09 Asset Inventory** | Projects tracked in database, Git repos as source of truth | Partial |
| **A8.03 Information Access Restriction** | Per-project namespaces, K8s RBAC generation, NetworkPolicies | Good |
| **A8.05 Secure Authentication** | Keycloak handles identity, session cookies signed with SECRET_KEY | Good |
| **A8.09 Configuration Management** | GitOps via ArgoCD, Jinja2 templates for K8s manifests | Good |
| **A8.24 Use of Cryptography** | AGE encryption (post-quantum), SOPS for secrets at rest, TLS for transit | Good |
| **A8.15 Logging** | Centralized logging via Python logging, rotating file handler, Prometheus metrics | Partial |
| **A8.16 Monitoring** | Prometheus/OpenTelemetry metrics, health/readiness probes | Partial |
| **A8.25 Secure Development** | Ruff linting with security rules (S), Pyright type checking, non-root container | Good |
| **A8.13 Information Backup** | Kopia backups (PVC, database, S3), retention policies, encryption | Good |
| **A8.08 Vulnerability Management** | Dependency locking (uv.lock), security linting | Partial |
| **A5.30 ICT Readiness** | Backup/restore system, federation for cross-cluster, graceful shutdown | Partial |

### Gaps (Non-Compliant or Missing)

| BIO2 Control | Gap Description | Severity |
|--------------|----------------|----------|
| **A5.35 ISMS** | No formal ISMS documented (BIO2 requires functioning ISMS per ISO 27001) | HIGH |
| **A5.24 Incident Management** | No structured incident response process; errors logged but no escalation/notification | HIGH |
| **A5.28 Evidence Collection** | No forensic logging or tamper-proof audit trail | MEDIUM |
| **A5.01 Information Security Policy** | No documented security policy for OPI | HIGH |
| **A5.10 Awareness/Training** | No security training tracking (BIO2 5.10.1, 5.10.4 - new mandatory measures) | MEDIUM |
| **A8.07 Anti-Malware** | No malware scanning on uploads or container images | MEDIUM |
| **A8.06 Rate Limiting** | No rate limiting on auth endpoints (brute force risk) | HIGH |
| **A8.08 Vuln Management** | No automated dependency vulnerability scanning (e.g., `pip-audit`, Trivy) | HIGH |
| **A5.14 Internet-Facing Registry** | No registry of internet-facing systems/APIs (BIO2 5.14.04 - new mandatory) | MEDIUM |
| **A8.15 Audit Logging** | Logging exists but no structured audit trail (who did what, when) | HIGH |
| **A8.20 Network Security** | TLS disabled by default for ArgoCD, MinIO, S3 backups | HIGH |
| **A8.25 Secure Dev Lifecycle** | No SAST/DAST scanning in CI/CD pipeline | MEDIUM |
| **A8.26 Application Security** | CSRF partially implemented; API key comparison not time-constant | MEDIUM |

### Security Issues Found in Code

| Issue | Location | Fix |
|-------|----------|-----|
| Default passwords in config | `config.py` - ARGOCD_PASSWORD="admin", DB password="changeMe123!" | Remove defaults, require env vars |
| API key comparison not constant-time | `endpoint_util.py:113` | Use `secrets.compare_digest()` |
| SOPS key fallback to local file | `config.py:389-431` | Disable in production |
| DEBUG=True by default | `config.py` | Default to False |
| ARGOCD_VERIFY_SSL=False | `config.py:206` | Default to True |
| TLS disabled for MinIO/S3 | `config.py` | Default to True |
| USE_UNSAFE_API_KEY option | `api_keys.py:37-39` | Block in production |
| ProxyHeaders trusts all hosts | `server.py:287` - `trusted_hosts=["*"]` | Restrict to known proxies |

---

## 3. Compliance Plan

### Phase 1 - Critical Security Fixes (Code Changes)

These are direct code fixes that address both BIO2 requirements and identified vulnerabilities.

#### 1.1 Constant-time API key comparison (A8.26)
- **File**: `opi/api/endpoint_util.py`
- **Change**: Replace `!=` with `secrets.compare_digest()` for all key comparisons
- **Effort**: Small

#### 1.2 Secure defaults in configuration (A8.09)
- **File**: `opi/core/config.py`
- **Changes**:
  - Remove hardcoded default passwords (ARGOCD_PASSWORD, DATABASE_ADMIN_PASSWORD, API_TOKEN)
  - Set `DEBUG=False` by default
  - Set `ARGOCD_VERIFY_SSL=True` by default
  - Set TLS enabled by default for MinIO/S3/ArgoCD
  - Block `USE_UNSAFE_API_KEY` when `ENVIRONMENT != "local"`
  - Restrict SOPS key fallback to local environment only
- **Effort**: Medium

#### 1.3 Rate limiting on authentication (A8.06)
- **Files**: `opi/api/auth_routes.py`, `opi/server.py`
- **Change**: Add rate limiting middleware (e.g., `slowapi`) on `/auth/login` and `/auth/callback`
- **Effort**: Medium

#### 1.4 Restrict trusted proxy hosts (A8.26)
- **File**: `opi/server.py`
- **Change**: Configure `ProxyHeadersMiddleware` with specific trusted hosts instead of `["*"]`
- **Effort**: Small

### Phase 2 - Structured Audit Logging (A8.15, A5.28)

BIO2 requires traceable audit trails. Current logging is operational, not audit-grade.

#### 2.1 Implement structured audit logging
- **New module**: `opi/core/audit.py`
- **What to log** (structured JSON):
  - Authentication events (login, logout, failed attempts)
  - Authorization decisions (access granted/denied)
  - Project mutations (create, update, delete)
  - Secret operations (encrypt, decrypt, rotate)
  - Deployment actions (deploy, rollback, scale)
  - Admin operations (reconciliation, cleanup)
- **Fields**: timestamp, actor (email/API key ID), action, resource, outcome, source IP
- **Effort**: Large

#### 2.2 Tamper-proof log forwarding
- Forward audit logs to external system (e.g., Loki, Elasticsearch)
- Separate from operational logs
- **Effort**: Medium (infrastructure)

### Phase 3 - Vulnerability Management (A8.08)

#### 3.1 Dependency scanning
- Add `pip-audit` or `safety` to CI/CD pipeline
- Add Trivy container image scanning
- **Effort**: Medium

#### 3.2 SAST integration
- Add `bandit` or extend Ruff security rules in CI
- **Effort**: Small

#### 3.3 Container image scanning
- Add Trivy scan to Dockerfile build pipeline
- Scan base images for known CVEs
- **Effort**: Medium

### Phase 4 - Network Security Hardening (A8.20, A8.21)

#### 4.1 Enforce TLS everywhere
- Update production config overlays to require TLS for all internal communication
- ArgoCD, MinIO, PostgreSQL, Keycloak - all TLS-enabled
- **Effort**: Medium (infrastructure)

#### 4.2 NetworkPolicy audit
- Review generated NetworkPolicies for least-privilege
- Ensure no `allow-all` policies in production
- **Effort**: Small

### Phase 5 - Documentation & Governance (A5.01, A5.35)

These are organizational/process requirements, not code changes.

#### 5.1 Information Security Policy document
- Document OPI's security architecture, controls, and responsibilities
- Required by BIO2 A5.01 (updated annually per measure 5.01.02)
- **Location**: `features/security-policy.md` or separate doc

#### 5.2 ISMS documentation
- BIO2 5.35.1 requires a functioning ISMS per ISO 27001
- Document: scope, risk assessment methodology, risk treatment plan
- This is an organizational effort beyond OPI code

#### 5.3 Internet-facing system registry
- BIO2 5.14.04 (new): Register all internet-facing systems, web apps, IPs, APIs
- Create and maintain a registry of OPI-managed endpoints
- **Effort**: Medium

#### 5.4 Incident response procedure
- Document escalation paths, roles, response timelines
- Integrate with OPI alerting (Prometheus alerts)
- **Effort**: Medium (process)

---

## 4. Priority Matrix

| Priority | Phase | Items | Impact |
|----------|-------|-------|--------|
| **P0 - Now** | Phase 1 | Secure defaults, constant-time comparison, rate limiting | Closes active vulnerabilities |
| **P1 - Short term** | Phase 2 | Structured audit logging | Core BIO2 requirement for traceability |
| **P2 - Medium term** | Phase 3 | Vulnerability scanning in CI/CD | Required for A8.08 compliance |
| **P3 - Medium term** | Phase 4 | TLS enforcement, NetworkPolicy audit | Production hardening |
| **P4 - Ongoing** | Phase 5 | Security policy, ISMS, incident response docs | Governance requirements |

---

## 5. BIO2 Specific New Measures - Applicability

| New BIO2 Measure | Requirement | OPI Applicability |
|-------------------|-------------|-------------------|
| **5.09.01** | Maintain detailed asset inventory | OPI manages project assets - could auto-generate registry |
| **5.10.1** | Leaders demonstrate cybersecurity training | Organizational - not OPI code |
| **5.10.4** | Regular employee security training | Organizational - not OPI code |
| **5.14.04** | Register internet-facing systems, APIs | OPI could generate this from project configs |
| **5.14.05** | Public websites reported via govt domain registry | OPI manages domains - could automate reporting |
| **5.22.02** | Current vendor and contract register | Organizational - not OPI code |
| **5.35.1** | Functioning ISMS per ISO 27001 | Organizational - OPI supports with controls |
| **8.07.5** | Test users on click behavior annually | Organizational - not OPI code |
| **8.08.04** | Annual technical compliance checks | OPI could support via automated scanning |

---

## 6. Sources

- [BIO2 Official Site](https://www.bio-overheid.nl/)
- [BIO2 v1.3 PDF](https://www.bio-overheid.nl/media/dr4inbhc/20260109-baseline-informatiebeveiliging-overheid-2-bio2-v13-def.pdf)
- [BIO2 GitHub (MinBZK)](https://minbzk.github.io/Baseline-Informatiebeveiliging-Overheid/inleiding/)
- [BIO2 Changes Overview (Bureau Veritas)](https://cybersecurity.bureauveritas.com/nl/services/proces/audit-assurance-services/bio-compliance/bio2-verschillen-met-bio1)
- [NL Digital Government](https://www.nldigitalgovernment.nl/overview/government-information-security-baseline/)
- [BIO2 FAQ](https://www.bio-overheid.nl/category/producten/faq)
