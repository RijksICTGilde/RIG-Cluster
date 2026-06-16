#!/usr/bin/env python3
"""
log_watch - periodic OPI production-log triage (single command, verbose).

Usage:
  uv run python scripts/log_watch/watch.py            # one cycle (or: task log-watch)
  uv run python scripts/log_watch/watch.py --loop     # run forever, one cycle / 30 min
  uv run python scripts/log_watch/watch.py --help     # all flags

It narrates each step to stdout so you can see exactly what it did: how many
lines it scanned, how many the ignore-list dropped, what is left, whether Claude
was needed, and whether an ntfy was sent.

Pipeline (one cycle):
  1. Query Loki for ERROR/WARNING/CRITICAL in the OPI container over a window.
  2. Drop every line matching the ignore-list (ignore_patterns.txt) - known noise.
  3. Deduplicate the remainder against prior runs (state.json) so the same ongoing
     issue is not re-alerted every cycle.
  4. If (and only if) something is left, summarize it, spawn a headless Claude
     session to triage, and push an ntfy notification. A clean run is silent
     (no Claude, no notification) but still logs what it checked.

Configuration:
  Copy config.example.py to config.py (gitignored) and edit it - ntfy topic/server,
  namespace, window, loop interval, morning catch-up, dedup window, Claude on/off.
  Runs every cycle (no office-hours gate); runs before MORNING_BEFORE_HOUR use the
  wider MORNING_WINDOW so overnight issues are not missed.
  The Grafana token is the only secret: read from the GRAFANA_TOKEN env var, else
  decrypted from the SOPS secret. A few settings can be overridden per-run via CLI
  flags: --loop, --interval, --window, --no-claude, --no-ntfy.
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
from datetime import UTC, datetime, timedelta
from pathlib import Path

import requests
import yaml

# Reuse the Loki query plumbing from the sibling script.
SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))
from grafana_loki_logs import (  # noqa: E402  # pyright: ignore[reportMissingImports]
    build_logql,
    find_loki_datasource,
    parse_frames,
    query_loki,
)

HERE = Path(__file__).resolve().parent
REPO_ROOT = Path(__file__).resolve().parents[4]
IGNORE_FILE = HERE / "ignore_patterns.txt"
STATE_FILE = HERE / "state.json"
SOPS_SECRET = (
    REPO_ROOT
    / "bootstrap/rig-system/kustomize/operations-manager/overlays/odcn-production/operations-manager-env-secrets.yaml"
)
AGE_KEY_FILE = REPO_ROOT / "security/key.txt"

# --- Config: defaults live in config.py (edit that file). A few are overridable
#     per-run via CLI flags in main(). The Grafana token is the only secret and
#     is read from env/SOPS, not config.py. ---
sys.path.insert(0, str(HERE))
try:
    import config as cfg  # pyright: ignore[reportMissingImports]
except ModuleNotFoundError as e:
    raise SystemExit("config.py not found - run: cp config.example.py config.py  (then edit it)") from e

NTFY_TOPIC = cfg.NTFY_TOPIC
NTFY_SERVER = cfg.NTFY_SERVER.rstrip("/")
WINDOW = cfg.WINDOW
NAMESPACE = cfg.NAMESPACE
CONTAINER = cfg.CONTAINER
LEVEL = cfg.LEVEL
USE_CLAUDE = cfg.USE_CLAUDE
SEND_NTFY = cfg.SEND_NTFY
DEDUP_HOURS = cfg.DEDUP_HOURS
CLAUDE_TIMEOUT = cfg.CLAUDE_TIMEOUT
LOOP_INTERVAL = cfg.LOOP_INTERVAL
LIMIT = cfg.MAX_LINES
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


def load_ignore_patterns() -> list[re.Pattern]:
    pats: list[re.Pattern] = []
    for ln in IGNORE_FILE.read_text().splitlines():
        ln = ln.strip()
        if ln and not ln.startswith("#"):
            pats.append(re.compile(ln))
    return pats


def extract_msg(line: str) -> str:
    if line.startswith("{"):
        try:
            obj = json.loads(line)
            return obj.get("message") or obj.get("log") or line
        except json.JSONDecodeError:
            return line
    return line


def severity(msg: str) -> int:
    if "CRITICAL" in msg:
        return 2
    if "- ERROR -" in msg or re.search(r"\bERROR\b", msg):
        return 1
    return 0


def ascii_short(s: str, n: int = 150) -> str:
    """ASCII-only, single-line, trimmed - safe and compact for an ntfy line."""
    return " ".join(s.encode("ascii", "ignore").decode().split())[:n]


def human(msg: str) -> str:
    """Reduce a full log line to 'module: message' for a compact alert."""
    mod = re.search(r"(opi\.[\w.]+) - (?:ERROR|WARNING|CRITICAL)", msg)
    name = mod.group(1).split(".")[-1] if mod else ""
    parts = re.split(r" - (?:ERROR|WARNING|CRITICAL) - ", msg, maxsplit=1)
    text = parts[1] if len(parts) > 1 else msg
    text = re.sub(r"^\[(req|task)-[^\]]+\]\s*", "", text)
    return f"{name}: {text}" if name else text


# Colored dot per severity for the ntfy body (works on every ntfy client; ntfy has
# no real text colors, and its markdown only renders well in the web app).
_SEV_EMOJI = {
    "CRITICAL": "\U0001f534",  # red
    "CRIT": "\U0001f534",
    "ERROR": "\U0001f534",
    "ERR": "\U0001f534",
    "HIGH": "\U0001f7e0",  # orange
    "MEDIUM": "\U0001f7e1",  # yellow
    "MED": "\U0001f7e1",
    "WARN": "\U0001f7e1",
    "WARNING": "\U0001f7e1",
    "LOW": "\U0001f7e2",  # green
    "INFO": "\U0001f7e2",
}


def sev_emoji(line: str) -> str:
    """Map a summary line's leading severity word (CRITICAL/HIGH/...) to a colored dot."""
    parts = line.strip().split(None, 1)
    word = parts[0].rstrip(":").upper() if parts else ""
    return _SEV_EMOJI.get(word, "\U0001f539")  # small blue diamond = unclassified


def extract_summary(verdict: str) -> str:
    """Pull the SUMMARY block out of Claude's response for the ntfy body."""
    if verdict.startswith("(claude"):  # triage failed/disabled marker
        return ""
    m = re.search(r"SUMMARY:\s*(.*?)(?:\n\s*DETAIL:|\Z)", verdict, re.DOTALL | re.IGNORECASE)
    text = (m.group(1) if m else verdict).strip()
    lines = [ascii_short(ln, 200) for ln in text.splitlines() if ln.strip()][:6]
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


def signature(msg: str) -> str:
    """Stable key for an alert: strip ids/timestamps so recurrences collapse."""
    s = re.sub(r"\[(req|task)-[^\]]+\]", "", msg)
    s = re.sub(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}[.,]?\d*", "", s)
    s = re.sub(r"[a-z0-9]{2,}[-_][a-z0-9_-]+", "X", s)
    return re.sub(r"\s+", " ", s).strip()[:120]


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
        "<one short plain-language line per distinct problem (max 5). Start each line with the "
        "severity in caps (CRITICAL/HIGH/MEDIUM/LOW), then what is broken and the affected "
        "project/component, so an on-call engineer instantly understands it. ASCII only, no "
        "markdown, no task IDs.>\n\n"
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


def ntfy(title: str, body: str, priority: str, tags: str) -> bool:
    log.info("ntfy push [%s] %s\n%s", priority, title, body)
    if not SEND_NTFY:
        log.info("ntfy disabled (--no-ntfy / SEND_NTFY=False) - would have sent to %s/%s", NTFY_SERVER, NTFY_TOPIC)
        return False
    try:
        requests.post(
            f"{NTFY_SERVER}/{NTFY_TOPIC}",
            data=body.encode("utf-8"),
            headers={"Title": title.encode("ascii", "ignore").decode(), "Priority": priority, "Tags": tags},
            timeout=15,
        )
        log.info("ntfy sent to %s/%s (priority %s)", NTFY_SERVER, NTFY_TOPIC, priority)
        return True
    except requests.RequestException as e:
        log.error("ntfy POST failed: %s", e)
        return False


def run_cycle() -> int:
    # Early-morning runs (before MORNING_BEFORE_HOUR local) widen the look-back so
    # issues from overnight / since yesterday afternoon are swept up, not missed.
    now_local = datetime.now()  # noqa: DTZ005 - local wall-clock for the morning check
    if not WINDOW_OVERRIDDEN and now_local.hour < MORNING_BEFORE_HOUR:
        window = MORNING_WINDOW
        log.info("early run (%s local) - morning catch-up window %s", now_local.strftime("%a %H:%M"), window)
    else:
        window = WINDOW

    log.info("log-watch start: %s/%s | window=%s | levels=%s", NAMESPACE, CONTAINER, window, LEVEL)

    token = load_token()
    ds = find_loki_datasource(token)
    if not ds:
        log.error("no Loki datasource found in Grafana")
        return 1

    expr = build_logql(NAMESPACE, CONTAINER, LEVEL, None)
    rows = parse_frames(query_loki(token, ds, expr, window, "now", LIMIT))
    log.info("fetched %d log line(s) from Loki", len(rows))

    ignore = load_ignore_patterns()
    dropped = 0
    remainder: dict[str, dict] = {}
    for row in rows:
        msg = extract_msg(row[2])
        if any(p.search(msg) for p in ignore):
            dropped += 1
            continue
        ent = remainder.setdefault(signature(msg), {"count": 0, "sample": msg})
        ent["count"] += 1
    log.info(
        "ignore-list (%d patterns) dropped %d line(s); %d remain across %d signature(s)",
        len(ignore),
        dropped,
        len(rows) - dropped,
        len(remainder),
    )

    if not remainder:
        log.info("RESULT: clean - nothing unexpected. No Claude, no notification.")
        return 0

    state = load_state()
    now = datetime.now(UTC)
    fresh: dict[str, dict] = {}
    for sig, ent in remainder.items():
        last = state.get(sig)
        if last:
            try:
                if now - datetime.fromisoformat(last) < timedelta(hours=DEDUP_HOURS):
                    continue
            except ValueError:
                pass
        fresh[sig] = ent
    log.info(
        "dedup: %d new signature(s), %d already alerted within %gh",
        len(fresh),
        len(remainder) - len(fresh),
        DEDUP_HOURS,
    )

    if not fresh:
        log.info("RESULT: no NEW issues (all within dedup window). No Claude, no notification.")
        return 0

    # Order by severity then frequency for a readable summary.
    ordered = sorted(fresh.values(), key=lambda e: (-severity(e["sample"]), -e["count"]))
    samples = [f"[{e['count']}x] {e['sample'][:300]}" for e in ordered]

    log.warning("RESULT: %d NEW issue(s) found:", len(fresh))
    for e in ordered:
        sev = {2: "CRIT", 1: "ERR ", 0: "WARN"}[severity(e["sample"])]
        log.warning("  [%s x%d] %s", sev, e["count"], e["sample"][:200])

    # Full Claude triage goes to the terminal/log; its SUMMARY block becomes the push.
    summary = ""
    if USE_CLAUDE:
        log.info("running Claude triage (timeout %ds)...", CLAUDE_TIMEOUT)
        verdict = run_claude(samples)
        log.info("Claude triage:\n%s", verdict)
        summary = extract_summary(verdict)
        suggested = extract_suggested_ignores(verdict)
        if suggested:
            log.info(
                "Claude suggests ignore-pattern(s) - REVIEW, then paste good ones into ignore_patterns.txt "
                "(not auto-applied):\n%s",
                "\n".join("  " + s for s in suggested),
            )
    else:
        log.info("Claude triage disabled (USE_CLAUDE=False / --no-claude)")

    errors = [e for e in ordered if severity(e["sample"]) >= 1]

    # Problem lines: Claude's grouped SUMMARY (preferred), or a raw errors-first
    # fallback when triage is off/failed. Everything un-ignored is grouped into these
    # problems, so the list IS the full actionable picture - no raw-line-count footer.
    if summary:
        body_lines = [ln for ln in summary.splitlines() if ln.strip()]
    else:
        tag = {2: "CRIT", 1: "ERR", 0: "WARN"}
        body_lines = [
            f"{tag[severity(e['sample'])]} {ascii_short(human(e['sample']))}" for e in (errors or ordered)[:5]
        ]
        if (extra := len(fresh) - len(body_lines)) > 0:
            body_lines.append(f"LOW (+{extra} more - see logs)")

    # Severity dot per line + a blank line between for readability.
    # Body is UTF-8 data so emoji are fine (well under ntfy's 4096-byte attachment limit).
    body = "\n\n".join(f"{sev_emoji(ln)} {ln}" for ln in body_lines)

    priority = (
        "urgent" if re.search(r"\bCRITICAL\b", summary) or any(severity(e["sample"]) == 2 for e in ordered) else "high"
    )
    # Neutral title icon - severity is shown per-line and via priority, not on the header.
    title = f"OPI log-watch: {len(body_lines)} issue(s)"
    ntfy(title, body, priority, "mag")

    iso = now.isoformat()
    for sig in fresh:
        state[sig] = iso
    save_state(state)
    log.info("done - state updated (%d signature(s) tracked)", len(state))
    return 0


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
        return run_cycle()

    log.info("loop mode: one cycle every %d min (Ctrl-C to stop)", args.interval // 60)
    try:
        while True:
            try:
                run_cycle()
            except Exception:
                log.exception("cycle failed - continuing to next cycle")
            log.info("sleeping %d min until next cycle", args.interval // 60)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        log.info("stopped")
        return 0


if __name__ == "__main__":
    sys.exit(main())
