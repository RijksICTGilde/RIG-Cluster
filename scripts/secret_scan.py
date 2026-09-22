"""Scan a tree, a set of files, or the whole git history for committed secrets.

There should never have been a commit with a secret in it. That is the requirement, and the
obvious answer -- a pre-commit hook -- is the one that does not work here: ``--no-verify`` is
standing practice in this project, because pre-commit stashes everything unstaged and knocks over
other sessions working in the same checkout. A guard one flag gets past, that everyone gets past
daily, is not a guard.

So there are three layers, of which only the second and third bind:

1. **local and friendly** -- the pre-commit hook. Catches the honest accident and costs nothing.
   Still bypassable, and that is accepted as long as layer 2 exists.
2. **CI, binding** -- a job on every push and every pull request that fails the build. This is
   the layer that counts, because it knows no ``--no-verify``. It scans the whole TREE of the
   branch and not only the diff, so something that arrives by a detour still shows up.
3. **on the server** -- GitHub push protection on the public repo. Note the limit: the default
   patterns cover known token shapes such as a GitHub PAT, but ``AGE-SECRET-KEY-`` is not among
   them. That needs a custom pattern, and whether that is available depends on the plan. Find out
   before relying on it.

**Why a valid AGE key and not the string ``AGE-SECRET-KEY-``.** The tree holds roughly twenty
placeholders (``AGE-SECRET-KEY-1TEST``, ``AGE-SECRET-KEY-FAKE``, ``AGE-SECRET-KEY-1PLAINTEXT``)
which are neither secret nor removable without making those tests worse. A scanner that alarms on
the prefix produces findings nobody has to fix, and an alarm everyone learns to walk around is no
alarm. So an AGE candidate is only a finding when ``age-keygen -y`` accepts it: a real key. That
is the difference between a guard and noise, and it is why the tree had to be cleaned first --
before this, the scan would have reported four findings, three of them known and harmless.

**What is scanned.** Not only AGE keys. GitHub PATs (``ghp_``, ``github_pat_``, and the other
``gh*_`` shapes), the ``age:``/``base64+age:`` form ZAD writes itself, private keys in PEM form,
and kubeconfig/service-account tokens. The patterns for our own shapes have to come from us: no
off-the-shelf scanner knows them.
"""

from __future__ import annotations

import base64
import json
import re
import subprocess
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

AGE_KEY_MARKER = "AGE-SECRET-KEY-"

#: Binary and vendored paths a source scan has no business walking.
SKIP_DIRECTORIES = frozenset(
    {".git", ".venv", "node_modules", "__pycache__", ".pytest_cache", ".ruff_cache", "dist", "build"}
)
SKIP_SUFFIXES = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".woff", ".woff2", ".ttf", ".eot", ".mp4", ".pdf", ".jar", ".zip"}
)
MAX_BYTES = 2_000_000


@dataclass(frozen=True)
class Finding:
    """One hit: where it is, what kind, and enough context to recognise it -- never the value."""

    path: str
    line_number: int
    kind: str
    hint: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line_number}: {self.kind} ({self.hint})"


@cache
def age_key_is_real(candidate: str) -> bool:
    """Whether ``age-keygen -y`` accepts this as a private key.

    This is the whole reason the guard is believable. Without it every placeholder in the test
    suite is a finding, and a scan with known findings in it gets ignored.

    Cached, and that is what makes the history sweep finishable: one subprocess per DISTINCT
    candidate instead of one per occurrence. The history holds every old version of every test
    file, so the same handful of placeholders comes past thousands of times.
    """
    process = subprocess.run(
        ["age-keygen", "-y"],  # noqa: S607
        input=candidate,
        capture_output=True,
        text=True,
        check=False,
    )
    return process.returncode == 0 and process.stdout.strip().startswith("age1")


#: Each rule is (kind, pattern, hint). A rule whose match needs proving carries its own check in
#: ``scan_text`` rather than being weakened into a looser regex.
RULES: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("age-private-key", re.compile(rf"{AGE_KEY_MARKER}[A-Z0-9]{{50,}}"), "a valid AGE private key"),
    ("github-pat", re.compile(r"\bghp_[A-Za-z0-9]{36,}\b"), "classic GitHub personal access token"),
    ("github-pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{50,}\b"), "fine-grained GitHub token"),
    ("github-token", re.compile(r"\b(?:gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}\b"), "GitHub OAuth/app/refresh token"),
    ("kubeconfig-token", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}\b"), "JWT"),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"), "Slack token"),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"), "AWS access key id"),
)

#: Rules that need more than one line, so they run over the whole text.
#:
#: A PEM private key is its header AND a body. Matching the header alone made six findings in the
#: test suite, every one of them a header string used as test data ("BEGIN PRIVATE KEY" followed by
#: "BBBB", or by nothing at all) -- exactly the kind of known finding that teaches people to ignore
#: the scan. Requiring two lines of real base64 in between leaves those alone and still catches a
#: key, which is never shorter than that.
MULTILINE_RULES: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "pem-private-key",
        re.compile(
            r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----\s*\n"
            r"(?:[A-Za-z0-9+/=]{32,}\s*\n){2,}"
        ),
        "PEM private key with a body",
    ),
)

#: The ciphertext forms ZAD writes itself. These are NOT findings on their own -- the whole point
#: of AGE is that the ciphertext may be committed -- so they are reported only when asked for
#: explicitly, as an inventory rather than an alarm.
CIPHERTEXT_RULES: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("age-ciphertext", re.compile(r"-----BEGIN AGE ENCRYPTED FILE-----"), "armored AGE block"),
    ("age-ciphertext", re.compile(r"\bbase64\+age:[A-Za-z0-9+/=]{20,}"), "base64+age value"),
)


def looks_like_a_jwt(candidate: str) -> bool:
    """Whether the first segment really decodes to a JWT header.

    A three-part ``eyJ...`` string is almost always a token, but "almost" is what produces findings
    nobody fixes. Decoding the header and requiring an ``alg`` makes the rule say what it means.
    """
    head = candidate.split(".", 1)[0]
    padded = head + "=" * (-len(head) % 4)
    try:
        header = json.loads(base64.urlsafe_b64decode(padded))
    except ValueError, UnicodeDecodeError:
        return False
    return isinstance(header, dict) and "alg" in header


def scan_text(text: str, path: str, *, include_ciphertext: bool = False) -> list[Finding]:
    """Every finding in one file's text, with the line number.

    An AGE candidate is checked with ``age-keygen`` before it counts and a JWT candidate has its
    header decoded, so a placeholder is not a finding. The rest is shape-based: a ``ghp_`` of the
    right length is a token whether it is live or revoked, and a scanner cannot tell those apart --
    nor should it try.
    """
    findings: list[Finding] = []
    rules = (*RULES, *CIPHERTEXT_RULES) if include_ciphertext else RULES
    for number, line in enumerate(text.splitlines(), start=1):
        for kind, pattern, hint in rules:
            for match in pattern.finditer(line):
                if kind == "age-private-key" and not age_key_is_real(match.group(0)):
                    continue
                if kind == "kubeconfig-token" and not looks_like_a_jwt(match.group(0)):
                    continue
                findings.append(Finding(path=path, line_number=number, kind=kind, hint=hint))
    for kind, pattern, hint in MULTILINE_RULES:
        for match in pattern.finditer(text):
            line_number = text.count("\n", 0, match.start()) + 1
            findings.append(Finding(path=path, line_number=line_number, kind=kind, hint=hint))
    return findings


def scannable(path: Path) -> bool:
    """Whether this path is worth reading: text, not vendored, not enormous."""
    if any(part in SKIP_DIRECTORIES for part in path.parts):
        return False
    if path.suffix.lower() in SKIP_SUFFIXES:
        return False
    try:
        return path.is_file() and path.stat().st_size <= MAX_BYTES
    except OSError:
        return False


def scan_files(paths: list[Path], *, include_ciphertext: bool = False) -> list[Finding]:
    """Scan the given files. Unreadable or binary content is skipped, not guessed at."""
    findings: list[Finding] = []
    for path in paths:
        if not scannable(path):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError, UnicodeDecodeError:
            continue
        findings.extend(scan_text(text, str(path), include_ciphertext=include_ciphertext))
    return findings


def tracked_files(tree: Path) -> list[Path]:
    """Every file git tracks in this tree, which is what a branch scan has to cover.

    ``git ls-files`` and not ``rglob``: the untracked ``security/`` holds the real keys on purpose,
    and an untracked scratch file is not what a commit guard is about.
    """
    result = subprocess.run(  # noqa: S603
        ["git", "-C", str(tree), "ls-files", "-z"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return [path for path in tree.rglob("*") if scannable(path)]
    return [tree / name for name in result.stdout.split("\0") if name]


def history_blobs(tree: Path) -> list[tuple[str, str]]:
    """Every blob that ever existed in this repository, as ``(sha, path)``.

    A scan of the current tree says nothing about what sits in old commits, and a key rotation
    does not change that: as long as the history exists, every old version is still openable with
    the key it was encrypted for. This is the sweep the plan asks for once -- a measurement, not a
    cleanup: what comes out of it decides whether more has to be rotated.
    """
    result = subprocess.run(  # noqa: S603
        ["git", "-C", str(tree), "rev-list", "--objects", "--all"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git rev-list failed: {result.stderr.strip()}")
    blobs: list[tuple[str, str]] = []
    for line in result.stdout.splitlines():
        sha, _, name = line.partition(" ")
        if name and not name.endswith("/"):
            blobs.append((sha, name))
    return blobs


def blob_texts(tree: Path, blobs: list[tuple[str, str]]) -> Iterator[tuple[str, str]]:
    """Stream ``(name, text)`` for every readable blob, through ONE ``git cat-file --batch``.

    A ``git cat-file -p`` per blob is a subprocess per blob, and this repository holds 47k of them
    -- minutes of pure process startup for a scan that should be a coffee break at most. ``--batch``
    takes the sha on stdin and writes ``<sha> <type> <size>\n<content>\n`` back, so one process
    serves the whole history.
    """
    names = dict(blobs)
    process = subprocess.Popen(  # noqa: S603
        ["git", "-C", str(tree), "cat-file", "--batch"],  # noqa: S607
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    if process.stdin is None or process.stdout is None:  # pragma: no cover - PIPE is requested above
        raise RuntimeError("git cat-file --batch gave no pipes")
    try:
        for sha, _name in blobs:
            process.stdin.write(f"{sha}\n".encode())
            process.stdin.flush()
            header = process.stdout.readline().decode(errors="replace").split()
            # "<sha> missing" carries no payload. Every other answer does, and it MUST be read
            # even when it is uninteresting: leaving a tree's bytes in the pipe desynchronises the
            # stream, and the next readline() then blocks forever on a header that never comes.
            # That is not theory -- it hung the first version of this sweep for nine minutes at
            # five seconds of CPU.
            if len(header) < 3:
                continue
            size = int(header[2])
            payload = process.stdout.read(size)
            process.stdout.read(1)  # the newline git writes after the content
            if header[1] != "blob" or size > MAX_BYTES:
                continue
            try:
                yield names[sha], payload.decode("utf-8")
            except UnicodeDecodeError:
                continue
    finally:
        process.stdin.close()
        process.wait()


def scan_history(tree: Path, *, include_ciphertext: bool = False) -> list[Finding]:
    """Scan every blob in the history. Slow by nature; meant to be run deliberately, not in CI."""
    blobs = [(sha, name) for sha, name in history_blobs(tree) if Path(name).suffix.lower() not in SKIP_SUFFIXES]
    findings: list[Finding] = []
    for name, text in blob_texts(tree, blobs):
        findings.extend(scan_text(text, name, include_ciphertext=include_ciphertext))
    return findings


def summarise(findings: list[Finding]) -> str:
    """A count per kind, so a long list still reads as a verdict."""
    per_kind: dict[str, int] = {}
    for finding in findings:
        per_kind[finding.kind] = per_kind.get(finding.kind, 0) + 1
    return ", ".join(f"{kind}: {count}" for kind, count in sorted(per_kind.items())) or "nothing"


#: Ciphertext is not an alarm, so it never reaches the exit code.
INVENTORY_KINDS = frozenset({"age-ciphertext"})


def split_findings(findings: list[Finding]) -> tuple[list[Finding], list[Finding]]:
    """Alarms first, inventory second.

    Committed AGE ciphertext is the point of AGE, so it must never fail a build. Reporting it under
    the same heading as a leaked key would put a permanent "FAIL" on a healthy repository, and that
    is how a guard stops being read.
    """
    alarms = [finding for finding in findings if finding.kind not in INVENTORY_KINDS]
    inventory = [finding for finding in findings if finding.kind in INVENTORY_KINDS]
    return alarms, inventory


def report(findings: list[Finding], *, what: str) -> int:
    """Print the findings and return the exit code: 0 when clean, 1 when there is an alarm."""
    alarms, inventory = split_findings(findings)
    unique = sorted({(f.path, f.line_number, f.kind, f.hint) for f in alarms})

    if unique:
        print(f"FAIL {len(unique)} findings in {what} ({summarise(alarms)}):")
        for path, line_number, kind, hint in unique:
            print(f"  {path}:{line_number}: {kind} ({hint})")
        print("\nA secret in version control has to be treated as leaked: rotate it, then remove it.")
    else:
        print(f"CLEAN no secrets found in {what}")

    if inventory:
        places = sorted({finding.path for finding in inventory})
        print(f"\nInventory: {len(inventory)} AGE ciphertext values in {len(places)} files.")
        print("Committing those is the point of AGE; this is the list a key rotation has to reach.")
        for path in places:
            count = sum(1 for finding in inventory if finding.path == path)
            print(f"  {count:4d}  {path}")

    return 1 if unique else 0
