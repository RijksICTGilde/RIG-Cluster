"""Unit tests for the shared log-watcher pipeline (opi.services.log_watcher).

These drive run_cycle with injected Loki rows and a captured ntfy sender, so the
same code both entrypoints use (CLI + in-app scheduler) is exercised directly.
"""

from __future__ import annotations

import json

import pytest
from opi.services import log_watcher
from opi.services.log_watcher import (
    SELF_LOG_EXCLUDE,
    LogWatchConfig,
    build_logql,
    parse_frames,
    run_cycle,
    severity,
    signature,
)


def _line(level: str, module: str, msg: str) -> str:
    return f"2026-07-01 10:00:00,000 - {module} - {level} - {msg}"


def _envelope(level: str, module: str, level_word: str, msg: str) -> str:
    """A JSON log envelope like the collector emits (authoritative level field)."""
    return json.dumps({"level": level, "message": f"2026-07-01 10:00:00,000 - {module} - {level_word} - {msg}"})


@pytest.fixture
def captured_ntfy(monkeypatch) -> list[dict]:
    """Replace send_ntfy with a recorder; returns the list of sent notifications."""
    sent: list[dict] = []

    def _fake_send(client, cfg, title, body, priority, tags):
        sent.append({"title": title, "body": body, "priority": priority, "tags": tags})
        return True

    monkeypatch.setattr(log_watcher, "send_ntfy", _fake_send)
    return sent


def _inject_rows(monkeypatch, lines: list[str]) -> None:
    """Bypass the network: query returns nothing, parse yields the given lines."""
    monkeypatch.setattr(log_watcher, "query_loki", lambda *a, **k: {})
    rows = [(0, {}, ln) for ln in lines]
    monkeypatch.setattr(log_watcher, "parse_frames", lambda _result: rows)


def _cfg() -> LogWatchConfig:
    # datasource_uid set so run_cycle skips datasource discovery (no network).
    return LogWatchConfig(grafana_url="http://grafana", ntfy_topic="topic-xyz", datasource_uid="loki-uid")


def test_missing_token_returns_error(captured_ntfy):
    rc = run_cycle(_cfg(), token="", state={})
    assert rc == 1
    assert captured_ntfy == []


def test_clean_run_sends_nothing(monkeypatch, captured_ntfy):
    # Both lines match ignore-list patterns -> nothing escalates.
    _inject_rows(
        monkeypatch,
        [
            _line("ERROR", "opi.connectors.postgres", "Database foo already exists on host db-1"),
            _line("WARNING", "opi.connectors.minio", "Unable to make bucket bar already own it"),
        ],
    )
    state: dict[str, str] = {}
    rc = run_cycle(_cfg(), token="tok", state=state)
    assert rc == 0
    assert captured_ntfy == []
    assert state == {}


def test_new_issue_notifies_and_records_state(monkeypatch, captured_ntfy):
    _inject_rows(monkeypatch, [_line("ERROR", "opi.manager.project_manager", "Deployment failed for project rig-foo")])
    state: dict[str, str] = {}
    rc = run_cycle(_cfg(), token="tok", state=state)

    assert rc == 0
    assert len(captured_ntfy) == 1
    note = captured_ntfy[0]
    # Deterministic fallback body (no triage_fn): severity tag + human message.
    assert "ERR" in note["body"]
    assert "project_manager: Deployment failed for project rig-foo" in note["body"]
    assert note["priority"] == "high"
    assert len(state) == 1  # signature recorded for dedup


def test_critical_sets_urgent_priority(monkeypatch, captured_ntfy):
    _inject_rows(monkeypatch, [_line("CRITICAL", "opi.server", "Event loop stalled")])
    rc = run_cycle(_cfg(), token="tok", state={})
    assert rc == 0
    assert captured_ntfy[0]["priority"] == "urgent"


def test_warning_shown_alongside_error(monkeypatch, captured_ntfy):
    # A warning must appear as its own line, not be demoted into a "+N more" footer.
    _inject_rows(
        monkeypatch,
        [
            _line("ERROR", "opi.manager.project_manager", "Deployment failed for project rig-foo"),
            _line("WARNING", "opi.connectors.kubectl", "Slow response from apiserver"),
        ],
    )
    run_cycle(_cfg(), token="tok", state={})
    body = captured_ntfy[0]["body"]
    assert "ERR" in body
    assert "project_manager: Deployment failed for project rig-foo" in body
    assert "WARN" in body
    assert "kubectl: Slow response from apiserver" in body
    assert "more" not in body  # no overflow footer for a 2-item batch
    assert "LOW" not in body  # the fake hardcoded severity is gone
    assert captured_ntfy[0]["title"] == "OPI log-watch: 2 issue(s)"


def test_overflow_footer_is_truthful(monkeypatch, captured_ntfy):
    words = [
        "alpha",
        "bravo",
        "charlie",
        "delta",
        "echo",
        "foxtrot",
        "golf",
        "hotel",
        "india",
        "juliet",
        "kilo",
        "lima",
    ]
    _inject_rows(monkeypatch, [_line("ERROR", "opi.manager.deploy", f"{w} deployment failed") for w in words])
    run_cycle(_cfg(), token="tok", state={})
    body = captured_ntfy[0]["body"]
    # 12 distinct signatures, capped at 10 -> honest overflow line, no invented severity.
    assert "(+2 more, see OPI logs)" in body
    assert "LOW" not in body


def test_dedup_suppresses_repeat_within_window(monkeypatch, captured_ntfy):
    line = _line("ERROR", "opi.manager.project_manager", "Deployment failed for project rig-foo")
    _inject_rows(monkeypatch, [line])
    state: dict[str, str] = {}

    run_cycle(_cfg(), token="tok", state=state)
    assert len(captured_ntfy) == 1

    # Same signature again within dedup_hours -> no second notification.
    run_cycle(_cfg(), token="tok", state=state)
    assert len(captured_ntfy) == 1


def test_triage_fn_body_overrides_fallback(monkeypatch, captured_ntfy):
    _inject_rows(monkeypatch, [_line("ERROR", "opi.manager.project_manager", "Deployment failed for project rig-foo")])

    def _triage(samples: list[str]) -> str:
        return "CRITICAL grouped summary of the problem"

    rc = run_cycle(_cfg(), token="tok", state={}, triage_fn=_triage)
    assert rc == 0
    body = captured_ntfy[0]["body"]
    assert "grouped summary of the problem" in body
    # A CRITICAL word in the triage summary escalates priority to urgent.
    assert captured_ntfy[0]["priority"] == "urgent"


def test_send_notification_false_skips_send(monkeypatch, captured_ntfy):
    _inject_rows(monkeypatch, [_line("ERROR", "opi.manager.project_manager", "Deployment failed for project rig-foo")])
    state: dict[str, str] = {}
    rc = run_cycle(_cfg(), token="tok", state=state, send_notification=False)
    assert rc == 0
    assert captured_ntfy == []
    assert len(state) == 1  # state still recorded even in dry-run


def test_signature_collapses_dynamic_ids():
    a = signature("[task-abc123] Deployment failed for rig-foo-prod")
    b = signature("[task-def456] Deployment failed for rig-foo-prod")
    assert a == b


def test_signature_collapses_numbers_and_ips():
    # Per-occurrence numbers/IPs must not create fresh dedup keys (else re-alert every cycle).
    a = signature("Failed to connect to 10.128.5.23:9000 after 1523ms")
    b = signature("Failed to connect to 10.9.9.9:9000 after 42ms")
    assert a == b


def test_clean_run_ignores_newly_added_patterns(monkeypatch, captured_ntfy):
    _inject_rows(
        monkeypatch,
        [
            _line("ERROR", "opi.connectors.keycloak", "Failed to assign client scope as realm default: 409: ..."),
            _line("WARNING", "opi.manager.project_manager", "No deployments found in project: jongo-lh2"),
            _line(
                "WARNING",
                "opi.manager.database_manager",
                "Source schema 'mpfm_w3h_test' exists but contains no tables. Cloning will result in an empty schema.",
            ),
            _line(
                "WARNING",
                "opi.connectors.postgres",
                "pg_dump produced very little output (91 bytes) - might be empty dump",
            ),
        ],
    )
    rc = run_cycle(_cfg(), token="tok", state={})
    assert rc == 0
    assert captured_ntfy == []  # all match the ignore-list now


def test_human_strips_uuids():
    msg = (
        "2026-07-02 10:49:56,979 - opi.core.persistent_task_progress - ERROR - "
        "[-] Task 52c247eb-b497-4a73-8f39-4121f9d66d8b: Failed task: Processing deployment "
        "(74838d37-fa03-4d9f-beee-9fe206f886c4): wies-pr-429"
    )
    out = log_watcher.human(msg)
    assert "52c247eb" not in out  # UUIDs gone
    assert "74838d37" not in out
    assert out == "persistent_task_progress: Failed task: Processing deployment: wies-pr-429"


def test_transient_git_push_is_ignored(monkeypatch, captured_ntfy):
    # Raw git stderr from a rejection the connector rebases/retries away -> not an alert.
    _inject_rows(
        monkeypatch,
        ["error: failed to push some refs to 'https://github.com/RijksICTGilde/rig-cluster-projects.git'"],
    )
    assert run_cycle(_cfg(), token="tok", state={}) == 0
    assert captured_ntfy == []


def test_terminal_git_push_failure_still_alerts(monkeypatch, captured_ntfy):
    # The connector's own "after N attempts" ERROR must NOT be caught by the ^error: rule.
    _inject_rows(
        monkeypatch,
        [
            _line(
                "ERROR",
                "opi.connectors.git",
                "Failed to push changes to main on github after 5 attempts: error: failed to push some refs",
            )
        ],
    )
    assert run_cycle(_cfg(), token="tok", state={}) == 0
    assert len(captured_ntfy) == 1


def test_argocd_timeout_cascade_collapses_to_root(monkeypatch, captured_ntfy):
    # One ArgoCD app-creation timeout is logged at 5 layers; only the root should alert.
    _inject_rows(
        monkeypatch,
        [
            _line(
                "ERROR",
                "opi.core.persistent_task_progress",
                "Failed task: Deployment processing: mpfm-w3h-pr-120: timed out waiting for application to be created",
            ),
            _line(
                "ERROR",
                "opi.core.persistent_task_progress",
                "Failed task: Waiting for ArgoCD deployment sync: Sync failures: mpfm-w3h-pr-120: "
                "timed out waiting for application to be created",
            ),
            _line(
                "ERROR",
                "opi.manager.project_manager",
                "ArgoCD sync completed with 1 failure(s): mpfm-w3h-pr-120: timed out waiting for application to be created",
            ),
            _line(
                "ERROR",
                "opi.manager.project_manager",
                "Timed out waiting for ArgoCD application 'mpfm-w3h-pr-120' to be created",
            ),
            _line(
                "ERROR",
                "opi.manager.argo_manager",
                "Timeout waiting for ArgoCD application 'mpfm-w3h-pr-120' to be created after 360s",
            ),
        ],
    )
    run_cycle(_cfg(), token="tok", state={})
    assert len(captured_ntfy) == 1
    body = captured_ntfy[0]["body"]
    assert "after 360s" in body  # the root argo_manager line survives
    assert "Failed task" not in body
    assert "sync completed with" not in body
    assert captured_ntfy[0]["title"] == "OPI log-watch: 1 issue(s)"


def test_severity_ranking():
    assert severity("something CRITICAL happened") == 2
    assert severity("opi.x - ERROR - boom") == 1
    assert severity("opi.x - WARNING - meh") == 0


def test_build_logql_filters_by_level_and_excludes_self_logs():
    expr = build_logql(
        "rig-prd-operations", "operations-manager", "(?i)(warn|error|crit|fatal)", None, exclude=SELF_LOG_EXCLUDE
    )
    # Level filtering uses the detected_level label, not a line regex over the message.
    assert '| detected_level=~"(?i)(warn|error|crit|fatal)"' in expr
    # Self-exclusion applied as line filters, before the label filter.
    assert '!= "opi.services.log_watcher"' in expr
    assert '!= "opi.core.logwatcher_scheduler"' in expr
    assert expr.index('!= "opi.services.log_watcher"') < expr.index("| detected_level")


def test_severity_prefers_level_field():
    # A known level field is authoritative - the message text is not scanned.
    # (Here the text says CRITICAL but the field says warn -> warn wins.)
    assert log_watcher.severity("levels=ERROR|WARNING|CRITICAL", "warn") == 0
    assert log_watcher.severity("anything", "error") == 1
    assert log_watcher.severity("anything", "critical") == 2
    # Blank/unknown level falls back to scanning the message text.
    assert log_watcher.severity("x - ERROR - y", "") == 1
    assert log_watcher.severity("plain warning text", "") == 0


def test_json_envelope_level_drives_classification(monkeypatch, captured_ntfy):
    _inject_rows(
        monkeypatch,
        [
            _envelope("error", "opi.manager.deploy", "ERROR", "rollout failed"),
            # level=warn, but the message text contains the word ERROR; the field must win -> WARN.
            _envelope("warn", "opi.connectors.kubectl", "WARNING", "ERROR string appeared in output"),
        ],
    )
    run_cycle(_cfg(), token="tok", state={})
    note = captured_ntfy[0]
    segments = note["body"].split("\n\n")
    err_line = next(s for s in segments if "rollout failed" in s)
    warn_line = next(s for s in segments if "appeared in output" in s)
    assert err_line.split()[1] == "ERR"  # emoji, tag, text...
    assert warn_line.split()[1] == "WARN"  # not ERR, despite "ERROR" in the text
    assert "kubectl: ERROR string appeared in output" in warn_line
    # No CRITICAL-level entry present, so priority stays high (not urgent).
    assert note["priority"] == "high"


def test_parse_frames_extracts_rows():
    result = {
        "results": {
            "A": {
                "frames": [
                    {
                        "schema": {
                            "fields": [
                                {"name": "Time", "type": "time"},
                                {"name": "Line", "type": "string"},
                            ]
                        },
                        "data": {"values": [[1000, 2000], ["line one", "line two"]]},
                    }
                ]
            }
        }
    }
    rows = parse_frames(result)
    assert [r[2] for r in rows] == ["line one", "line two"]
    assert rows[0][0] == 1000
