# RBAC Request: Deployment Monitoring Permissions

**Status**: Pending
**Date**: 2026-03-08
**Priority**: High — blocks post-deploy health monitoring and OOM auto-tuning

## Problem

The operations manager (OPI) needs to monitor deployment status in user namespaces after deploying or refreshing applications. This is used for:

1. **OOM kill detection** — After deploy, OPI checks if pods were OOM-killed and auto-tunes memory limits
2. **Deployment status monitoring** — The dashboard and task system report deployment readiness (replicas ready/available/updated)
3. **Resource status API** — The `/api/resources/{project}` endpoint reports deployment health

Currently failing with:

```
Error from server (Forbidden): deployments.apps is forbidden:
User "system:serviceaccount:rig-prd-operations:namespace-manager"
cannot list resource "deployments" in API group "apps"
in the namespace "rig-prd-{project}"
```

## What We Need

The `namespace-manager` service account in the `rig-prd-operations` namespace needs **read-only** access to `deployments` in the `apps` API group, across all `rig-prd-*` user namespaces.

### Required RBAC Rule

```yaml
- apiGroups:
    - apps
  resources:
    - deployments
  verbs:
    - get
    - list
```

This should be added as a **ClusterRole** (or scoped to `rig-prd-*` namespaces if ODCN uses namespace-scoped Roles).

### Service Account Details

| Field | Value |
|---|---|
| Service Account Name | `namespace-manager` |
| Service Account Namespace | `rig-prd-operations` |
| Scope | All `rig-prd-*` namespaces (user project namespaces) |
| Access Level | Read-only (`get`, `list`) |

## Context

The sandbox environments (local, sandboxed-local) use a ClusterRole+ClusterRoleBinding defined in:
- `bootstrap/rig-system/kustomize/operations-manager/overlays/sandboxed-local/cluster-role.yaml`
- `bootstrap/rig-system/kustomize/operations-manager/overlays/sandboxed-local/cluster-binding.yaml`

The ODCN production overlay (`overlays/odcn-production/`) does **not** include these files — RBAC for production is managed externally by the ODCN team.

## Sandbox Fix

Already applied in this repository:
- `overlays/local/cluster-role.yaml` — added `apps/deployments` get+list
- `overlays/sandboxed-local/cluster-role.yaml` — added `apps/deployments` get+list
