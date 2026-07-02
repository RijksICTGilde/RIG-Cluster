"""Shared OPI production-log triage pipeline - single source of truth.

Both entrypoints call ``run_cycle`` here so their logic never diverges:

  - the standalone CLI at ``scripts/log_watch/watch.py`` injects Claude triage
    (``triage_fn``), a SOPS-decrypted token, and file-based dedup state; and
  - the in-app ``LogwatcherScheduler`` (``opi/core/logwatcher_scheduler.py``)
    runs it every ``LOGWATCHER_INTERVAL_SECONDS`` with no triage_fn (deterministic
    grouped body) and an in-memory dedup dict.

The pipeline is synchronous (``httpx.Client``); the async scheduler runs it via
``asyncio.to_thread`` so it never blocks the event loop.

One cycle:
  1. Query Loki (via the Grafana datasource API) for ERROR/WARNING/CRITICAL lines
     in the OPI container over ``cfg.window``.
  2. Drop every line matching the ignore-list (known noise).
  3. Deduplicate the remainder against ``state`` so an ongoing issue is not
     re-alerted every cycle (within ``cfg.dedup_hours``).
  4. If (and only if) something new is left, build a notification body and POST it
     to ntfy. A clean run is silent but still logged.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

# Ignore-list ships inside the package so both entrypoints read the same file.
DEFAULT_IGNORE_FILE = Path(__file__).resolve().parent / "log_watch_ignore_patterns.txt"

# Max problem lines in one ntfy body - a safety net for ntfy's ~4096-byte limit.
# The deduped set is almost always far smaller, so this rarely bites.
MAX_BODY_LINES = 10

# The watcher scans the OPI container's own logs, which include the watcher's own
# output. Exclude those logger names at query time or it alerts on itself - a loop
# that grows every cycle (its "RESULT: N NEW issue(s)" WARNING lines get re-picked).
SELF_LOG_EXCLUDE = ("opi.services.log_watcher", "opi.core.logwatcher_scheduler")

# A triage function takes the ordered sample lines and returns a plain-text SUMMARY
# block (empty string if it produced nothing). The standalone CLI wires this to
# Claude; the in-app scheduler passes None and the deterministic fallback is used.
TriageFn = Callable[[list[str]], str]


@dataclass
class LogWatchConfig:
    """Everything the pipeline needs except the secret token (passed separately)."""

    grafana_url: str
    ntfy_topic: str
    ntfy_server: str = "https://ntfy.sh"
    namespace: str = "rig-prd-operations"
    container: str = "operations-manager"
    level: str = "(?i)(warn|error|crit|fatal)"  # regex matched against the detected_level label
    window: str = "now-35m"  # look-back per run (35m = 30m cadence + 5m overlap)
    dedup_hours: float = 6.0  # do not re-alert the same signature within this window
    max_lines: int = 5000  # Loki fetch cap per run
    datasource_uid: str | None = None  # skip discovery when the Loki UID is known
    # Optional early-morning catch-up: runs before ``morning_before_hour`` (local)
    # widen the look-back to ``morning_window`` so overnight issues are not missed.
    # Both None (the in-app default) disables it - that loop runs 24/7 every 30 min.
    morning_before_hour: int | None = None
    morning_window: str | None = None
    ignore_file: Path = DEFAULT_IGNORE_FILE


# --- Loki query plumbing (httpx; mirrors scripts/grafana_loki_logs.py) -------


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def find_loki_datasource(client: httpx.Client, base_url: str, token: str) -> dict | None:
    resp = client.get(f"{base_url}/api/datasources", headers=_headers(token), timeout=10)
    resp.raise_for_status()
    for ds in resp.json():
        if ds.get("type") == "loki":
            return ds
    return None


def build_logql(namespace: str, container: str, level: str, grep: str | None, exclude: tuple[str, ...] = ()) -> str:
    expr = f'{{k8s_namespace_name="{namespace}", k8s_container_name="{container}"}}'
    # Drop lines containing any exclude marker (e.g. the watcher's own log output,
    # which would otherwise make it alert on itself in a growing feedback loop).
    # These are cheap line filters, so apply them before any label filter.
    for term in exclude:
        expr += f' != "{term}"'
    if grep:
        expr += f' |~ "{grep}"'
    # Filter on the collector's detected_level structured-metadata label rather than
    # regex-scanning the line: index-level (cheap, skips the debug/info bulk) and it
    # won't match an INFO line that merely prints the word ERROR.
    if level:
        expr += f' | detected_level=~"{level}"'
    return expr


def query_loki(
    client: httpx.Client,
    base_url: str,
    token: str,
    datasource_uid: str,
    expr: str,
    time_from: str,
    time_to: str,
    limit: int,
) -> dict:
    payload = {
        "queries": [
            {
                "refId": "A",
                "datasource": {"type": "loki", "uid": datasource_uid},
                "expr": expr,
                "queryType": "range",
                "maxLines": limit,
            }
        ],
        "from": time_from,
        "to": time_to,
    }
    resp = client.post(f"{base_url}/api/ds/query", headers=_headers(token), json=payload, timeout=60)
    resp.raise_for_status()
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


# --- message parsing / classification / formatting ---------------------------


def load_ignore_patterns(ignore_file: Path) -> list[re.Pattern]:
    pats: list[re.Pattern] = []
    for raw in ignore_file.read_text().splitlines():
        ln = raw.strip()
        if ln and not ln.startswith("#"):
            pats.append(re.compile(ln))
    return pats


def extract_fields(line: str) -> tuple[str, str]:
    """Return (message, level) from the JSON log envelope the collector emits.

    The envelope carries an authoritative ``level`` field (``warn``/``error``/...);
    the human text lives in ``message``. Falls back to the raw line with an empty
    level for anything that is not the expected JSON envelope.
    """
    if line.startswith("{"):
        try:
            obj = json.loads(line)
            msg = obj.get("message") or obj.get("log") or line
            return msg, str(obj.get("level") or "")
        except json.JSONDecodeError:
            return line, ""
    return line, ""


# Envelope level field -> severity rank. Prefer this authoritative field over
# scanning the message text, which mis-fires (e.g. an INFO line that merely prints
# the word CRITICAL). Unknown/blank levels fall back to text scanning below.
_LEVEL_RANK = {
    "critical": 2,
    "crit": 2,
    "fatal": 2,
    "emerg": 2,
    "alert": 2,
    "error": 1,
    "err": 1,
    "warning": 0,
    "warn": 0,
}


def severity(msg: str, level: str = "") -> int:
    """Severity rank (2=crit, 1=error, 0=warn), from the level field when present."""
    rank = _LEVEL_RANK.get(level.strip().lower())
    if rank is not None:
        return rank
    if "CRITICAL" in msg:
        return 2
    if "- ERROR -" in msg or re.search(r"\bERROR\b", msg):
        return 1
    return 0


def ascii_short(s: str, n: int = 150) -> str:
    """ASCII-only, single-line, trimmed - safe and compact for an ntfy line."""
    return " ".join(s.encode("ascii", "ignore").decode().split())[:n]


_UUID_RE = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


def human(msg: str) -> str:
    """Reduce a full log line to 'module: message' for a compact alert."""
    mod = re.search(r"(opi\.[\w.]+) - (?:ERROR|WARNING|CRITICAL)", msg)
    name = mod.group(1).split(".")[-1] if mod else ""
    parts = re.split(r" - (?:ERROR|WARNING|CRITICAL) - ", msg, maxsplit=1)
    text = parts[1] if len(parts) > 1 else msg
    text = re.sub(r"^\[[^\]]*\]\s*", "", text)  # leading request-id field ([-] or [req-..]/[task-..])
    # Drop opaque UUIDs (and their "Task <uuid>:" / "(<uuid>)" wrappers) - they say
    # nothing on a phone and crowd out the useful project/reason within the char budget.
    text = re.sub(rf"\bTask {_UUID_RE}:\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(rf"\s*\(?{_UUID_RE}\)?", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+([:)])", r"\1", text)  # tidy the space left before a stranded : or )
    text = re.sub(r"\s{2,}", " ", text).strip()
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

_SEV_RANK = {
    "CRITICAL": 4,
    "CRIT": 4,
    "HIGH": 3,
    "ERROR": 3,
    "ERR": 3,
    "MEDIUM": 2,
    "MED": 2,
    "WARN": 2,
    "WARNING": 2,
    "LOW": 1,
    "INFO": 1,
}


def _sev_word(line: str) -> str:
    parts = line.strip().split(None, 1)
    return parts[0].rstrip(":").upper() if parts else ""


def sev_emoji(line: str) -> str:
    """Map a line's leading severity word (CRITICAL/HIGH/...) to a colored dot."""
    return _SEV_EMOJI.get(_sev_word(line), "\U0001f539")  # small blue diamond = unclassified


def sev_rank(line: str) -> int:
    """Numeric severity rank from a line's leading word - higher = more severe."""
    return _SEV_RANK.get(_sev_word(line), 0)


def signature(msg: str) -> str:
    """Stable key for an alert: strip ids/timestamps/volatile values so recurrences collapse.

    Order matters: timestamps and IPs are removed before the catch-all number pass,
    so a per-occurrence count/port/duration/IP never produces a fresh key (which
    would defeat dedup and re-alert the same issue every cycle).
    """
    s = re.sub(r"\[(req|task)-[^\]]+\]", "", msg)
    s = re.sub(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}[.,]?\d*", "", s)  # timestamps
    s = re.sub(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", "X", s)  # IPv4 addresses
    s = re.sub(r"[a-z0-9]{2,}[-_][a-z0-9_-]+", "X", s)  # hyphenated ids (pods, uuids)
    s = re.sub(r"\d+", "N", s)  # any remaining number (counts, ports, sizes, durations)
    return re.sub(r"\s+", " ", s).strip()[:120]


def send_ntfy(client: httpx.Client, cfg: LogWatchConfig, title: str, body: str, priority: str, tags: str) -> bool:
    server = cfg.ntfy_server.rstrip("/")
    logger.info("ntfy push [%s] %s\n%s", priority, title, body)
    try:
        client.post(
            f"{server}/{cfg.ntfy_topic}",
            content=body.encode("utf-8"),
            headers={"Title": title.encode("ascii", "ignore").decode(), "Priority": priority, "Tags": tags},
            timeout=15,
        )
        logger.info("ntfy sent to %s/%s (priority %s)", server, cfg.ntfy_topic, priority)
        return True
    except httpx.HTTPError as e:
        logger.error("ntfy POST failed: %s", e)
        return False


def _effective_window(cfg: LogWatchConfig) -> str:
    """Widen to the morning catch-up window for early runs, when configured."""
    if cfg.morning_before_hour is None or cfg.morning_window is None:
        return cfg.window
    now_local = datetime.now()  # noqa: DTZ005 - local wall-clock for the morning check
    if now_local.hour < cfg.morning_before_hour:
        logger.info(
            "early run (%s local) - morning catch-up window %s",
            now_local.strftime("%a %H:%M"),
            cfg.morning_window,
        )
        return cfg.morning_window
    return cfg.window


def run_cycle(
    cfg: LogWatchConfig,
    token: str,
    state: dict[str, str],
    *,
    triage_fn: TriageFn | None = None,
    send_notification: bool = True,
) -> int:
    """Run one triage cycle. Mutates ``state`` (signature -> ISO timestamp) in place.

    Returns 0 on success (clean or alerted), 1 if Loki was unreachable.
    """
    if not token:
        logger.error("no Grafana token - cannot query Loki")
        return 1
    if not cfg.ntfy_topic:
        logger.error("no ntfy topic configured - cannot notify")
        return 1

    window = _effective_window(cfg)
    logger.info("log-watch start: %s/%s | window=%s | levels=%s", cfg.namespace, cfg.container, window, cfg.level)

    with httpx.Client() as client:
        ds_uid = cfg.datasource_uid
        if not ds_uid:
            ds = find_loki_datasource(client, cfg.grafana_url, token)
            if not ds:
                logger.error("no Loki datasource found in Grafana")
                return 1
            ds_uid = ds["uid"]

        expr = build_logql(cfg.namespace, cfg.container, cfg.level, None, exclude=SELF_LOG_EXCLUDE)
        result = query_loki(client, cfg.grafana_url, token, ds_uid, expr, window, "now", cfg.max_lines)
        rows = parse_frames(result)
        logger.info("fetched %d log line(s) from Loki", len(rows))

        ignore = load_ignore_patterns(cfg.ignore_file)
        dropped = 0
        remainder: dict[str, dict] = {}
        for row in rows:
            msg, level = extract_fields(row[2])
            if any(p.search(msg) for p in ignore):
                dropped += 1
                continue
            ent = remainder.setdefault(signature(msg), {"count": 0, "sample": msg, "level": level})
            ent["count"] += 1
        logger.info(
            "ignore-list (%d patterns) dropped %d line(s); %d remain across %d signature(s)",
            len(ignore),
            dropped,
            len(rows) - dropped,
            len(remainder),
        )

        if not remainder:
            logger.info("RESULT: clean - nothing unexpected. No notification.")
            return 0

        now = datetime.now(UTC)
        fresh: dict[str, dict] = {}
        for sig, ent in remainder.items():
            last = state.get(sig)
            if last:
                try:
                    if now - datetime.fromisoformat(last) < timedelta(hours=cfg.dedup_hours):
                        continue
                except ValueError:
                    pass
            fresh[sig] = ent
        logger.info(
            "dedup: %d new signature(s), %d already alerted within %gh",
            len(fresh),
            len(remainder) - len(fresh),
            cfg.dedup_hours,
        )

        if not fresh:
            logger.info("RESULT: no NEW issues (all within dedup window). No notification.")
            return 0

        # Order by severity then frequency for a readable summary.
        ordered = sorted(fresh.values(), key=lambda e: (-severity(e["sample"], e["level"]), -e["count"]))
        samples = [f"[{e['count']}x] {e['sample'][:300]}" for e in ordered]

        logger.warning("RESULT: %d NEW issue(s) found:", len(fresh))
        for e in ordered:
            sev = {2: "CRIT", 1: "ERR ", 0: "WARN"}[severity(e["sample"], e["level"])]
            logger.warning("  [%s x%d] %s", sev, e["count"], e["sample"][:200])

        # Optional triage (Claude in the CLI) produces a grouped SUMMARY block that
        # becomes the push body; without it we fall back to deterministic formatting.
        summary = triage_fn(samples) if triage_fn else ""

        if summary:
            body_lines = [ln for ln in summary.splitlines() if ln.strip()]
        else:
            # No triage: show every fresh signature with its real severity, ordered
            # most-severe first. Cap only as an ntfy-size safety net; if the cap is
            # hit, say so truthfully (no invented severity on the overflow line).
            tag = {2: "CRIT", 1: "ERR", 0: "WARN"}
            shown = ordered[:MAX_BODY_LINES]
            body_lines = [f"{tag[severity(e['sample'], e['level'])]} {ascii_short(human(e['sample']))}" for e in shown]
            if (extra := len(ordered) - len(shown)) > 0:
                body_lines.append(f"(+{extra} more, see OPI logs)")

        # Sort shown problems most-severe first (stable - keeps triage order within a tier).
        body_lines.sort(key=lambda ln: -sev_rank(ln))
        body = "\n\n".join(f"{sev_emoji(ln)} {ln}" for ln in body_lines)

        priority = (
            "urgent"
            if re.search(r"\bCRITICAL\b", summary) or any(severity(e["sample"], e["level"]) == 2 for e in ordered)
            else "high"
        )
        title = f"OPI log-watch: {len(fresh)} issue(s)"

        if send_notification:
            send_ntfy(client, cfg, title, body, priority, "mag")
        else:
            logger.info("notification disabled - would have sent to %s/%s", cfg.ntfy_server, cfg.ntfy_topic)

    iso = now.isoformat()
    for sig in fresh:
        state[sig] = iso
    logger.info("done - state updated (%d signature(s) tracked)", len(state))
    return 0
