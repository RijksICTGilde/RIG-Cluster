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

import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from opi.utils.sops import generate_sops_key_pair

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from secret_scan import (  # noqa: E402
    Finding,
    age_key_is_real,
    looks_like_a_jwt,
    report,
    scan_files,
    scan_text,
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
# what is scanned, and what is left alone
# ---------------------------------------------------------------------------


@needs_age
def test_a_binary_suffix_is_skipped(tmp_path: Path) -> None:
    private_key, _public_key = generate_sops_key_pair()
    (tmp_path / "logo.png").write_text(private_key)
    (tmp_path / "config.yaml").write_text(private_key)

    findings = scan_files([tmp_path / "logo.png", tmp_path / "config.yaml"])

    assert [Path(finding.path).name for finding in findings] == ["config.yaml"]


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
    assert scan_files(paths) == []


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
    # On every push to main and on every pull request.
    triggers = workflow[True] if True in workflow else workflow["on"]
    assert "pull_request" in triggers
    assert "main" in triggers["push"]["branches"]


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
    findings = scan_files(tracked_files(_REPO_ROOT))

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
