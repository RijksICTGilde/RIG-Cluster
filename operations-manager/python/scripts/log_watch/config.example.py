"""
log_watch configuration TEMPLATE.

Copy this to config.py (same directory) and edit it - config.py is gitignored so
your real ntfy topic never lands in the repo:

    cp config.example.py config.py

The Grafana token is NOT here - it is read from the GRAFANA_TOKEN env var or
decrypted from the SOPS secret, so no secret lives in this file.

CLI flags override a few of these for one-off runs - see `watch.py --help`.
"""

# --- ntfy notification ------------------------------------------------------
# CHANGE THIS to your own secret, unguessable topic. An ntfy.sh topic is public
# to anyone who knows the name, so treat it like a password. Subscribe to the
# same topic in the ntfy phone app (server below) to receive alerts.
NTFY_TOPIC = "rig-opi-watch-change-me"
NTFY_SERVER = "https://ntfy.sh"

# --- what to scan -----------------------------------------------------------
NAMESPACE = "rig-prd-operations"
CONTAINER = "operations-manager"
LEVEL = "ERROR|WARNING|CRITICAL"  # Loki line filter (regex of log-level words)
WINDOW = "now-35m"  # look-back per run (35m = 30m cadence + 5m overlap)

# --- behaviour --------------------------------------------------------------
LOOP_INTERVAL = 1800  # seconds between runs in --loop mode (1800 = 30 min)
OFFICE_HOURS = True  # True = only act Mon-Fri 09-17 local; False = run any time
DEDUP_HOURS = 6.0  # do not re-alert the same signature within this many hours
USE_CLAUDE = True  # spawn `claude -p` to triage whatever is left after the ignore-list
SEND_NTFY = True  # actually POST to ntfy (False = dry-run: log only)
CLAUDE_TIMEOUT = 240  # seconds to wait for the Claude triage
MAX_LINES = 5000  # Loki fetch cap per run
