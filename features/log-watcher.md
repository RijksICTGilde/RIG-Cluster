# Log watcher

Periodic triage of this OPI instance's own production logs, pushing an
[ntfy](https://ntfy.sh) notification whenever something unexpected and
not-yet-alerted shows up. It runs as a background async loop inside the
Operations Manager, so there is nothing extra to deploy.

## What it does

Every `LOGWATCHER_INTERVAL_SECONDS` (default 30 min) one cycle:

1. Queries Loki (via the Grafana datasource API) for `ERROR|WARNING|CRITICAL`
   lines in the OPI container over a `now-35m` window.
2. Drops every line matching the ignore-list of known-benign noise
   (`operations-manager/python/opi/services/log_watch_ignore_patterns.txt`).
3. Deduplicates the remainder against an in-memory map of recently-alerted
   signatures (`LOGWATCHER_DEDUP_HOURS`, default 6h), so an ongoing issue is not
   re-alerted every cycle. The map is in-memory: an OPI restart at worst repeats
   one alert.
4. If anything new remains, builds a compact, severity-ordered notification body
   (`module: message` per problem, top 5) and POSTs it to the ntfy topic. A clean
   run is silent but still logged.

There is **no LLM in the pod**: the notification body is built by deterministic
formatting. The standalone CLI (below) additionally runs a Claude triage step for
richer grouping, but the in-app loop deliberately does not.

## Configuration

All settings are OPI env vars (inject secrets via the SOPS env secret, exactly
like `GRAFANA_TOKEN`). It reuses the existing `GRAFANA_URL` / `GRAFANA_TOKEN` /
`GRAFANA_DATASOURCE_UID`.

| Setting | Default | Meaning |
|---|---|---|
| `LOGWATCHER_ENABLED` | `False` | The on/off "start" flag. Opt-in. |
| `LOGWATCHER_INTERVAL_SECONDS` | `1800` | Seconds between cycles (30 min). |
| `LOGWATCHER_NTFY_TOPIC` | `None` | Secret, unguessable ntfy topic (treat like a password). Required. |
| `LOGWATCHER_NTFY_SERVER` | `https://ntfy.sh` | ntfy server. |
| `LOGWATCHER_NAMESPACE` | `rig-prd-operations` | Namespace whose OPI logs to scan. |
| `LOGWATCHER_CONTAINER` | `operations-manager` | Container name in the Loki labels. |
| `LOGWATCHER_WINDOW` | `now-35m` | Loki look-back per run. |
| `LOGWATCHER_DEDUP_HOURS` | `6.0` | Do not re-alert the same signature within this window. |

The loop only starts when `LOGWATCHER_ENABLED` is true, and each sweep is skipped
(with a warning) if `GRAFANA_TOKEN` or `LOGWATCHER_NTFY_TOPIC` is missing.

Subscribe to the same topic in the ntfy phone app to receive alerts.

## Architecture

The pipeline lives in `opi/services/log_watcher.py` (`run_cycle`) and is the
**single source of truth** shared by both entrypoints:

- `opi/core/logwatcher_scheduler.py` - the in-app async loop. Starts/stops in the
  FastAPI lifespan (`opi/server.py`), mirroring the other schedulers. The pipeline
  is synchronous (httpx), so each sweep is offloaded with `asyncio.to_thread` to
  keep the event loop free. Injects `triage_fn=None` and an in-memory dedup dict.
- `scripts/log_watch/watch.py` - the standalone CLI for ad-hoc local runs. Injects
  the Claude triage step, a SOPS-decrypted token, and file-based dedup state
  (`state.json`), then calls the same `run_cycle`. See `scripts/log_watch/` for its
  `config.py` (copy `config.example.py`).

## Dependencies

- Loki reachable through the Grafana datasource API, and a Grafana service-account
  token with datasource query access (`GRAFANA_TOKEN`).
- An ntfy topic (public `ntfy.sh` or self-hosted).
- `httpx` (already an OPI dependency).
