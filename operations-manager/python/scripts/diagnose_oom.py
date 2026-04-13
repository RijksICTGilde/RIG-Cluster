#!/usr/bin/env python3
"""
OOM Kill Diagnostics Script

Queries Prometheus via Grafana to diagnose OOMKilled containers by checking
memory limits, usage patterns, high-water marks, and restart counts.

Usage:
    python diagnose_oom.py --namespace rig-prd-regel-k4c --pod regelrecht-enrichworker

Environment variables:
    GRAFANA_URL: Grafana instance URL (default: https://grafana.rig.prd1.gn2.quattro.rijksapps.nl)
    GRAFANA_TOKEN: Service account token (will prompt if not set)
"""

import argparse
import os
import sys
from datetime import UTC, datetime

import requests

GRAFANA_URL = os.environ.get("GRAFANA_URL", "https://grafana.rig.prd1.gn2.quattro.rijksapps.nl")


def get_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def find_prometheus_datasource(token: str) -> dict | None:
    """Discover Prometheus/Mimir datasource from Grafana."""
    resp = requests.get(f"{GRAFANA_URL}/api/datasources", headers=get_headers(token), timeout=10)
    if resp.status_code != 200:
        print(f"Failed to fetch datasources: {resp.status_code} {resp.text[:200]}")
        return None

    for ds in resp.json():
        if ds.get("type") in ("prometheus", "mimir"):
            print(f"Using datasource: {ds['name']} (uid: {ds['uid']}, type: {ds['type']})")
            return ds

    print("No Prometheus/Mimir datasource found.")
    return None


def query_instant(token: str, ds: dict, expr: str) -> list[dict]:
    """Execute an instant Prometheus query via Grafana."""
    payload = {
        "queries": [
            {
                "refId": "A",
                "datasource": {"type": ds["type"], "uid": ds["uid"]},
                "expr": expr,
                "instant": True,
                "range": False,
            }
        ],
        "from": "now-5m",
        "to": "now",
    }
    resp = requests.post(f"{GRAFANA_URL}/api/ds/query", headers=get_headers(token), json=payload, timeout=30)
    if resp.status_code != 200:
        return []

    frames = []
    for ref_result in resp.json().get("results", {}).values():
        frames.extend(ref_result.get("frames", []))
    return frames


def query_range(token: str, ds: dict, expr: str, duration_hours: int = 6, step_minutes: int = 5) -> list[dict]:
    """Execute a range Prometheus query via Grafana."""
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    from_ms = now_ms - (duration_hours * 3600 * 1000)

    payload = {
        "queries": [
            {
                "refId": "A",
                "datasource": {"type": ds["type"], "uid": ds["uid"]},
                "expr": expr,
                "range": True,
                "instant": False,
                "intervalMs": step_minutes * 60 * 1000,
                "maxDataPoints": (duration_hours * 60) // step_minutes,
            }
        ],
        "from": str(from_ms),
        "to": str(now_ms),
    }
    resp = requests.post(f"{GRAFANA_URL}/api/ds/query", headers=get_headers(token), json=payload, timeout=30)
    if resp.status_code != 200:
        return []

    frames = []
    for ref_result in resp.json().get("results", {}).values():
        frames.extend(ref_result.get("frames", []))
    return frames


def extract_instant_value(frames: list[dict]) -> float | None:
    """Extract a single numeric value from instant query frames."""
    for frame in frames:
        values = frame.get("data", {}).get("values", [])
        if values and len(values) > 1 and values[1]:
            return float(values[1][0])
    return None


def extract_labels_and_value(frames: list[dict]) -> list[tuple[dict, float]]:
    """Extract (labels, value) pairs from instant query frames."""
    results = []
    for frame in frames:
        schema = frame.get("schema", {})
        data = frame.get("data", {})
        labels = {}
        for field in schema.get("fields", []):
            labels.update(field.get("labels", {}))
        values = data.get("values", [])
        if values and len(values) > 1 and values[1]:
            results.append((labels, float(values[1][0])))
    return results


def extract_timeseries(frames: list[dict]) -> list[tuple[float, float]]:
    """Extract (timestamp, value) pairs from range query frames."""
    points = []
    for frame in frames:
        values = frame.get("data", {}).get("values", [])
        if values and len(values) > 1:
            timestamps = values[0]
            vals = values[1]
            for ts, val in zip(timestamps, vals, strict=False):
                if val is not None:
                    points.append((ts, float(val)))
    return sorted(points)


def fmt_bytes(b: float | None) -> str:
    if b is None:
        return "N/A"
    if b >= 1024**3:
        return f"{b / 1024**3:.2f} GB"
    return f"{b / 1024**2:.1f} MB"


def main() -> int:
    parser = argparse.ArgumentParser(description="Diagnose OOMKilled containers via Prometheus/Grafana")
    parser.add_argument("--namespace", "-n", required=True, help="Kubernetes namespace")
    parser.add_argument("--pod", "-p", required=True, help="Pod name prefix (e.g. regelrecht-enrichworker)")
    parser.add_argument("--hours", type=int, default=6, help="Hours of history for range queries (default: 6)")
    parser.add_argument("--step", type=int, default=2, help="Step in minutes for range queries (default: 2)")
    args = parser.parse_args()

    ns = args.namespace
    pod = args.pod

    print("=" * 60)
    print(f"OOM Diagnostics: {pod} in {ns}")
    print("=" * 60)
    print(f"Target: {GRAFANA_URL}")

    token = os.environ.get("GRAFANA_TOKEN")
    if not token:
        token = input("\nEnter Grafana service account token: ").strip()
    if not token:
        print("Error: No token provided")
        return 1

    ds = find_prometheus_datasource(token)
    if not ds:
        return 1

    # 1. Memory limit
    print("\n--- Memory Limit ---")
    frames = query_instant(
        token,
        ds,
        f'kube_container_resource_limits{{namespace="{ns}",pod=~"{pod}.*",resource="memory"}}',
    )
    for labels, val in extract_labels_and_value(frames):
        container = labels.get("container", "?")
        print(f"  {container}: limit = {fmt_bytes(val)}")
    if not frames:
        # Try alternative metric name
        frames = query_instant(
            token,
            ds,
            f'kube_pod_container_resource_limits{{namespace="{ns}",pod=~"{pod}.*",resource="memory"}}',
        )
        for labels, val in extract_labels_and_value(frames):
            container = labels.get("container", "?")
            print(f"  {container}: limit = {fmt_bytes(val)}")
    if not frames:
        print("  No limit data found (metric may not be available)")

    # 2. Memory request
    print("\n--- Memory Request ---")
    frames = query_instant(
        token,
        ds,
        f'kube_container_resource_requests{{namespace="{ns}",pod=~"{pod}.*",resource="memory"}}',
    )
    for labels, val in extract_labels_and_value(frames):
        container = labels.get("container", "?")
        print(f"  {container}: request = {fmt_bytes(val)}")
    if not frames:
        print("  No request data found")

    # 3. Current memory working set
    print("\n--- Current Memory (working set) ---")
    frames = query_instant(
        token,
        ds,
        f'container_memory_working_set_bytes{{namespace="{ns}",pod=~"{pod}.*",container!=""}}',
    )
    for labels, val in extract_labels_and_value(frames):
        container = labels.get("container", "?")
        print(f"  {container}: {fmt_bytes(val)}")
    if not frames:
        print("  No data (pod may not be running)")

    # 4. Max memory usage (high-water mark)
    print("\n--- Max Memory Usage (high-water mark, if available) ---")
    frames = query_instant(
        token,
        ds,
        f'container_memory_max_usage_bytes{{namespace="{ns}",pod=~"{pod}.*",container!=""}}',
    )
    for labels, val in extract_labels_and_value(frames):
        container = labels.get("container", "?")
        print(f"  {container}: max = {fmt_bytes(val)}")
    if not frames:
        print("  Metric not available (cgroup v2 may not expose this)")

    # 5. RSS vs working set (to see if kernel cache is a factor)
    print("\n--- Current RSS (resident set size) ---")
    frames = query_instant(
        token,
        ds,
        f'container_memory_rss{{namespace="{ns}",pod=~"{pod}.*",container!=""}}',
    )
    for labels, val in extract_labels_and_value(frames):
        container = labels.get("container", "?")
        print(f"  {container}: RSS = {fmt_bytes(val)}")
    if not frames:
        print("  Metric not available")

    # 6. Restart count
    print("\n--- Restart Count ---")
    frames = query_instant(
        token,
        ds,
        f'kube_pod_container_status_restarts_total{{namespace="{ns}",pod=~"{pod}.*"}}',
    )
    for labels, val in extract_labels_and_value(frames):
        container = labels.get("container", "?")
        print(f"  {container}: {int(val)} restarts")
    if not frames:
        print("  No restart data found")

    # 7. Last termination reason
    print("\n--- Last Termination Reason ---")
    frames = query_instant(
        token,
        ds,
        f'kube_pod_container_status_last_terminated_reason{{namespace="{ns}",pod=~"{pod}.*"}}',
    )
    for labels, val in extract_labels_and_value(frames):
        container = labels.get("container", "?")
        reason = labels.get("reason", "?")
        if val == 1:
            print(f"  {container}: {reason}")
    if not frames:
        print("  No termination data found")

    # 8. Memory time-series (to spot the growth pattern)
    print(f"\n--- Memory Over Last {args.hours}h (step={args.step}m) ---")
    points = extract_timeseries(
        query_range(
            token,
            ds,
            f'sum(container_memory_working_set_bytes{{namespace="{ns}",pod=~"{pod}.*",container!=""}}) by (pod)',
            duration_hours=args.hours,
            step_minutes=args.step,
        )
    )

    if points:
        values = [v for _, v in points]
        min_mem = min(values)
        max_mem = max(values)
        last_mem = values[-1]

        print(f"  Data points: {len(points)}")
        print(f"  Min:     {fmt_bytes(min_mem)}")
        print(f"  Max:     {fmt_bytes(max_mem)}")
        print(f"  Current: {fmt_bytes(last_mem)}")
        print(f"  Growth:  {fmt_bytes(max_mem - min_mem)} (max - min)")

        # Show last 10 data points
        print(f"\n  Last {min(10, len(points))} data points:")
        for ts, val in points[-10:]:
            dt = datetime.fromtimestamp(ts / 1000, tz=UTC).strftime("%H:%M")
            print(f"    {dt}  {fmt_bytes(val)}")
    else:
        print("  No time-series data returned")

    # 9. OOM kill events from kube-state-metrics
    print("\n--- OOM Kill Count (kube-state-metrics) ---")
    frames = query_instant(
        token,
        ds,
        f'kube_pod_container_status_last_terminated_reason{{namespace="{ns}",pod=~"{pod}.*",reason="OOMKilled"}}',
    )
    if frames:
        for labels, val in extract_labels_and_value(frames):
            container = labels.get("container", "?")
            print(f"  {container}: OOMKilled = {'yes' if val == 1 else 'no'}")
    else:
        print("  No OOM kill data from kube-state-metrics")

    print("\n" + "=" * 60)
    print("Diagnostics complete.")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
