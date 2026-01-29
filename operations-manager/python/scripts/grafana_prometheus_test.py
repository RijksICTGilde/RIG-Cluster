#!/usr/bin/env python3
"""
Grafana Prometheus Query Test Script

Tests querying Prometheus metrics through Grafana's API using a service account token.
This is simpler than OAuth authentication and suitable for programmatic access.

Usage:
    python grafana_prometheus_test.py

Environment variables (optional):
    GRAFANA_URL: Grafana instance URL (default: https://grafana.rig.prd1.gn2.quattro.rijksapps.nl)
    GRAFANA_TOKEN: Service account token (will prompt if not set)
"""

import os
import sys

import requests


GRAFANA_URL = os.environ.get("GRAFANA_URL", "https://grafana.rig.prd1.gn2.quattro.rijksapps.nl")


def get_headers(token: str) -> dict[str, str]:
    """Create headers with Bearer token authentication."""
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def test_connection(token: str) -> bool:
    """Test basic API connectivity and token validity."""
    print("\n=== Testing API Connection ===")

    url = f"{GRAFANA_URL}/api/user"
    headers = get_headers(token)

    try:
        resp = requests.get(url, headers=headers, timeout=10)
        print(f"Status: {resp.status_code}")

        if resp.status_code == 200:
            user_info = resp.json()
            print(f"Authenticated as: {user_info.get('login', 'unknown')}")
            print(f"User ID: {user_info.get('id', 'unknown')}")
            return True
        else:
            print(f"Authentication failed: {resp.text[:200]}")
            return False
    except requests.RequestException as e:
        print(f"Connection error: {e}")
        return False


def get_datasources(token: str) -> list[dict] | None:
    """Fetch all datasources and find Prometheus/Mimir."""
    print("\n=== Fetching Datasources ===")

    url = f"{GRAFANA_URL}/api/datasources"
    headers = get_headers(token)

    try:
        resp = requests.get(url, headers=headers, timeout=10)
        print(f"Status: {resp.status_code}")

        if resp.status_code != 200:
            print(f"Failed to fetch datasources: {resp.text[:200]}")
            return None

        datasources = resp.json()
        print(f"Found {len(datasources)} datasource(s):")

        for ds in datasources:
            ds_type = ds.get("type", "")
            ds_name = ds.get("name", "")
            ds_uid = ds.get("uid", "N/A")
            is_default = ds.get("isDefault", False)
            default_marker = " (default)" if is_default else ""
            print(f"  - {ds_name} [type: {ds_type}, uid: {ds_uid}]{default_marker}")

        return datasources
    except requests.RequestException as e:
        print(f"Error fetching datasources: {e}")
        return None


def find_prometheus_datasource(datasources: list[dict]) -> dict | None:
    """Find the Prometheus or Mimir datasource."""
    for ds in datasources:
        ds_type = ds.get("type", "")
        if ds_type in ("prometheus", "mimir"):
            return ds
    return None


def query_prometheus(token: str, datasource: dict, query: str = "up") -> dict | None:
    """Execute a Prometheus query through Grafana's datasource API."""
    print(f"\n=== Querying Prometheus: {query} ===")

    url = f"{GRAFANA_URL}/api/ds/query"
    headers = get_headers(token)

    ds_uid = datasource.get("uid")
    ds_type = datasource.get("type")

    payload = {
        "queries": [
            {
                "refId": "A",
                "datasource": {
                    "type": ds_type,
                    "uid": ds_uid,
                },
                "expr": query,
                "instant": True,
                "range": False,
            }
        ],
        "from": "now-5m",
        "to": "now",
    }

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=30)
        print(f"Status: {resp.status_code}")

        if resp.status_code != 200:
            print(f"Query failed: {resp.text[:500]}")
            return None

        result = resp.json()
        return result
    except requests.RequestException as e:
        print(f"Error executing query: {e}")
        return None


def print_query_results(result: dict, max_results: int = 15) -> None:
    """Pretty print query results with labels."""
    if not result:
        return

    results = result.get("results", {})

    for ref_result in results.values():
        frames = ref_result.get("frames", [])
        if not frames:
            print("  No data returned")
            continue

        print(f"  Found {len(frames)} series:")

        for frame in frames[:max_results]:
            schema = frame.get("schema", {})
            data = frame.get("data", {})

            # Extract labels from schema
            labels = {}
            fields = schema.get("fields", [])
            for field in fields:
                field_labels = field.get("labels", {})
                if field_labels:
                    labels.update(field_labels)

            # Get the value (usually last field, last value)
            values = data.get("values", [])
            value = None
            if values and len(values) > 1 and values[1]:
                value = values[1][0]  # Second column (Value), first row

            # Format labels
            if labels:
                label_str = ", ".join(f"{k}={v}" for k, v in sorted(labels.items()))
                print(f"    [{label_str}] = {value}")
            else:
                print(f"    value = {value}")

        if len(frames) > max_results:
            print(f"    ... and {len(frames) - max_results} more series")


def main() -> int:
    print("=" * 60)
    print("Grafana Prometheus Query Test")
    print("=" * 60)
    print(f"Target: {GRAFANA_URL}")

    # Get token from environment or prompt
    token = os.environ.get("GRAFANA_TOKEN")
    if not token:
        token = input("\nEnter service account token: ").strip()

    if not token:
        print("Error: No token provided")
        return 1

    # Step 1: Test connection
    if not test_connection(token):
        print("\nConnection test failed. Check your token.")
        return 1

    # Step 2: Get datasources
    datasources = get_datasources(token)
    if not datasources:
        print("\nFailed to fetch datasources.")
        return 1

    # Step 3: Find Prometheus datasource
    prometheus_ds = find_prometheus_datasource(datasources)
    if not prometheus_ds:
        print("\nNo Prometheus/Mimir datasource found.")
        return 1

    print(f"\nUsing datasource: {prometheus_ds.get('name')} (uid: {prometheus_ds.get('uid')})")

    # Step 4: Execute test queries
    test_queries = [
        ("up", "Basic connectivity check"),
        # Resource limits queries (from prometheus.py connector)
        ('sum(kube_pod_container_resource_limits{resource="cpu"}) by (namespace, pod)', "CPU limits by pod"),
        (
            'sum(kube_pod_container_resource_limits{resource="memory"}) by (namespace, pod)',
            "Memory limits by pod (bytes)",
        ),
        # Resource usage queries
        (
            'sum(rate(container_cpu_usage_seconds_total{container!=""}[5m])) by (namespace, pod)',
            "CPU usage by pod (cores)",
        ),
        ('sum(container_memory_usage_bytes{container!=""}) by (namespace, pod)', "Memory usage by pod (bytes)"),
        # Cluster overview
        ("count(kube_pod_info)", "Total pod count"),
    ]

    for query, description in test_queries:
        print(f"\n--- {description} ---")
        result = query_prometheus(token, prometheus_ds, query)
        if result:
            print_query_results(result)

    print("\n" + "=" * 60)
    print("Test completed successfully!")
    print("=" * 60)

    return 0


if __name__ == "__main__":
    sys.exit(main())
