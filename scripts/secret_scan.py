"""Scan a tree, a set of files, or the whole git history for committed secrets.

This is the guard described in ``features/sops-sleutel-roteren.md``: the pre-commit hook, the CI
jobs and a pytest test all call ``scan-secrets.py``, which calls this module. The hook does not
bind -- ``--no-verify`` is standing practice in this project, because pre-commit stashes everything
unstaged and knocks over other sessions working in the same checkout -- so the binding layers are
the ``secret-scan`` job over the tree, the weekly ``secret-scan-history`` job over the history, and
the pytest test that gives any build running the suite the same tree scan outside GitHub.

**Why a valid AGE key and not the string ``AGE-SECRET-KEY-``.** The tree holds roughly twenty
placeholders (``AGE-SECRET-KEY-1TEST``, ``AGE-SECRET-KEY-FAKE``, ``AGE-SECRET-KEY-1PLAINTEXT``)
which are neither secret nor removable without making those tests worse. A scanner that alarms on
the prefix produces findings nobody has to fix, and an alarm everyone learns to walk around is no
alarm. So an AGE candidate is only a finding when ``age-keygen -y`` accepts it: a real key.

**And it reads base64 as well as plain text.** A Kubernetes secret encodes every value, so a
manifest carrying the platform key holds no ``AGE-SECRET-KEY-`` a plain-text scan can see. Not a
hypothetical shape: the history of this repository carries one, and the sweep found it only once
this pass existed.
"""

from __future__ import annotations

import base64
import json
import re
import subprocess
from dataclasses import dataclass, replace
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

AGE_KEY_MARKER = "AGE-SECRET-KEY-"

#: Directories a WALK has no business descending into, and nothing else. This list applies only to
#: the ``rglob`` fallback in ``tracked_files``, for a tree that is not a git repository: caches and
#: installed dependencies are not committed and not what a commit guard is about.
#:
#: It deliberately holds no ``dist`` or ``build``, and it deliberately does not run on the
#: ``git ls-files`` path: where a file sits is never a reason to leave it unread. What that cost
#: while it did is measured under "Wat er NIET gelezen wordt" in
#: ``features/sops-sleutel-roteren.md``.
WALK_SKIP_DIRECTORIES = frozenset({".git", ".venv", "node_modules", "__pycache__", ".pytest_cache", ".ruff_cache"})
#: Suffixes that carry no readable text. ``.svg`` is deliberately NOT among them: it is XML, a
#: token pasted into one is as readable as in any other file, and a suffix list is the wrong place
#: to decide that a text file does not count. Content that really is binary still falls out on its
#: own, one step later, where it is counted as unread rather than dropped.
SKIP_SUFFIXES = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".ttf", ".eot", ".mp4", ".pdf", ".jar", ".zip"}
)
MAX_BYTES = 2_000_000

#: Why a file was passed over. One of these travels with every path the scan did not open.
SKIP_BINARY = "binary or media suffix"
SKIP_TOO_LARGE = "larger than the size limit"
SKIP_NOT_TEXT = "not UTF-8 text"
SKIP_UNREADABLE = "not a readable file"


@dataclass(frozen=True)
class Finding:
    """One hit: where it is, what kind, and enough context to recognise it -- never the value."""

    path: str
    line_number: int
    kind: str
    hint: str
    #: The git blob this came out of, for a history scan. Empty for a scan of files on disk, where
    #: the path IS the identity. A blob sha is what a baseline entry pins: a path moves and a line
    #: number shifts, while the sha of the content that leaked does not.
    blob: str = ""

    def __str__(self) -> str:
        return f"{self.path}:{self.line_number}: {self.kind} ({self.hint})"


@dataclass(frozen=True)
class BaselineEntry:
    """One history finding that has been dealt with: the material is retracted, the blob remains.

    Rewriting the history of this repository is not on the table, so a rotated key stays readable
    in every old commit forever. Without a baseline the weekly job reports it every week, and a job
    that is red every week is a job nobody opens.

    It pins the blob and the KIND, never the value and never the path: a list of where secrets sit
    is a map to what is not revoked yet, and this file goes to GitHub. ``scripts/README.md`` says
    the same about the scanner's output.
    """

    blob: str
    kind: str


@cache
def age_key_is_real(candidate: str) -> bool:
    """Whether ``age-keygen -y`` accepts this as a private key.

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


#: The capture group the ``url-credential`` rule puts the password in, so ``findings_in`` can hand
#: that part and not the whole URL to the check below.
URL_CREDENTIAL_GROUP = "credential"

#: The shortest password the ``url-credential`` rule treats as one. Below this it is a stub: the
#: tree holds ``user:x@``, ``postgres:p@`` and ``app:s@`` in connector tests.
MIN_URL_CREDENTIAL = 8

#: Characters that make a URL password a template rather than a value: a shell or Jinja variable,
#: an angle-bracket placeholder, a masked value. A real password holding one of these is missed,
#: and that is the trade: without it every documented example in the tree is a finding.
URL_CREDENTIAL_TEMPLATE_CHARACTERS = frozenset("${}<>*")

#: A password that spells out what it is. Matched as a substring, because the forms in this tree
#: are ``devpassword`` and ``redispass`` rather than the bare word.
URL_CREDENTIAL_PLACEHOLDER_WORDS = ("pass", "secret", "example", "changeme", "placeholder", "dummy", "redacted")

#: The sandbox password, committed on purpose in some twenty-five places and printed in
#: ``docs/sandbox/sandbox-reference.md`` as the login for every service on a local Kind cluster. It opens
#: nothing that outlives a ``task sandbox:destroy``, and alarming on it would put a permanent FAIL
#: on a healthy tree: the known finding that teaches people to ignore the scan.
COMMITTED_SANDBOX_PASSWORDS = frozenset({"admin1234"})


def looks_like_a_real_url_credential(secret: str) -> bool:
    """Whether the password in a ``scheme://user:password@host`` is a value and not a stand-in.

    Measured over ``git ls-files`` of this repository: twenty-three URLs carry something in the
    password position and not one of them is a live credential. Nine are stubs under eight
    characters, six spell out what they are, four are shell or Jinja variables, and four are the
    sandbox password. A rule without this check would report all twenty-three.
    """
    if len(secret) < MIN_URL_CREDENTIAL:
        return False
    if URL_CREDENTIAL_TEMPLATE_CHARACTERS.intersection(secret):
        return False
    lowered = secret.lower()
    if any(word in lowered for word in URL_CREDENTIAL_PLACEHOLDER_WORDS):
        return False
    return lowered not in COMMITTED_SANDBOX_PASSWORDS


#: Each rule is (kind, pattern, hint). A rule whose match needs proving carries its own check in
#: ``findings_in`` rather than being weakened into a looser regex.
RuleSet = tuple[tuple[str, re.Pattern[str], str], ...]

RULES: RuleSet = (
    ("age-private-key", re.compile(rf"{AGE_KEY_MARKER}[A-Z0-9]{{50,}}"), "a valid AGE private key"),
    ("github-pat", re.compile(r"\bghp_[A-Za-z0-9]{36,}\b"), "classic GitHub personal access token"),
    ("github-pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{50,}\b"), "fine-grained GitHub token"),
    ("github-token", re.compile(r"\b(?:gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}\b"), "GitHub OAuth/app/refresh token"),
    ("kubeconfig-token", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}\b"), "JWT"),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"), "Slack token"),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"), "AWS access key id"),
    ("gitlab-token", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b"), "GitLab personal access token"),
    (
        "aws-secret-key",
        re.compile(r"aws_secret_access_key\s*[=:]\s*[\"\']?[A-Za-z0-9/+]{40}", re.IGNORECASE),
        "AWS secret access key next to its name",
    ),
    (
        "url-credential",
        re.compile(
            rf"\b[a-z][a-z0-9+.\-]+://[A-Za-z0-9._%\-]*:(?P<{URL_CREDENTIAL_GROUP}>[^\s/@\"\'`]+)@[A-Za-z0-9.\-]"
        ),
        "a credential in a URL",
    ),
)

# A Forgejo or Gitea token is forty hex characters with no prefix, and this platform runs on
# Forgejo. There is deliberately no rule for it: forty hex characters is also a commit sha, and the
# tree and the history are full of those. Such a rule can only produce findings nobody has to fix,
# which is the one failure mode this scanner is built to avoid. An AWS secret access key has the
# same problem on its own, hence the rule above wanting the key NAME beside it.

#: Rules that need more than one line, so they run over the whole text.
#:
#: A PEM private key is its header AND a body. Matching the header alone made six findings in the
#: test suite, every one of them a header string used as test data ("BEGIN PRIVATE KEY" followed by
#: "BBBB", or by nothing at all) -- exactly the kind of known finding that teaches people to ignore
#: the scan. Requiring two lines of real base64 in between leaves those alone and still catches a
#: key, which is never shorter than that.
MULTILINE_RULES: RuleSet = (
    (
        "pem-private-key",
        re.compile(
            r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP |ENCRYPTED )?PRIVATE KEY-----\s*\n"
            r"(?:[A-Za-z0-9+/=]{32,}\s*\n){2,}"
        ),
        "PEM private key with a body",
    ),
)

#: The ciphertext forms ZAD writes itself. These are NOT findings on their own -- the whole point
#: of AGE is that the ciphertext may be committed -- so they are reported only when asked for
#: explicitly, as an inventory rather than an alarm.
CIPHERTEXT_RULES: RuleSet = (
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
    except (ValueError, UnicodeDecodeError):
        return False
    return isinstance(header, dict) and "alg" in header


#: A base64 run long enough to hide something, measured against the SHORTEST shape ``RULES``
#: accepts rather than picked as a round number. That shape is a Slack token -- ``xox[abprs]-``
#: plus ten characters, fifteen in all -- and fifteen bytes encode to exactly twenty base64
#: characters. Every other shape encodes longer: the next shortest is an AWS access key id at
#: twenty bytes, twenty-seven characters and a padding character. The count here is of the
#: character class only, which is why the padding sits outside the repetition.
#:
#: It stood at twenty-four, and there that Slack token came back CLEAN inside a Kubernetes secret
#: while the same token in plain text alarmed -- the same half-guard the base64 pass exists to
#: close. Twenty adds the runs that carry fifteen, sixteen and seventeen bytes (twenty-one
#: characters is not a decodable length), and such a run still has to decode to text before it
#: reaches a rule. Measured over ``git ls-files`` of this repository, the drop added no findings
#: and not one decoded run.
BASE64_BLOB = re.compile(r"[A-Za-z0-9+/]{20,}={0,2}")


def decoded_blobs(line: str) -> list[str]:
    """Every base64 run on this line that decodes to text, decoded.

    One level deep and no further: a secret hidden under two rounds of base64 is not a shape this
    repository produces. Runs that decode to bytes rather than text are dropped here, which is
    what keeps AGE and SOPS ciphertext -- base64 over a binary payload -- out of the second pass.
    """
    decoded: list[str] = []
    for match in BASE64_BLOB.finditer(line):
        try:
            decoded.append(base64.b64decode(match.group(0), validate=True).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            continue
    return decoded


def findings_in(text: str, path: str, line_number: int, rules: RuleSet) -> list[Finding]:
    """Apply the single-line rules to one piece of text, all of it reported on one line number."""
    findings: list[Finding] = []
    for kind, pattern, hint in rules:
        for match in pattern.finditer(text):
            if kind == "age-private-key" and not age_key_is_real(match.group(0)):
                continue
            if kind == "kubeconfig-token" and not looks_like_a_jwt(match.group(0)):
                continue
            if kind == "url-credential" and not looks_like_a_real_url_credential(match.group(URL_CREDENTIAL_GROUP)):
                continue
            findings.append(Finding(path=path, line_number=line_number, kind=kind, hint=hint))
    return findings


def multiline_findings_in(text: str, path: str, *, line_number: int | None = None) -> list[Finding]:
    """Apply the rules that need more than one line, over a whole piece of text.

    ``line_number`` overrides the one computed from the text, which is what a decoded blob needs:
    the line a commit has to lose is the encoded one, not a line inside a decoding that exists
    nowhere on disk.
    """
    findings: list[Finding] = []
    for kind, pattern, hint in MULTILINE_RULES:
        for match in pattern.finditer(text):
            where = line_number if line_number is not None else text.count("\n", 0, match.start()) + 1
            findings.append(Finding(path=path, line_number=where, kind=kind, hint=hint))
    return findings


def scan_text(text: str, path: str, *, include_ciphertext: bool = False) -> list[Finding]:
    """Every finding in one file's text, with the line number.

    An AGE candidate is checked with ``age-keygen`` before it counts and a JWT candidate has its
    header decoded, so a placeholder is not a finding. The rest is shape-based: a ``ghp_`` of the
    right length is a token whether it is live or revoked, and a scanner cannot tell those apart --
    nor should it try.

    Each line is read twice: as it stands, and with every base64 run on it decoded. A finding from
    the decoded pass keeps the line number of the ENCODED line, because that is the line a commit
    has to lose. The decoded pass runs the alarm rules only: a base64 run that unwraps to
    ciphertext is the same ciphertext, and counting it twice would inflate the inventory.

    The multi-line rules run over the decoded text as well as over the file. A PEM key in a
    Kubernetes secret's ``data`` or in a kubeconfig's ``client-key-data`` is base64 and nothing
    else, so a multi-line pass over the raw text alone saw none of them.
    """
    findings: list[Finding] = []
    rules = (*RULES, *CIPHERTEXT_RULES) if include_ciphertext else RULES
    for number, line in enumerate(text.splitlines(), start=1):
        findings.extend(findings_in(line, path, number, rules))
        for decoded in decoded_blobs(line):
            findings.extend(findings_in(decoded, path, number, RULES))
            findings.extend(multiline_findings_in(decoded, path, line_number=number))
    findings.extend(multiline_findings_in(text, path))
    return findings


def skip_reason(path: Path) -> str | None:
    """Why this file will not be read, or ``None`` when it will be.

    The whole filter, in one place and with a reason attached, so ``report`` can name what it
    passed over.
    """
    if path.suffix.lower() in SKIP_SUFFIXES:
        return SKIP_BINARY
    try:
        if not path.is_file():
            return SKIP_UNREADABLE
        if path.stat().st_size > MAX_BYTES:
            return SKIP_TOO_LARGE
    except OSError:
        return SKIP_UNREADABLE
    return None


@dataclass(frozen=True)
class ScanResult:
    """What a scan found AND what it actually opened."""

    findings: list[Finding]
    scanned: list[Path]
    skipped: list[tuple[Path, str]]

    def skips_per_reason(self) -> dict[str, int]:
        order = (SKIP_BINARY, SKIP_TOO_LARGE, SKIP_NOT_TEXT, SKIP_UNREADABLE)
        counts = {reason: sum(1 for _path, why in self.skipped if why == reason) for reason in order}
        return {reason: count for reason, count in counts.items() if count}


def scan_files(paths: list[Path], *, include_ciphertext: bool = False) -> ScanResult:
    """Scan the given files, and report which of them were read and which were passed over."""
    findings: list[Finding] = []
    scanned: list[Path] = []
    skipped: list[tuple[Path, str]] = []
    for path in paths:
        reason = skip_reason(path)
        if reason is not None:
            skipped.append((path, reason))
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            skipped.append((path, SKIP_NOT_TEXT))
            continue
        except OSError:
            skipped.append((path, SKIP_UNREADABLE))
            continue
        scanned.append(path)
        findings.extend(scan_text(text, str(path), include_ciphertext=include_ciphertext))
    return ScanResult(findings=findings, scanned=scanned, skipped=skipped)


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
        return [
            path
            for path in tree.rglob("*")
            if path.is_file() and not any(part in WALK_SKIP_DIRECTORIES for part in path.parts)
        ]
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


def blob_texts(tree: Path, blobs: list[tuple[str, str]]) -> Iterator[tuple[str, str, str]]:
    """Stream ``(sha, name, text)`` for every readable blob, through ONE ``git cat-file --batch``.

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
            if len(header) < 3:
                continue
            size = int(header[2])
            payload = process.stdout.read(size)
            process.stdout.read(1)  # the newline git writes after the content
            if header[1] != "blob" or size > MAX_BYTES:
                continue
            try:
                yield sha, names[sha], payload.decode("utf-8")
            except UnicodeDecodeError:
                continue
    finally:
        process.stdin.close()
        process.wait()


def scan_history(tree: Path, *, include_ciphertext: bool = False) -> list[Finding]:
    """Scan every blob in the history, each finding tagged with the blob it came out of.

    Slow by nature, which is why the ``secret-scan-history`` job runs it weekly on a schedule and
    not on a pull request. Nothing is subtracted here: hand the result to ``apply_baseline`` to
    take the retracted findings off it.
    """
    blobs = [(sha, name) for sha, name in history_blobs(tree) if Path(name).suffix.lower() not in SKIP_SUFFIXES]
    findings: list[Finding] = []
    for sha, name, text in blob_texts(tree, blobs):
        findings.extend(
            replace(finding, blob=sha) for finding in scan_text(text, name, include_ciphertext=include_ciphertext)
        )
    return findings


#: Where the baseline sits, relative to the root of the repository being scanned.
BASELINE_FILE = Path("scripts/secret-scan-baseline.json")


def load_baseline(path: Path) -> list[BaselineEntry]:
    """Read the baseline, or return nothing when there is none.

    An absent file is not an error: a clone without one simply has no retracted findings on
    record, and the scan then reports everything it sees.

    The blobs are grouped per kind under the one judgement that accounts for them, so the reason a
    finding is accepted stands once rather than forty-seven times. The reason is for the reader;
    nothing here acts on it.
    """
    if not path.is_file():
        return []
    document = json.loads(path.read_text(encoding="utf-8"))
    return [BaselineEntry(blob=blob, kind=group["kind"]) for group in document["retracted"] for blob in group["blobs"]]


def apply_baseline(findings: list[Finding], baseline: list[BaselineEntry]) -> tuple[list[Finding], list[BaselineEntry]]:
    """The findings the baseline does not account for, and the entries that accounted for nothing.

    The second half is what keeps the baseline from growing quietly. An entry whose blob no longer
    turns up has either been garbage collected or stopped being a finding, and either way it is
    now a line that silences nothing while reading as if it does.
    """
    retracted = {(entry.blob, entry.kind) for entry in baseline}
    seen = {(finding.blob, finding.kind) for finding in findings}
    remaining = [finding for finding in findings if (finding.blob, finding.kind) not in retracted]
    stale = [entry for entry in baseline if (entry.blob, entry.kind) not in seen]
    return remaining, stale


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


def report(
    findings: list[Finding],
    *,
    what: str,
    skipped: dict[str, int] | None = None,
    stale_baseline: list[BaselineEntry] | None = None,
) -> int:
    """Print the findings and return the exit code: 0 when clean, 1 when there is an alarm.

    ``skipped`` is what the scan did NOT open, per reason. It is printed under both verdicts and
    not only under FAIL: the moment "CLEAN" can cover fewer files than the operator handed over,
    the count of the difference is part of the verdict rather than a footnote to it.

    ``stale_baseline`` is the entries that matched nothing, printed but deliberately NOT failing
    the run: a line too many in the baseline silences nothing, so it is housekeeping and not an
    alarm.
    """
    alarms, inventory = split_findings(findings)
    unique = sorted({(f.path, f.line_number, f.kind, f.hint) for f in alarms})

    if unique:
        print(f"FAIL {len(unique)} findings in {what} ({summarise(alarms)}):")
        for path, line_number, kind, hint in unique:
            print(f"  {path}:{line_number}: {kind} ({hint})")
        print("\nA secret in version control has to be treated as leaked: rotate it, then remove it.")
    else:
        print(f"CLEAN no secrets found in {what}")

    if skipped:
        per_reason = ", ".join(f"{reason}: {count}" for reason, count in skipped.items())
        print(f"Unread: {sum(skipped.values())} files were not opened ({per_reason})")

    if stale_baseline:
        print(f"\nBaseline: {len(stale_baseline)} entries matched nothing in this run and can go:")
        for entry in sorted(stale_baseline, key=lambda entry: (entry.kind, entry.blob)):
            print(f"  {entry.blob} {entry.kind}")

    if inventory:
        places = sorted({finding.path for finding in inventory})
        print(f"\nInventory: {len(inventory)} AGE ciphertext values in {len(places)} files.")
        print("Committing those is the point of AGE; this is the list a key rotation has to reach.")
        for path in places:
            count = sum(1 for finding in inventory if finding.path == path)
            print(f"  {count:4d}  {path}")

    return 1 if unique else 0
