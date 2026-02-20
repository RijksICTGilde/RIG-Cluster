# Zero-Downtime Rolling Updates

## What It Is

Prevents 503 errors during rolling updates by adding a `preStop` lifecycle hook to pod containers. This addresses a well-known Kubernetes race condition where the ingress controller still routes traffic to a terminating pod before its backend list is updated.

## The Problem

When Kubernetes performs a rolling update (even with `maxUnavailable: 0`), two things happen **in parallel** when a pod is marked for deletion:

1. The **kubelet** executes the `preStop` hook, then sends SIGTERM to the container
2. The **endpoint controller** removes the pod from the Service endpoints, and the **ingress controller** updates its backend list

Because these are asynchronous and uncoordinated, there's a window where the ingress controller still sends traffic to a pod that has already received SIGTERM and is shutting down. This results in 503 responses visible to end users.

```
Timeline without preStop:

  t=0   Pod marked for deletion
  t=0   SIGTERM sent to container (app starts shutting down)
  t=0   Endpoint controller begins removing pod from Service
  t=1-3 Ingress controller still routing to terminating pod --> 503!
  t=3   Ingress controller finishes backend update
```

## The Solution

A `preStop` hook with a short `sleep` delays the SIGTERM, keeping the application alive and serving while the ingress controller catches up:

```
Timeline with preStop sleep 5:

  t=0   Pod marked for deletion
  t=0   preStop hook starts (sleep 5)
  t=0   Endpoint controller begins removing pod from Service
  t=1-3 Ingress controller updates backend list (pod still serving!)
  t=5   preStop finishes, SIGTERM sent to container
  t=5+  App shuts down gracefully, no more traffic routed to it
```

## How It's Implemented

### Operations Manager Deployment

In `bootstrap/rig-system/kustomize/operations-manager/base/deployment.yaml`:

```yaml
containers:
  - name: operations-manager
    lifecycle:
      preStop:
        exec:
          command: ["sleep", "5"]
```

Combined with the existing rolling update strategy:

```yaml
strategy:
  type: RollingUpdate
  rollingUpdate:
    maxUnavailable: 0
    maxSurge: 1
```

### Project Workload Deployments

In `operations-manager/python/manifests/deployment.yaml.jinja`, all project containers get the same hook:

```yaml
containers:
  - name: app
    lifecycle:
      preStop:
        exec:
          command: ["sleep", "5"]
```

## Why 5 Seconds?

- The ingress controller (NGINX) typically updates its backend list within 1-3 seconds
- 5 seconds provides a comfortable margin without unnecessarily delaying rollouts
- The default `terminationGracePeriodSeconds` (30s) is more than enough to cover 5s sleep + graceful shutdown

## Alternatives Considered

| Approach | Why Not |
|---|---|
| Endpoint Slice terminating condition (K8s 1.26+) | Requires ingress controller support; not universally reliable yet |
| Pod Readiness Gates | Designed for external load balancers (AWS ALB, etc.); overkill for in-cluster ingress |
| Application-level drain only | Complementary but doesn't solve the race; requests still arrive after SIGTERM |

The `preStop` sleep is the approach recommended by the Kubernetes documentation and is widely used in production environments.

## Dependencies

- Kubernetes rolling update strategy with `maxUnavailable: 0`
- An ingress controller that watches Service endpoints (NGINX ingress, Traefik, etc.)
