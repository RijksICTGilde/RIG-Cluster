"""Tests for scripts/secret_scan.py: the guard against committing a secret.

The plan calls for a counter-check: offer a commit with a key-shaped string and expect it to be
refused. Without that, the guard goes soft at the next configuration change and nobody notices --
a scan that never says no is indistinguishable from no scan.

So the tests here come in pairs. For each rule: a real secret must be a finding, and the
placeholder shape that lives in this tree must NOT be. That second half is the one that keeps the
guard believable: an alarm with known findings in it is an alarm people learn to walk around, and
it is why the tree had to be cleaned before this was switched on.

The three layers are wired up elsewhere, and the wiring is checked here too, because a hook or a
job that silently stops calling the scan is the other way this rots:

* layer 1, ``.pre-commit-config.yaml`` -- local, bypassable with ``--no-verify``, accepted;
* layer 2, ``.github/workflows/security.yml`` -- binding, scans the whole tree of the branch;
* layer 3, GitHub push protection -- on the server, and not something a test can reach.
"""

from __future__ import annotations

import ast
import base64
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import yaml
from opi.utils.sops import generate_sops_key_pair

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from secret_scan import (  # noqa: E402
    BASE64_BLOB,
    MAX_BYTES,
    RULES,
    SKIP_BINARY,
    SKIP_NOT_TEXT,
    SKIP_SUFFIXES,
    SKIP_TOO_LARGE,
    WALK_SKIP_DIRECTORIES,
    Finding,
    age_key_is_real,
    decoded_blobs,
    history_blobs,
    looks_like_a_jwt,
    report,
    scan_files,
    scan_history,
    scan_text,
    skip_reason,
    split_findings,
    tracked_files,
)

needs_age = pytest.mark.skipif(shutil.which("age-keygen") is None, reason="requires the age-keygen binary")

#: Every sample below is COMPOSED from pieces rather than written out, so no literal that the
#: scanner recognises exists in this file. That is not decoration: the scan covers the whole tree,
#: this file included, and the first version of it failed its own last test on two sample tokens.
#:
#: The alternative -- an inline "ignore this line" marker -- was rejected. It would be the one
#: bypass in a guard whose entire point is that ``--no-verify`` must not get past it, and a marker
#: that exists gets used. Composing the samples costs a little readability and adds no way out.
_JWT_HEADER = "eyJ" + "hbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
FAKE_JWT = _JWT_HEADER + ".eyJpc3MiOiJhcmdvY2QiLCJzdWIiOiJwcm9qOmRlZmF1bHQ6dGVzdCJ9.c2lnbmF0dXJl"
FAKE_SLACK_TOKEN = "xox" + "b-1234567890-abcdefghij"
FAKE_AWS_KEY = "AKI" + "AIOSFODNN7EXAMPLE"

#: The shortest first segment the JWT rule accepts. ``{"alg":11}`` base64url-encodes to fourteen
#: characters and the rule wants ``eyJ`` plus ten; a shorter header (``{"alg":1}``) encodes to
#: twelve, which the rule rightly leaves alone.
SHORTEST_JWT = "eyJ" + "hbGciOjExfQ" + "." + "b" * 10 + "." + "c" * 5

#: The SHORTEST string each alarm rule accepts, which is what the base64 threshold has to sit
#: under. A roomy example value proves nothing about that boundary: ``FAKE_SLACK_TOKEN`` encodes
#: to thirty-six characters and clears any plausible threshold, while the shortest token its own
#: rule accepts encodes to exactly twenty -- see ``BASE64_BLOB``.
#:
#: ``age-private-key`` is deliberately absent: the only thing that rule accepts is a key
#: ``age-keygen -y`` agrees with, so its length is not ours to choose, and
#: ``test_a_key_inside_a_kubernetes_secret_is_a_finding`` covers it through base64 already.
SHORTEST_ALARM_SHAPES: tuple[tuple[str, str], ...] = (
    ("github-pat", "gh" + "p_" + "a" * 36),
    ("github-pat", "github" + "_pat_" + "b" * 50),
    ("github-token", "gh" + "s_" + "c" * 36),
    ("kubeconfig-token", SHORTEST_JWT),
    ("slack-token", "xox" + "b-" + "d" * 10),
    ("aws-access-key", "AKI" + "A" + "E" * 16),
)


# ---------------------------------------------------------------------------
# an AGE key: a real one is a finding, a placeholder is not
# ---------------------------------------------------------------------------


@needs_age
def test_a_real_age_private_key_is_a_finding() -> None:
    """The counter-check the plan asks for, on the shape that started this task."""
    private_key, _public_key = generate_sops_key_pair()

    findings = scan_text(f'PRIVATE_KEY = "{private_key}"\n', "tests/whatever.py")

    assert [finding.kind for finding in findings] == ["age-private-key"]
    assert findings[0].line_number == 1


@needs_age
@pytest.mark.parametrize(
    "placeholder",
    [
        "AGE-SECRET-KEY-1TEST",
        "AGE-SECRET-KEY-1ABCDEF",
        "AGE-SECRET-KEY-FAKE",
        "AGE-SECRET-KEY-1PLAINTEXT",
        "AGE-SECRET-KEY-1PROJECT",
        # A string of the right length that is not a valid key: the bech32 checksum fails.
        "AGE-SECRET-KEY-" + "Q" * 59,
    ],
)
def test_a_placeholder_key_is_not_a_finding(placeholder: str) -> None:
    """These shapes really live in this tree, in about twenty places.

    A scanner matching the prefix reports them all, and an alarm with known findings in it gets
    ignored -- which would make the guard worthless while looking like it works.
    """
    assert scan_text(f'KEY = "{placeholder}"\n', "tests/whatever.py") == []


@needs_age
def test_age_key_validity_is_what_the_rule_hangs_on() -> None:
    private_key, _public_key = generate_sops_key_pair()
    assert age_key_is_real(private_key)
    assert not age_key_is_real("AGE-SECRET-KEY-1TEST")
    assert not age_key_is_real("AGE-SECRET-KEY-" + "Q" * 59)


@needs_age
def test_the_public_half_is_not_a_finding() -> None:
    """A public key is not a secret and belongs in documentation and in SOPS metadata."""
    _private_key, public_key = generate_sops_key_pair()
    assert scan_text(f"recipient: {public_key}\n", "some.sops.yaml") == []


# ---------------------------------------------------------------------------
# the other shapes
# ---------------------------------------------------------------------------


@needs_age
@pytest.mark.parametrize(
    ("text", "kind"),
    [
        (f'token = "gh{"p"}_{"a" * 36}"', "github-pat"),
        (f'token = "github{"_pat_"}{"b" * 60}"', "github-pat"),
        (f'token = "gh{"s"}_{"c" * 36}"', "github-token"),
        (f'export ARGOCD_TOKEN="{FAKE_JWT}"', "kubeconfig-token"),
        (f'key = "{FAKE_SLACK_TOKEN}"', "slack-token"),
        (f'id = "{FAKE_AWS_KEY}"', "aws-access-key"),
    ],
)
def test_each_token_shape_is_a_finding(text: str, kind: str) -> None:
    findings = scan_text(text + "\n", "somewhere.md")
    assert [finding.kind for finding in findings] == [kind]


@needs_age
def test_a_pem_private_key_with_a_body_is_a_finding() -> None:
    body = "\n".join(["MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQ" * 2] * 3)
    text = f"-----BEGIN PRIVATE KEY-----\n{body}\n-----END PRIVATE KEY-----\n"

    findings = scan_text(text, "some.pem")

    assert [finding.kind for finding in findings] == ["pem-private-key"]


@needs_age
@pytest.mark.parametrize(
    "text",
    [
        # A header used as an assertion string.
        'assert "-----BEGIN PRIVATE KEY-----" not in source\n',
        # A header with a stub body, as several tests in this tree use.
        "-----BEGIN OPENSSH PRIVATE KEY-----\nFAKEKEYMATERIAL==\n-----END OPENSSH PRIVATE KEY-----\n",
        "-----BEGIN PRIVATE KEY-----\nBBBB\n-----END PRIVATE KEY-----\n",
        "-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----\n",
    ],
)
def test_a_pem_header_without_a_real_body_is_not_a_finding(text: str) -> None:
    """Matching the header alone produced six findings in this tree, all of them test data.

    These four shapes are taken from the files it hit: test_argo_repository_no_hardcoded_key,
    test_publish_passthrough and test_attachment_catalog_model.
    """
    assert scan_text(text, "tests/whatever.py") == []


@needs_age
def test_a_jwt_shaped_string_without_a_real_header_is_not_a_finding() -> None:
    """Three dot-separated chunks starting with eyJ, but the header is not a JWT header."""
    not_a_header = "eyJ" + "ub3RhaGVhZGVy"
    assert scan_text(f"{not_a_header}.eyJub3RhcGF5bG9hZA.c2lnbmF0dXJl\n", "somewhere.md") == []
    assert looks_like_a_jwt(FAKE_JWT)
    assert not looks_like_a_jwt(f"{not_a_header}.x.y")


@needs_age
def test_age_ciphertext_is_not_a_finding_but_can_be_inventoried() -> None:
    """Committing AGE ciphertext is the point of AGE, so it is never an alarm.

    It is still worth being able to list: that inventory is how you find the places a key rotation
    has to reach.
    """
    text = "password: base64+age:QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo=\n"

    assert scan_text(text, "project.yaml") == []
    inventory = scan_text(text, "project.yaml", include_ciphertext=True)
    assert [finding.kind for finding in inventory] == ["age-ciphertext"]


def test_ciphertext_never_reaches_the_exit_code(capsys: pytest.CaptureFixture) -> None:
    """A permanent FAIL on a healthy repository is how a guard stops being read.

    Every SOPS file and every project file carries AGE ciphertext, so reporting that as a failure
    would put the build red forever and teach everyone to ignore the job.
    """
    inventory_only = [Finding(path="project.yaml", line_number=3, kind="age-ciphertext", hint="block")]

    assert report(inventory_only, what="a tree") == 0
    printed = capsys.readouterr().out
    assert "CLEAN" in printed
    assert "Inventory: 1 AGE ciphertext values" in printed


def test_an_alarm_next_to_an_inventory_still_fails(capsys: pytest.CaptureFixture) -> None:
    mixed = [
        Finding(path="project.yaml", line_number=3, kind="age-ciphertext", hint="block"),
        Finding(path="notes.md", line_number=9, kind="github-pat", hint="token"),
    ]

    assert report(mixed, what="a tree") == 1
    printed = capsys.readouterr().out
    assert "FAIL 1 findings" in printed
    assert "Inventory: 1 AGE ciphertext" in printed


def test_the_split_puts_ciphertext_on_the_inventory_side() -> None:
    alarms, inventory = split_findings(
        [
            Finding(path="a", line_number=1, kind="age-ciphertext", hint="x"),
            Finding(path="b", line_number=1, kind="age-private-key", hint="y"),
        ]
    )
    assert [finding.kind for finding in alarms] == ["age-private-key"]
    assert [finding.kind for finding in inventory] == ["age-ciphertext"]


# ---------------------------------------------------------------------------
# base64: a Kubernetes secret hides the same key in plain sight
# ---------------------------------------------------------------------------


@needs_age
def test_a_key_inside_a_kubernetes_secret_is_a_finding() -> None:
    """The gap this closes, on the exact manifest ``docs/sops-en-age-met-de-hand.md`` step 2 makes.

    Measured before the base64 pass existed: the same key in a ``.py`` failed the scan, and this
    manifest came back CLEAN -- while neither ``sops-secret.yaml`` nor ``sops-key.txt`` is covered
    by ``.gitignore`` in the repository root. So hook, CI and history sweep all said CLEAN over a
    commit carrying the platform key.
    """
    private_key, _public_key = generate_sops_key_pair()
    encoded = base64.b64encode(f"# public key: whatever\n{private_key}\n".encode()).decode()
    manifest = f"apiVersion: v1\nkind: Secret\nmetadata:\n  name: sops-age-key\ndata:\n  key: {encoded}\n"

    findings = scan_text(manifest, "sops-secret.yaml")

    assert [finding.kind for finding in findings] == ["age-private-key"]
    # The ENCODED line, because that is the line the commit has to lose -- not a line number
    # inside a decoding that exists nowhere on disk.
    assert findings[0].line_number == 6


@needs_age
@pytest.mark.parametrize(("kind", "secret"), SHORTEST_ALARM_SHAPES)
def test_every_alarm_rule_reaches_through_base64(kind: str, secret: str) -> None:
    """Not only the AGE rule: the pass runs the whole alarm set over the decoded text.

    A base64 layer that covered one rule would be the next half-guard -- a token in a secret's
    ``data`` is the same manifest with a different field.

    Parametrised on the SHORTEST shape each rule accepts, and encoding the BARE value, because
    that is the case the base64 threshold decides. The first version used example values and
    encoded ``token: `` along with them; every sample cleared the threshold by a wide margin, so
    this test passed green while a minimum-length Slack token in a secret's ``data`` was CLEAN.

    The plain assertion comes first on purpose. Without it a sample could stop being a shape the
    rule accepts at all and the base64 half would still pass -- proving that a rule which alarms
    on nothing alarms on nothing through base64 too.
    """
    assert [finding.kind for finding in scan_text(f"token: {secret}\n", "somewhere.md")] == [kind]

    encoded = base64.b64encode(secret.encode()).decode()

    assert [finding.kind for finding in scan_text(f"data:\n  token: {encoded}\n", "secret.yaml")] == [kind]


def test_the_base64_threshold_sits_exactly_on_the_shortest_shape_the_rules_accept() -> None:
    """The number in ``BASE64_BLOB``, pinned from both sides so it cannot drift back up.

    The test above goes red if the threshold rises, but only for the shapes that are in the table.
    This one pins the relationship itself: the threshold IS the shortest encoding -- it matches a
    run of exactly that length and not one character less -- so moving the number either way is
    red here even if the table is never read again.
    """
    encoded = {kind: base64.b64encode(secret.encode()).decode() for kind, secret in SHORTEST_ALARM_SHAPES}
    for kind, blob in encoded.items():
        assert BASE64_BLOB.fullmatch(blob), f"{kind}: {len(blob)} characters does not reach the threshold"

    shortest = min(len(blob) for blob in encoded.values())

    assert shortest == 20, f"the shortest encoding is now {shortest}; the threshold has to follow"
    assert BASE64_BLOB.fullmatch("A" * shortest) is not None
    assert BASE64_BLOB.search("A" * (shortest - 1)) is None


def test_every_alarm_rule_has_a_shortest_shape_on_record() -> None:
    """Which is what makes the threshold above measured rather than remembered.

    Both tests above only see the shapes in ``SHORTEST_ALARM_SHAPES``. A rule added to ``RULES``
    without an entry there would be a shape nobody compared against the threshold -- and a rule
    shorter than the Slack one would silently reopen exactly the gap this round closed. So the
    table is required to hold one entry per rule, with ``age-private-key`` the single documented
    exception because its length is fixed by ``age-keygen``.
    """
    on_record = {kind for kind, _secret in SHORTEST_ALARM_SHAPES} | {"age-private-key"}

    assert on_record == {kind for kind, _pattern, _hint in RULES}
    assert len(SHORTEST_ALARM_SHAPES) + 1 == len(RULES), "one shortest shape per rule, not per kind"


@needs_age
def test_a_placeholder_inside_base64_is_still_not_a_finding() -> None:
    """The proof rules survive the decoding, so the second pass cannot reintroduce the noise.

    Without this, every base64 blob in the tree that happens to decode to text would get a second
    chance at producing a finding nobody has to fix.
    """
    encoded = base64.b64encode(b'KEY = "AGE-SECRET-KEY-1TEST"\n').decode()
    assert scan_text(f"data:\n  key: {encoded}\n", "secret.yaml") == []


def test_base64_over_binary_is_left_alone() -> None:
    """Which is what keeps AGE and SOPS ciphertext out of the second pass: it is not text.

    Nearly every base64 run in this repository is ciphertext. Decoding those to bytes and handing
    them to the rules would cost time on every file and could only ever produce noise.
    """
    assert decoded_blobs(base64.b64encode(bytes(range(256))).decode()) == []
    assert decoded_blobs("short") == []


@needs_age
def test_ciphertext_is_not_counted_twice_through_its_own_base64() -> None:
    """``base64+age:`` is base64 over an AGE block, so the inventory could count it once encoded
    and once decoded. The decoded pass runs alarm rules only, which is what stops that."""
    block = "-----BEGIN AGE ENCRYPTED FILE-----\nYWJj\n-----END AGE ENCRYPTED FILE-----\n"
    text = f"password: base64+age:{base64.b64encode(block.encode()).decode()}\n"

    inventory = scan_text(text, "project.yaml", include_ciphertext=True)

    assert [finding.kind for finding in inventory] == ["age-ciphertext"]


@needs_age
def test_a_key_split_over_two_base64_lines_is_not_claimed_to_be_covered() -> None:
    """The stated limit, pinned so it stays stated: the pass reads one LINE at a time.

    A value wrapped over several lines is not a shape ``kubectl -o yaml`` produces, and joining
    adjacent base64 lines would have to guess where a blob ends. Pinning the limit is what keeps
    the docs honest about it rather than letting a reader assume full coverage.
    """
    private_key, _public_key = generate_sops_key_pair()
    encoded = base64.b64encode(f"{private_key}\n".encode()).decode()
    half = len(encoded) // 2

    assert scan_text(f"data:\n  key: {encoded}\n", "one-line.yaml") != []
    assert scan_text(f"data:\n  key: |\n    {encoded[:half]}\n    {encoded[half:]}\n", "wrapped.yaml") == []


# ---------------------------------------------------------------------------
# what is scanned, and what is left alone
# ---------------------------------------------------------------------------


@needs_age
def test_a_binary_suffix_is_skipped(tmp_path: Path) -> None:
    private_key, _public_key = generate_sops_key_pair()
    (tmp_path / "logo.png").write_text(private_key)
    (tmp_path / "config.yaml").write_text(private_key)

    result = scan_files([tmp_path / "logo.png", tmp_path / "config.yaml"])

    assert [Path(finding.path).name for finding in result.findings] == ["config.yaml"]
    assert [(path.name, why) for path, why in result.skipped] == [("logo.png", SKIP_BINARY)]


@needs_age
def test_a_built_bundle_is_scanned_like_any_other_tracked_file(tmp_path: Path) -> None:
    """The measured hole: ``dist`` and ``build`` were skipped on the tracked path as well.

    A bundle with a token baked into it is one of the most ordinary leak shapes there is, and this
    repository really does track committed files under ``presentation/reveal/dist/``. Both layers
    of the guard call ``scan_files``, so a directory rule here took the finding away from both at
    once -- and said "CLEAN" while doing it.
    """
    private_key, _public_key = generate_sops_key_pair()
    for name in ("dist/vendor.js", "build/out.txt", "node_modules/pkg/index.js", "src/app.py"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'KEY = "{private_key}"\n')
    paths = [tmp_path / name for name in ("dist/vendor.js", "build/out.txt", "node_modules/pkg/index.js", "src/app.py")]

    result = scan_files(paths)

    assert sorted(Path(finding.path).name for finding in result.findings) == [
        "app.py",
        "index.js",
        "out.txt",
        "vendor.js",
    ]
    assert result.skipped == []


def test_where_a_file_sits_is_never_a_reason_to_leave_it_unread(tmp_path: Path) -> None:
    """The counterpart of the test above, and the one that catches the repair being undone by
    hand: the directory list may only be consulted while WALKING a tree that git cannot list."""
    for name in ("dist", "build", "node_modules", ".git"):
        path = tmp_path / name / "thing.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("nothing secret\n")
        assert skip_reason(path) is None, f"{name} is skipped for where it sits"

    assert "dist" not in WALK_SKIP_DIRECTORIES
    assert "build" not in WALK_SKIP_DIRECTORIES


@needs_age
def test_an_svg_is_text_and_is_read_like_any_other_file(tmp_path: Path) -> None:
    """It sat on the media list between the PNGs, and it is the one entry there that is text:
    a key pasted into XML reads exactly like a key pasted into YAML."""
    private_key, _public_key = generate_sops_key_pair()
    (tmp_path / "diagram.svg").write_text(f"<svg><desc>{private_key}</desc></svg>\n")

    result = scan_files([tmp_path / "diagram.svg"])

    assert [finding.kind for finding in result.findings] == ["age-private-key"]
    assert ".svg" not in SKIP_SUFFIXES


def test_a_file_over_the_size_limit_is_left_unread_and_counted(tmp_path: Path) -> None:
    """The size limit is a real hole in the guard, so it has to be a visible one.

    Exactly on the limit is still read: the boundary is pinned here because nothing else pins it,
    and a limit that drifts downwards takes coverage with it without a word.
    """
    (tmp_path / "at_the_limit.yaml").write_text("a" * MAX_BYTES)
    (tmp_path / "over_the_limit.yaml").write_text("a" * (MAX_BYTES + 1))

    result = scan_files([tmp_path / "at_the_limit.yaml", tmp_path / "over_the_limit.yaml"])

    assert [path.name for path in result.scanned] == ["at_the_limit.yaml"]
    assert [(path.name, why) for path, why in result.skipped] == [("over_the_limit.yaml", SKIP_TOO_LARGE)]


def test_content_that_is_not_text_is_counted_as_unread_instead_of_dropped(tmp_path: Path) -> None:
    """A binary file without a known suffix used to fall out of the scan in silence.

    It still cannot be scanned -- there is nothing to read -- but it is now counted, which is the
    difference between "CLEAN over everything" and "CLEAN over what could be opened".
    """
    (tmp_path / "blob.bin").write_bytes(b"\xff\xfe\x00\x01binary")

    result = scan_files([tmp_path / "blob.bin", tmp_path / "gone.yaml"])

    assert result.scanned == []
    assert [why for _path, why in result.skipped] == [SKIP_NOT_TEXT, "not a readable file"]


def test_the_verdict_says_how_many_files_were_passed_over(capsys: pytest.CaptureFixture) -> None:
    """ "CLEAN" has to be a statement about a number the operator can check.

    It printed ``len(paths)`` from BEFORE the filtering, so a clean verdict over the 2765 tracked
    files of this repository was a statement about the 2604 it really opened. The numbers below
    are that measurement. Under FAIL as well: a run that finds something is exactly the run where
    the rest of the coverage matters.
    """
    skipped = {SKIP_BINARY: 143, SKIP_TOO_LARGE: 1, SKIP_NOT_TEXT: 8}

    assert report([], what="2613 of 2765 tracked files", skipped=skipped) == 0
    clean = capsys.readouterr().out
    assert "CLEAN no secrets found in 2613 of 2765 tracked files" in clean
    assert (
        "Unread: 152 files were not opened "
        "(binary or media suffix: 143, larger than the size limit: 1, not UTF-8 text: 8)" in clean
    )

    alarm = [Finding(path="notes.md", line_number=9, kind="github-pat", hint="token")]
    assert report(alarm, what="2613 of 2765 tracked files", skipped=skipped) == 1
    assert "Unread: 152 files" in capsys.readouterr().out


@needs_age
def test_the_whole_tree_scan_counts_what_it_read_and_not_what_it_was_handed(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """The entry point both layers share, end to end: the hook and the CI job print this line."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@e.invalid"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "T"], check=True)
    (tmp_path / "logo.png").write_text("not really a png\n")
    (tmp_path / "notes.md").write_text("nothing secret\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "-m", "x"], check=True)
    scan_module = _scan_secrets_module()

    assert scan_module.main(["--tree", str(tmp_path)]) == 0

    printed = capsys.readouterr().out
    assert "1 of 2 tracked files" in printed
    assert "Unread: 1 files were not opened (binary or media suffix: 1)" in printed


@needs_age
def test_only_tracked_files_are_scanned(tmp_path: Path) -> None:
    """``security/`` holds the real keys on purpose, and it is untracked.

    Scanning the working tree instead of ``git ls-files`` would report those on every run, and the
    guard is about what gets committed.
    """
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@e.invalid"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "T"], check=True)
    private_key, _public_key = generate_sops_key_pair()
    (tmp_path / "security").mkdir()
    (tmp_path / "security" / "key.txt").write_text(private_key)
    (tmp_path / "committed.txt").write_text("nothing secret\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "committed.txt"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "-m", "x"], check=True)

    paths = tracked_files(tmp_path)

    assert [path.name for path in paths] == ["committed.txt"]
    assert scan_files(paths).findings == []


# ---------------------------------------------------------------------------
# the one-off sweep over the history
# ---------------------------------------------------------------------------


def _repo_with(tmp_path: Path, files: dict[str, str]) -> Path:
    """A git repository holding these files, one commit per file."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@e.invalid"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "T"], check=True)
    for name, content in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        subprocess.run(["git", "-C", str(tmp_path), "add", name], check=True)
        subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "-m", f"add {name}"], check=True)
    return tmp_path


@needs_age
def test_a_secret_deleted_from_the_tree_is_still_found_in_the_history(tmp_path: Path) -> None:
    """The reason the sweep exists: removing the file does not remove the secret."""
    private_key, _public_key = generate_sops_key_pair()
    repo = _repo_with(tmp_path, {"leaked.py": f'KEY = "{private_key}"\n'})
    (repo / "leaked.py").unlink()
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-am", "remove it"], check=True)

    assert scan_files(tracked_files(repo)).findings == []

    findings = scan_history(repo)

    assert [(finding.path, finding.kind) for finding in findings] == [("leaked.py", "age-private-key")]


@needs_age
@pytest.mark.timeout(60)
def test_the_history_sweep_finishes_on_a_repository_that_has_subdirectories(tmp_path: Path) -> None:
    """The measured failure: a non-blob object left unread in the batch pipe desynchronises it.

    A subdirectory is the smallest shape that puts a tree object in the listing, so it takes one
    to reproduce this at all. Both outcomes of the desynchronisation fail the test: garbage where
    a header belongs raises, and the timeout catches the version that waited forever.
    """
    private_key, _public_key = generate_sops_key_pair()
    repo = _repo_with(
        tmp_path,
        {
            "nested/deep/one.txt": "nothing to see\n",
            "nested/two.txt": "also nothing\n",
            "zz-last.py": f'KEY = "{private_key}"\n',
        },
    )

    named = [name for _sha, name in history_blobs(repo)]
    assert "nested" in named, "no tree object in the listing, so this would not reproduce it"

    findings = scan_history(repo)

    assert [finding.path for finding in findings] == ["zz-last.py"]


# ---------------------------------------------------------------------------
# the layers are actually wired up
# ---------------------------------------------------------------------------


def test_the_pre_commit_hook_calls_the_scan() -> None:
    """Layer 1. A hook that quietly stops calling the scan is one of the ways this rots."""
    config = yaml.safe_load((_REPO_ROOT / ".pre-commit-config.yaml").read_text())
    hooks = [hook for repo in config["repos"] for hook in repo.get("hooks", [])]
    scan_hooks = [hook for hook in hooks if hook.get("id") == "secret-scan"]

    assert len(scan_hooks) == 1
    assert "scripts/scan-secrets.py" in scan_hooks[0]["entry"]
    assert scan_hooks[0]["pass_filenames"] is True


def test_every_script_parses_on_the_oldest_python_the_hook_may_meet() -> None:
    """Layer 1 runs on whatever ``python3`` the developer has, so 3.14-only syntax refuses every commit.

    Why the pin under ``scripts/`` exists at all: ``scripts/README.md``, "Aanroepen". Nothing
    else catches a breach of it -- ``ruff format`` calls the file formatted either way, and the
    ruff hooks in ``.pre-commit-config.yaml`` are limited to ``^operations-manager/python/``, so
    no gate runs ruff over ``scripts/``.
    """

    def version_of(text: str) -> tuple[int, int]:
        """``py312`` as ``(3, 12)``."""
        target = tomllib.loads(text)
        setting = target.get("target-version") or target["tool"]["ruff"]["target-version"]
        return int(setting[2]), int(setting[3:])

    floor = version_of((_SCRIPTS_DIR / ".ruff.toml").read_text())
    venv = version_of((_REPO_ROOT / "operations-manager" / "python" / "pyproject.toml").read_text())
    assert floor < venv, "the pin only guards anything while it sits below the version the venv pins"

    scripts = sorted(_SCRIPTS_DIR.glob("*.py"))
    assert scripts, f"nothing matched in {_SCRIPTS_DIR}, so this would pass on an empty set"

    broken: list[str] = []
    for script in scripts:
        try:
            ast.parse(script.read_text(), filename=str(script), feature_version=floor)
        except SyntaxError as e:
            broken.append(f"{script.name}:{e.lineno}: {e.msg}")

    assert broken == [], f"not parseable on python {floor[0]}.{floor[1]}:\n" + "\n".join(broken)


def test_ci_scans_the_whole_tree_and_not_the_diff() -> None:
    """Layer 2, the one that binds: it knows no ``--no-verify``.

    The tree and not the diff, so something that arrives by a rebase, a merge or a commit made
    with ``--no-verify`` still shows up. Passing ``--files`` here would undo exactly that.
    """
    workflow = yaml.safe_load((_REPO_ROOT / ".github" / "workflows" / "security.yml").read_text())
    job = workflow["jobs"]["secret-scan"]
    commands = [step.get("run", "") for step in job["steps"]]

    assert any("scripts/scan-secrets.py" in command for command in commands)
    assert not any("--files" in command for command in commands)
    # age-keygen has to be there, or every placeholder in the suite becomes a finding.
    assert any("age" in command for command in commands)
    # On every pull request, and on a push to main or main_github.
    triggers = workflow[True] if True in workflow else workflow["on"]
    assert "pull_request" in triggers
    assert set(triggers["push"]["branches"]) >= {"main", "main_github"}


def test_every_flag_the_docs_hand_an_operator_exists_on_the_scanner() -> None:
    """The invocations live in four files outside the scanner, and a paste is only as good as its flags.

    The history run moved to ``scripts/README.md`` when the document holding its output left the
    repository, so the one command that takes a quarter of an hour to find out is documented in
    a file the parser knows nothing about. Renaming a flag keeps the option list above green --
    it is measured against the parser -- and leaves every documented line wrong.
    """
    options = {option for action in _scan_secrets_module().build_parser()._actions for option in action.option_strings}
    sources = (
        _REPO_ROOT / "scripts" / "README.md",
        _REPO_ROOT / "features" / "sops-sleutel-vervangen.md",
        _REPO_ROOT / ".pre-commit-config.yaml",
        _REPO_ROOT / ".github" / "workflows" / "security.yml",
    )

    seen = 0
    for source in sources:
        for line in source.read_text().splitlines():
            if "scan-secrets.py" not in line:
                continue
            # Up to the trailing "# een andere clone": a comment is prose, not an argument.
            arguments = line.split("scan-secrets.py", 1)[1].split("#", 1)[0]
            for flag in re.findall(r"--[a-z][a-z-]*", arguments):
                seen += 1
                assert flag in options, f"{source.name} documents {flag}, which the scanner does not have"

    assert seen >= 4, "the documented invocations lost their flags, so this checks nothing"


def _scan_secrets_module():
    """The hyphenated CLI, loaded by path and registered so its own module-level code can run."""
    existing = sys.modules.get("scan_secrets")
    if existing is not None:
        return existing
    import importlib.util

    spec = importlib.util.spec_from_file_location("scan_secrets", _SCRIPTS_DIR / "scan-secrets.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["scan_secrets"] = module
    spec.loader.exec_module(module)
    return module


def test_the_scan_refuses_to_run_without_age_keygen(monkeypatch: pytest.MonkeyPatch) -> None:
    """Silently scanning without the validity check would report the ~20 placeholders.

    Better to fail loudly than to produce a finding list nobody can act on.
    """
    scan_module = _scan_secrets_module()

    monkeypatch.setattr(scan_module.shutil, "which", lambda _name: None)
    assert scan_module.main([]) == 2


@needs_age
def test_this_repository_is_clean_right_now() -> None:
    """The acceptance criterion: a scan of the working tree gives zero findings.

    First the tree, then the guard -- otherwise you build an alarm everyone learns to work around.
    This is the assertion that keeps it that way, and it is the same code CI runs.
    """
    findings = scan_files(tracked_files(_REPO_ROOT)).findings

    assert findings == [], "\n".join(str(finding) for finding in findings)


@needs_age
def test_empty_files_does_not_fall_through_to_the_whole_tree(capsys: pytest.CaptureFixture) -> None:
    """``--files`` with nothing after it must scan nothing, not everything.

    A truthiness test on the list would make the hook silently scan the whole tree, which is the
    opposite of what the hook is for.
    """
    scan_module = _scan_secrets_module()

    assert scan_module.main(["--files"]) == 0
    assert "0 staged files" in capsys.readouterr().out


def test_the_scan_has_no_way_to_write_its_findings_to_a_file() -> None:
    """The findings never go to git, and the script enforces that by not being able to.

    The rule and its reason are in ``scripts/README.md``; it is only as good as the absence of a
    flag that names an output path, because that flag is one step away from a commit. So this
    pins the option list and the fact that both modules only ever print.
    """
    scan_module = _scan_secrets_module()

    options = {option for action in scan_module.build_parser()._actions for option in action.option_strings}

    assert options == {"-h", "--help", "--files", "--history", "--inventory", "--tree"}, (
        "a new flag on the scanner: if it names an output path, the findings can land in git"
    )

    for name in ("scan-secrets.py", "secret_scan.py"):
        source = (_SCRIPTS_DIR / name).read_text()
        assert "write_text(" not in source, f"{name} writes a file; findings belong on stdout"
        assert not re.search(r"open\([^)]*[\"'][wax]", source), f"{name} opens a file for writing"
