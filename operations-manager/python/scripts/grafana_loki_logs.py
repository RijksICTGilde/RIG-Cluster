#!/usr/bin/env python3
"""
Grafana Loki Log Query Script

Queries Loki logs through Grafana's API using a service account token.
Modeled on grafana_prometheus_test.py.

Usage:
    GRAFANA_TOKEN=... python grafana_loki_logs.py [options]

Examples:
    # Default: ERROR|WARN in operations-manager, last 24h
    python grafana_loki_logs.py

    # Errors over the last 3 days
    python grafana_loki_logs.py --from now-3d

    # Around a maintenance window (UTC)
    python grafana_loki_logs.py --from 2026-04-29T08:00:00Z --to 2026-04-29T12:00:00Z

    # Custom namespace + extra grep
    python grafana_loki_logs.py --namespace rig-system --grep "OOM|killed"

Environment variables:
    GRAFANA_URL: Grafana instance URL (default: https://grafana.rig.prd1.gn2.quattro.rijksapps.nl)
    GRAFANA_TOKEN: Service account token (will prompt if not set)
"""

import argparse
import json
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


def find_loki_datasource(token: str) -> dict | None:
    url = f"{GRAFANA_URL}/api/datasources"
    resp = requests.get(url, headers=get_headers(token), timeout=10)
    resp.raise_for_status()
    for ds in resp.json():
        if ds.get("type") == "loki":
            return ds
    return None


def build_logql(namespace: str, container: str, level: str, grep: str | None) -> str:
    expr = f'{{k8s_namespace_name="{namespace}", k8s_container_name="{container}"}}'
    if level:
        expr += f' |~ "{level}"'
    if grep:
        expr += f' |~ "{grep}"'
    return expr


def query_loki(token: str, datasource: dict, expr: str, time_from: str, time_to: str, limit: int) -> dict:
    url = f"{GRAFANA_URL}/api/ds/query"
    payload = {
        "queries": [
            {
                "refId": "A",
                "datasource": {"type": "loki", "uid": datasource["uid"]},
                "expr": expr,
                "queryType": "range",
                "maxLines": limit,
            }
        ],
        "from": time_from,
        "to": time_to,
    }
    resp = requests.post(url, headers=get_headers(token), json=payload, timeout=60)
    if resp.status_code != 200:
        print(f"Query failed: {resp.status_code} {resp.text[:500]}", file=sys.stderr)
        sys.exit(1)
    return resp.json()


def parse_frames(result: dict) -> list[tuple[int, dict, str]]:
    """Extract (timestamp_ms, labels, line) rows from Grafana data-plane frames."""
    rows: list[tuple[int, dict, str]] = []
    for ref_result in result.get("results", {}).values():
        for frame in ref_result.get("frames", []):
            schema = frame.get("schema", {})
            values = frame.get("data", {}).get("values", [])
            fields = schema.get("fields", [])

            time_idx = line_idx = labels_idx = None
            field_labels: dict = {}
            for i, f in enumerate(fields):
                fname = f.get("name", "")
                if fname == "Time" or f.get("type") == "time":
                    time_idx = i
                elif fname == "Line":
                    line_idx = i
                elif fname == "labels":
                    labels_idx = i
                if f.get("labels"):
                    field_labels.update(f["labels"])

            if time_idx is None or line_idx is None or not values:
                continue

            times = values[time_idx] if time_idx < len(values) else []
            lines = values[line_idx] if line_idx < len(values) else []
            label_col = values[labels_idx] if (labels_idx is not None and labels_idx < len(values)) else None

            for j in range(min(len(times), len(lines))):
                row_labels = dict(field_labels)
                if label_col is not None and j < len(label_col):
                    raw = label_col[j]
                    if isinstance(raw, dict):
                        row_labels.update(raw)
                rows.append((int(times[j]), row_labels, lines[j]))
    return rows


def format_ts(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def main() -> int:
    parser = argparse.ArgumentParser(description="Query Loki logs through Grafana")
    parser.add_argument("--namespace", default="rig-prd-operations")
    parser.add_argument("--container", default="operations-manager", help="k8s_container_name")
    parser.add_argument(
        "--level",
        default="ERROR|WARNING|CRITICAL",
        help="Line regex matching python log level keywords. Empty string to disable.",
    )
    parser.add_argument("--grep", default=None, help="Additional LogQL line filter")
    parser.add_argument("--from", dest="time_from", default="now-24h", help="Time range start (e.g. now-3d, RFC3339)")
    parser.add_argument("--to", dest="time_to", default="now", help="Time range end")
    parser.add_argument("--limit", type=int, default=1000, help="Max log lines (Loki cap 5000)")
    parser.add_argument(
        "--max-line", type=int, default=300, help="Truncate each message to N chars (0 = no truncation)"
    )
    parser.add_argument("--query", default=None, help="Override LogQL expression entirely")
    args = parser.parse_args()

    token = os.environ.get("GRAFANA_TOKEN")
    if not token:
        token = input("Enter service account token: ").strip()
    if not token:
        print("Error: No token provided", file=sys.stderr)
        return 1

    ds = find_loki_datasource(token)
    if not ds:
        print("No Loki datasource found in Grafana", file=sys.stderr)
        return 1
    print(f"Using datasource: {ds.get('name')} (uid: {ds.get('uid')})", file=sys.stderr)

    expr = args.query or build_logql(args.namespace, args.container, args.level, args.grep)
    print(f"LogQL: {expr}", file=sys.stderr)
    print(f"Range: {args.time_from} -> {args.time_to} (limit {args.limit})", file=sys.stderr)
    print("---", file=sys.stderr)

    result = query_loki(token, ds, expr, args.time_from, args.time_to, args.limit)
    rows = parse_frames(result)
    rows.sort(key=lambda r: r[0])

    pods_seen: set[str] = set()
    for ts, labels, line in rows:
        msg = line
        pod = "?"
        if line.startswith("{"):
            try:
                obj = json.loads(line)
                msg = obj.get("message") or obj.get("log") or line
                pod = obj.get("kubernetes", {}).get("pod_name", "?")
            except json.JSONDecodeError:
                pass
        pods_seen.add(pod)
        level = labels.get("detected_level", "?")
        if args.max_line and len(msg) > args.max_line:
            msg = msg[: args.max_line] + "...(truncated)"
        print(f"{format_ts(ts)} [{level}] {pod} {msg}".rstrip())

    print("---", file=sys.stderr)
    print(f"Matches: {len(rows)}, pods: {len(pods_seen)}", file=sys.stderr)
    if len(rows) >= args.limit:
        print(
            f"WARNING: hit limit ({args.limit}) - narrow the time range or filter",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
