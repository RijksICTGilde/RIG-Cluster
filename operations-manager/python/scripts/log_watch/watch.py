#!/usr/bin/env python3
"""
log_watch - periodic OPI production-log triage (standalone CLI, verbose).

Usage:
  uv run python scripts/log_watch/watch.py            # one cycle (or: task log-watch)
  uv run python scripts/log_watch/watch.py --loop     # run forever, one cycle / 30 min
  uv run python scripts/log_watch/watch.py --help     # all flags

The pipeline itself lives in opi.services.log_watcher.run_cycle - the SAME code the
in-app LogwatcherScheduler runs. This CLI only supplies the standalone-specific
bits: config.py, the SOPS-decrypted Grafana token, file-based dedup state, and the
Claude triage step (the in-app loop runs the identical pipeline without Claude).

Pipeline (one cycle):
  1. Query Loki for ERROR/WARNING/CRITICAL in the OPI container over a window.
  2. Drop every line matching the ignore-list (opi/services/log_watch_ignore_patterns.txt).
  3. Deduplicate the remainder against prior runs (state.json).
  4. If something is left, spawn a headless Claude session to triage and push an
     ntfy notification. A clean run is silent but still logs what it checked.

Configuration:
  Copy config.example.py to config.py (gitignored) and edit it - ntfy topic/server,
  namespace, window, loop interval, morning catch-up, dedup window, Claude on/off.
  The Grafana token is the only secret: read from the GRAFANA_TOKEN env var, else
  decrypted from the SOPS secret. GRAFANA_URL comes from the env var (prod default).
  A few settings can be overridden per-run via CLI flags: --loop, --interval,
  --window, --no-claude, --no-ntfy.
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
REPO_ROOT = Path(__file__).resolve().parents[4]
PYTHON_ROOT = Path(__file__).resolve().parents[2]  # operations-manager/python (has the opi package)
STATE_FILE = HERE / "state.json"
SOPS_SECRET = (
    REPO_ROOT
    / "bootstrap/rig-system/kustomize/operations-manager/overlays/odcn-production/operations-manager-env-secrets.yaml"
)
AGE_KEY_FILE = REPO_ROOT / "security/key.txt"
GRAFANA_URL = os.environ.get("GRAFANA_URL", "https://grafana.rig.prd1.gn2.quattro.rijksapps.nl")

# Import the shared pipeline (single source of truth with the in-app scheduler).
sys.path.insert(0, str(PYTHON_ROOT))
from opi.services.log_watcher import (  # noqa: E402  # pyright: ignore[reportMissingImports]
    LogWatchConfig,
    run_cycle,
)

# --- Config: defaults live in config.py (edit that file). A few are overridable
#     per-run via CLI flags in main(). The Grafana token is the only secret and
#     is read from env/SOPS, not config.py. ---
sys.path.insert(0, str(HERE))
try:
    import config as cfg  # pyright: ignore[reportMissingImports]
except ModuleNotFoundError as e:
    raise SystemExit("config.py not found - run: cp config.example.py config.py  (then edit it)") from e

WINDOW = cfg.WINDOW
USE_CLAUDE = cfg.USE_CLAUDE
SEND_NTFY = cfg.SEND_NTFY
CLAUDE_TIMEOUT = cfg.CLAUDE_TIMEOUT
LOOP_INTERVAL = cfg.LOOP_INTERVAL
MORNING_BEFORE_HOUR = getattr(cfg, "MORNING_BEFORE_HOUR", 9)  # runs before this local hour catch up further back
MORNING_WINDOW = getattr(cfg, "MORNING_WINDOW", "now-16h")  # look-back for those early runs (~5pm prev day)
WINDOW_OVERRIDDEN = False  # set when --window is passed

log = logging.getLogger("log-watch")


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )


def load_token() -> str:
    """Grafana token from env, or decrypted from the SOPS secret (prod AGE key)."""
    tok = os.environ.get("GRAFANA_TOKEN")
    if tok:
        return tok.strip()
    age_key = AGE_KEY_FILE.read_text().splitlines()[2].strip()
    out = subprocess.run(  # noqa: S603
        ["sops", "--decrypt", str(SOPS_SECRET)],  # noqa: S607
        capture_output=True,
        text=True,
        env={**os.environ, "SOPS_AGE_KEY": age_key},
    )
    if out.returncode != 0:
        raise SystemExit(f"could not decrypt GRAFANA_TOKEN: {out.stderr[:300]}")
    data = yaml.safe_load(out.stdout)
    val = (data.get("stringData") or {}).get("GRAFANA_TOKEN")
    if not val and (data.get("data") or {}).get("GRAFANA_TOKEN"):
        val = base64.b64decode(data["data"]["GRAFANA_TOKEN"]).decode()
    if not val:
        raise SystemExit("GRAFANA_TOKEN not found in secret")
    return val.strip()


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True))


def run_claude(samples: list[str]) -> str:
    prompt = (
        "You are triaging PRODUCTION logs for the RIG-Cluster Operations Manager (OPI), "
        "a FastAPI GitOps controller. The lines below are ERROR/WARNING entries NOT in our "
        "known-issues ignore-list, so they are unexpected. Group related lines into distinct "
        "problems. Respond in EXACTLY this format and nothing else:\n\n"
        "SUMMARY:\n"
        "<one short plain-language line per distinct problem (max 5), ordered most-severe first. "
        "Start each line with the severity in caps (CRITICAL/HIGH/MEDIUM/LOW), then what is broken "
        "and the affected project/component, so an on-call engineer instantly understands it. "
        "ASCII only, no markdown, no task IDs.>\n\n"
        "DETAIL:\n"
        "<for each problem: most likely cause and the single most useful next action. You may "
        "use read-only kubectl/Loki to verify, but change nothing.>\n\n"
        "SUGGESTED IGNORES:\n"
        "<ONLY for problems above that are clearly benign recurring noise (never a real issue): "
        "one conservative, well-scoped Python regex per line that matches that log message, to "
        "paste into our ignore-list. Prefer the module name plus a distinctive phrase, and escape "
        "regex metacharacters. If nothing is safely ignorable, write 'none'.>\n\n" + "\n".join(samples[:80])
    )
    try:
        out = subprocess.run(  # noqa: S603
            ["claude", "-p", prompt],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=CLAUDE_TIMEOUT,
            cwd=str(REPO_ROOT),
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
        return f"(claude triage failed rc={out.returncode}: {out.stderr.strip()[:200]})"
    except FileNotFoundError:
        return "(claude CLI not found - skipping triage)"
    except subprocess.TimeoutExpired:
        return "(claude triage timed out)"


def extract_summary(verdict: str) -> str:
    """Pull the SUMMARY block out of Claude's response for the ntfy body."""
    if verdict.startswith("(claude"):  # triage failed/disabled marker
        return ""
    m = re.search(r"SUMMARY:\s*(.*?)(?:\n\s*DETAIL:|\Z)", verdict, re.DOTALL | re.IGNORECASE)
    text = (m.group(1) if m else verdict).strip()
    lines = [" ".join(ln.split())[:200] for ln in text.splitlines() if ln.strip()][:6]
    return "\n".join(lines)[:1800]


def extract_suggested_ignores(verdict: str) -> list[str]:
    """Pull Claude's SUGGESTED IGNORES regexes (compile-checked).

    These are only logged for human review - never auto-applied to the ignore-list.
    """
    m = re.search(r"SUGGESTED IGNORES:\s*(.*)\Z", verdict, re.DOTALL | re.IGNORECASE)
    if not m:
        return []
    out: list[str] = []
    for ln in m.group(1).splitlines():
        pat = re.sub(r"^\s*(?:[-*]|\d+\.)\s+", "", ln).strip()  # strip list markers, keep the regex
        if not pat or pat.lower() == "none":
            continue
        try:
            re.compile(pat)
        except re.error:
            continue  # skip anything that is not a valid regex
        out.append(pat)
    return out


def _triage(samples: list[str]) -> str:
    """Claude triage step injected into the shared pipeline (CLI only)."""
    if not USE_CLAUDE:
        log.info("Claude triage disabled (USE_CLAUDE=False / --no-claude)")
        return ""
    log.info("running Claude triage (timeout %ds)...", CLAUDE_TIMEOUT)
    verdict = run_claude(samples)
    log.info("Claude triage:\n%s", verdict)
    suggested = extract_suggested_ignores(verdict)
    if suggested:
        log.info(
            "Claude suggests ignore-pattern(s) - REVIEW, then paste good ones into "
            "log_watch_ignore_patterns.txt (not auto-applied):\n%s",
            "\n".join("  " + s for s in suggested),
        )
    return extract_summary(verdict)


def build_config() -> LogWatchConfig:
    return LogWatchConfig(
        grafana_url=GRAFANA_URL,
        ntfy_topic=cfg.NTFY_TOPIC,
        ntfy_server=cfg.NTFY_SERVER,
        namespace=cfg.NAMESPACE,
        container=cfg.CONTAINER,
        # level intentionally left at the shared default (error+ only, via detected_level).
        window=WINDOW,
        dedup_hours=cfg.DEDUP_HOURS,
        max_lines=cfg.MAX_LINES,
        # --window disables the morning catch-up (an explicit look-back was asked for).
        morning_before_hour=None if WINDOW_OVERRIDDEN else MORNING_BEFORE_HOUR,
        morning_window=None if WINDOW_OVERRIDDEN else MORNING_WINDOW,
    )


def one_cycle() -> int:
    token = load_token()
    state = load_state()
    rc = run_cycle(build_config(), token, state, triage_fn=_triage, send_notification=SEND_NTFY)
    save_state(state)
    return rc


def main() -> int:
    global WINDOW, WINDOW_OVERRIDDEN, USE_CLAUDE, SEND_NTFY
    parser = argparse.ArgumentParser(description="OPI production-log triage watcher")
    parser.add_argument(
        "--loop",
        action="store_true",
        help=f"run forever, one cycle every --interval seconds (default {LOOP_INTERVAL})",
    )
    parser.add_argument("--interval", type=int, default=LOOP_INTERVAL, help="seconds between cycles in --loop mode")
    parser.add_argument(
        "--window", help="override the Loki look-back for this run (e.g. now-16h); disables morning catch-up"
    )
    parser.add_argument("--no-claude", action="store_true", help="skip the Claude triage")
    parser.add_argument("--no-ntfy", action="store_true", help="do not send ntfy (dry-run)")
    args = parser.parse_args()

    setup_logging()
    if args.window:
        WINDOW = args.window
        WINDOW_OVERRIDDEN = True
    if args.no_claude:
        USE_CLAUDE = False
    if args.no_ntfy:
        SEND_NTFY = False

    if not args.loop:
        return one_cycle()

    log.info("loop mode: one cycle every %d min (Ctrl-C to stop)", args.interval // 60)
    try:
        while True:
            try:
                one_cycle()
            except Exception:
                log.exception("cycle failed - continuing to next cycle")
            log.info("sleeping %d min until next cycle", args.interval // 60)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        log.info("stopped")
        return 0


if __name__ == "__main__":
    sys.exit(main())
