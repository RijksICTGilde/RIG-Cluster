"""Weigh a scanner's outcome against a checked-in exception list.

The attack proof runs several scanners: nuclei against a self-provisioned target,
kube-linter and checkov against the manifests. Every scanner has its own output format
but the same gate: a finding that is not in ``aanvalsproef/uitzonderingen.yaml`` makes
the run fail. An exception carries a statement and an expiry date, as in
``.trivyignore.yaml``, and an expired exception no longer silences anything, so the
finding comes back by itself.

Usage::

    uv run python -m opi.tools.aanvalsproef grendel http://127.0.0.1:8111
    uv run python -m opi.tools.aanvalsproef nuclei nuclei.jsonl --drempel medium
    uv run python -m opi.tools.aanvalsproef kube-linter kube-linter.json
    uv run python -m opi.tools.aanvalsproef checkov checkov.json
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime
from ipaddress import ip_address
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from opi.utils.yaml_util import load_yaml_safe

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

#: Repo root: four directories above this file (opi/tools -> opi -> python -> operations-manager).
REPO_ROOT = Path(__file__).resolve().parents[4]
EXCEPTIONS_FILE = REPO_ROOT / "aanvalsproef" / "uitzonderingen.yaml"

#: The severity levels of nuclei, from low to high. The threshold picks one of them.
SEVERITY_ASCENDING = ("info", "low", "medium", "high", "critical")

#: Names that count as loopback. Anything else is not a self-provisioned target.
LOOPBACK_NAMES = ("localhost", "127.0.0.1", "::1")


class GuardError(Exception):
    """The target is not a self-provisioned instance."""


@dataclass(frozen=True)
class Finding:
    """A finding from a scanner, detached from the format it was reported in."""

    id: str
    location: str
    severity: str


def guard_target(doelwit: str) -> None:
    """Refuse every target that is not on this runner's loopback.

    The attack proof fires real attack traffic. Only an instance the run provisioned itself
    may receive that, never production and never the shared sandbox.
    """
    parsed = urlparse(doelwit)
    if parsed.scheme not in ("http", "https"):
        raise GuardError(f"doelwit {doelwit!r} heeft geen http(s)-schema")
    host = parsed.hostname
    if not host:
        raise GuardError(f"doelwit {doelwit!r} noemt geen host")
    if host in LOOPBACK_NAMES:
        return
    try:
        address = ip_address(host)
    except ValueError as error:
        raise GuardError(f"doelwit {host!r} is geen loopback-adres en geen loopback-naam") from error
    if not address.is_loopback:
        raise GuardError(f"doelwit {host!r} is geen loopback-adres")


def valid_exceptions(soort: str, today: date, path: Path = EXCEPTIONS_FILE) -> set[str]:
    """The ids that stay silent for this scanner, without the expired and incomplete entries.

    The scanner name is also the section in the list, so it keeps the name the CLI takes.
    """
    content = load_yaml_safe(path.read_text(encoding="utf-8")) or {}
    valid = set()
    for entry in content.get(soort) or []:
        expires_at = entry.get("expired_at")
        if not isinstance(expires_at, date) or not str(entry.get("statement") or "").strip():
            continue
        if expires_at >= today:
            valid.add(str(entry["id"]))
    return valid


def read_nuclei(report: Path) -> list[Finding]:
    """The findings from nuclei's JSONL output (``-jsonl-export``)."""
    findings = []
    for entry in report.read_text(encoding="utf-8").splitlines():
        if not entry.strip():
            continue
        hit = json.loads(entry)
        findings.append(
            Finding(
                id=str(hit.get("template-id", "<zonder template-id>")),
                location=str(hit.get("matched-at") or hit.get("host") or ""),
                severity=str((hit.get("info") or {}).get("severity", "unknown")).lower(),
            )
        )
    return findings


def read_kube_linter(report: Path) -> list[Finding]:
    """The findings from ``kube-linter lint --format json``.

    kube-linter reports no severity: the selection in ``aanvalsproef/kube-linter.yaml`` is
    the threshold, so every finding weighs the same.
    """
    content = json.loads(report.read_text(encoding="utf-8")) or {}
    findings = []
    for report_entry in content.get("Reports") or []:
        scanned = report_entry.get("Object") or {}
        k8s = scanned.get("K8sObject") or {}
        kind = (k8s.get("GroupVersionKind") or {}).get("Kind", "?")
        file_path = (scanned.get("Metadata") or {}).get("FilePath", "?")
        findings.append(
            Finding(
                id=str(report_entry.get("Check", "<zonder check>")),
                location=f"{kind}/{k8s.get('Name', '?')} in {file_path}",
                severity="high",
            )
        )
    return findings


def read_checkov(report: Path) -> list[Finding]:
    """The failed checks from ``checkov -o json``.

    Checkov writes a list as soon as several frameworks run, and an object otherwise.
    """
    content = json.loads(report.read_text(encoding="utf-8"))
    reports = content if isinstance(content, list) else [content]
    return [
        Finding(
            id=str(check.get("check_id", "<zonder check_id>")),
            location=f"{check.get('resource', '?')} in {check.get('file_path', '?')}",
            severity=str(check.get("severity") or "high").lower(),
        )
        for part in reports
        for check in ((part.get("results") or {}).get("failed_checks") or [])
    ]


def read_semgrep(report: Path) -> list[Finding]:
    """The findings from ``semgrep scan --json``.

    Semgrep's severity (INFO, WARNING, ERROR) is mapped onto the scale of the threshold.
    """
    severity_per_level = {"INFO": "low", "WARNING": "medium", "ERROR": "high"}
    content = json.loads(report.read_text(encoding="utf-8")) or {}
    return [
        Finding(
            id=str(hit.get("check_id", "<zonder check_id>")),
            location=f"{hit.get('path', '?')}:{(hit.get('start') or {}).get('line', '?')}",
            severity=severity_per_level.get(str((hit.get("extra") or {}).get("severity", "")), "high"),
        )
        for hit in content.get("results") or []
    ]


#: The readers per scanner. The key is also the section in the exception list.
READERS: dict[str, Callable[[Path], list[Finding]]] = {
    "nuclei": read_nuclei,
    "kube-linter": read_kube_linter,
    "checkov": read_checkov,
    "semgrep": read_semgrep,
}


def above_threshold(findings: Iterable[Finding], threshold: str) -> list[Finding]:
    """The findings from the threshold severity upwards. An unknown severity always counts."""
    floor = SEVERITY_ASCENDING.index(threshold)
    return [
        finding
        for finding in findings
        if finding.severity not in SEVERITY_ASCENDING or SEVERITY_ASCENDING.index(finding.severity) >= floor
    ]


def gate(findings: Iterable[Finding], silenced: set[str], threshold: str) -> list[Finding]:
    """What is left after threshold and exceptions, and so makes the run red."""
    return [finding for finding in above_threshold(findings, threshold) if finding.id not in silenced]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Toets de uitkomst van een aanvalsproef-scanner.")
    parser.add_argument("soort", choices=["grendel", *READERS], help="de scanner, of grendel voor de doelwitcontrole")
    parser.add_argument("doelwit", help="het rapportbestand, of de URL bij grendel")
    parser.add_argument(
        "--drempel", choices=SEVERITY_ASCENDING, default="medium", help="vanaf welke ernst een bevinding meetelt"
    )
    parser.add_argument("--uitzonderingen", type=Path, default=EXCEPTIONS_FILE, help="pad naar de uitzonderingslijst")
    args = parser.parse_args(argv)

    if args.soort == "grendel":
        try:
            guard_target(args.doelwit)
        except GuardError as error:
            sys.stderr.write(f"grendel: {error}\n")
            return 1
        sys.stdout.write(f"grendel: {args.doelwit} is een zelf opgezet doelwit\n")
        return 0

    report = Path(args.doelwit)
    if not report.exists():
        sys.stderr.write(f"rapport {report} bestaat niet, de scanner heeft niets geschreven\n")
        return 1

    findings = READERS[args.soort](report)
    silenced = valid_exceptions(args.soort, datetime.now(tz=UTC).date(), args.uitzonderingen)
    blocking = gate(findings, silenced, args.drempel)

    sys.stdout.write(
        f"{args.soort}: {len(findings)} bevindingen, drempel {args.drempel},"
        f" {len(silenced)} geldige uitzonderingen, {len(blocking)} blokkerend\n"
    )
    for finding in blocking:
        sys.stderr.write(f"  {finding.severity:>8}  {finding.id}  {finding.location}\n")
    if blocking:
        sys.stderr.write(
            "Nieuwe bevindingen boven de drempel. Dicht ze, of zet ze met een reden en een"
            f" vervaldatum in {args.uitzonderingen}.\n"
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
