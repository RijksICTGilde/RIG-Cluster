"""De Taskfile-taken die ArgoCD een OPI-sleutel laten vertrouwen.

- ``argocd:trust-public-key``: zet een sleutel in ``argocd-gpg-keys-cm`` en voegt zijn key ID
  toe aan ``signatureKeys`` op het AppProject, zonder bestaande sleutels weg te gooien.
- ``argocd:trust-git-signing-key``: haalt de privesleutel uit het cluster-secret en
  exporteert het publieke deel.

Deze tests draaien de shell, zodat ze de dedup en guards in de taak-logica vangen.
"""

from __future__ import annotations

import base64
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
import yaml
from tests.programma import echt_programma

REPO_ROOT = Path(__file__).resolve().parents[3]
TASKFILE = REPO_ROOT / "Taskfile.yaml"
NAMESPACE = "rig-system"


def _taakshell(taak: str) -> str:
    """De eerste (bash-)stap van een taak, met zijn template-variabelen ingevuld."""
    taskfile = yaml.safe_load(TASKFILE.read_text())
    cmd = taskfile["tasks"][taak]["cmds"][0]
    return cmd.replace("{{.RIG_NAMESPACE}}", NAMESPACE)


def _statusshell(taak: str) -> str:
    """Het ``status``-script van een taak: slaagt het, dan slaat Task de taak over."""
    taskfile = yaml.safe_load(TASKFILE.read_text())
    return taskfile["tasks"][taak]["status"][0].replace("{{.RIG_NAMESPACE}}", NAMESPACE)


@pytest.fixture(autouse=True)
def _gereedschap() -> None:
    if shutil.which("bash") is None:
        pytest.skip("bash ontbreekt")
    if shutil.which("gpg") is None:
        pytest.skip("gpg ontbreekt")


def _generate_key(tmp_path: Path, naam: str) -> tuple[str, str, str]:
    """Een sleutel zonder passphrase. Geeft (fingerprint, armored public, armored secret)."""
    home = tmp_path / naam
    home.mkdir(mode=0o700)
    env = {"GNUPGHOME": str(home)}
    subprocess.run(
        [
            "gpg",
            "--batch",
            "--quiet",
            "--passphrase",
            "",
            "--quick-generate-key",
            f"{naam} <{naam}@example.com>",
            "ed25519",
            "sign",
            "never",
        ],
        check=True,
        capture_output=True,
        env=env,
    )
    fpr_proc = subprocess.run(
        ["gpg", "--batch", "--list-keys", "--with-colons"], check=True, capture_output=True, text=True, env=env
    )
    fingerprint = next(line.split(":")[9] for line in fpr_proc.stdout.splitlines() if line.startswith("fpr:"))
    public = subprocess.run(
        ["gpg", "--batch", "--armor", "--export", fingerprint], check=True, capture_output=True, text=True, env=env
    ).stdout
    secret = subprocess.run(
        ["gpg", "--batch", "--armor", "--export-secret-keys", fingerprint],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    ).stdout
    return fingerprint, public, secret


def _kubectl_stub(
    bin_dir: Path,
    log: Path,
    *,
    appproject_exists: bool = True,
    configmap_exists: bool = True,
    current_ids: str = "",
    current_read_fails: bool = False,
    secret_value: str = "",
) -> None:
    """Een kubectl die zijn aanroepen opschrijft.

    Een leeg ``secret_value`` staat voor een ontbrekend secret, en dan gedraagt de stub
    zich als echte kubectl: exit 1, tenzij de aanroep ``--ignore-not-found`` meegeeft.
    """
    secret_ontbreekt = not secret_value
    (bin_dir / "kubectl").write_text(
        "#!/bin/bash\n"
        'printf "%s\\n" "$*" >> "$KUBECTL_LOG"\n'
        'case "$*" in\n'
        f'  "get appproject zad-app-of-apps -n {NAMESPACE}")\n'
        f"    exit {0 if appproject_exists else 1};;\n"
        f'  "get configmap argocd-gpg-keys-cm -n {NAMESPACE}")\n'
        f"    exit {0 if configmap_exists else 1};;\n"
        f'  "get appproject zad-app-of-apps -n {NAMESPACE} -o jsonpath={{.spec.signatureKeys[*].keyID}}")\n'
        f'    printf "%s" "{current_ids}"; exit {1 if current_read_fails else 0};;\n'
        f'  "get secret operations-manager-git-signing-key -n {NAMESPACE}'
        f' --ignore-not-found -o jsonpath={{.data.signing-key\\.asc}}")\n'
        f'    printf "%s" "{secret_value}"; exit 0;;\n'
        f'  "get secret operations-manager-git-signing-key -n {NAMESPACE}'
        f' -o jsonpath={{.data.signing-key\\.asc}}")\n'
        f'    printf "%s" "{secret_value}"; exit {1 if secret_ontbreekt else 0};;\n'
        "  *)\n"
        "    exit 0;;\n"
        "esac\n"
    )
    (bin_dir / "kubectl").chmod(0o755)
    log.touch()


def _draai(shell: str, tmp_path: Path, bin_dir: Path, log: Path) -> subprocess.CompletedProcess[str]:
    """De taakshell met errexit, want Task's interpreter draait hem zo.

    Zonder ``-e`` liep een mislukt commando stil door en bleef een pad groen dat onder
    ``task`` meteen afbrak.
    """
    bash = echt_programma("bash")
    gpg = echt_programma("gpg")
    return subprocess.run(
        [bash, "-ec", shell],
        cwd=tmp_path,
        env={
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "HOME": str(tmp_path),
            "KUBECTL_LOG": str(log),
            "TMPDIR": tempfile.gettempdir(),
        },
        capture_output=True,
        text=True,
        timeout=60,
    )


def _keyid_entries(regel: str) -> str:
    """Het JSON-fragment achter ``-p`` in een gelogde patch-aanroep."""
    return regel.split("-p ", 1)[1]


class TestTrustPublicKey:
    def test_dry_run_raakt_kubectl_niet_aan(self, tmp_path: Path) -> None:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        log = tmp_path / "kubectl.log"
        _kubectl_stub(bin_dir, log)
        key_file = tmp_path / "key.asc"
        key_file.write_text("onbelangrijk in een dry-run\n")

        shell = (
            _taakshell("argocd:trust-public-key")
            .replace("{{.KEY_FILE}}", str(key_file))
            .replace("{{.DRY_RUN}}", "true")
        )
        proc = _draai(shell, tmp_path, bin_dir, log)

        assert proc.returncode == 0, proc.stderr
        assert log.read_text() == ""

    def test_zonder_appproject_stopt_met_foutcode_en_raakt_niets_aan(self, tmp_path: Path) -> None:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        log = tmp_path / "kubectl.log"
        _kubectl_stub(bin_dir, log, appproject_exists=False)
        key_file = tmp_path / "key.asc"
        key_file.write_text("onbelangrijk, want de taak stopt voor het importeren\n")

        shell = (
            _taakshell("argocd:trust-public-key")
            .replace("{{.KEY_FILE}}", str(key_file))
            .replace("{{.DRY_RUN}}", "false")
        )
        proc = _draai(shell, tmp_path, bin_dir, log)

        assert proc.returncode == 1
        regels = log.read_text().splitlines()
        assert regels == [f"get appproject zad-app-of-apps -n {NAMESPACE}"]

    def test_ontbrekende_configmap_wordt_aangemaakt_en_krijgt_de_sleutel(self, tmp_path: Path) -> None:
        fingerprint, public, _secret = _generate_key(tmp_path, "sleutel-een")
        key_id = fingerprint[-16:]
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        log = tmp_path / "kubectl.log"
        _kubectl_stub(bin_dir, log, configmap_exists=False, current_ids="")
        key_file = tmp_path / "key.asc"
        key_file.write_text(public)

        shell = (
            _taakshell("argocd:trust-public-key")
            .replace("{{.KEY_FILE}}", str(key_file))
            .replace("{{.DRY_RUN}}", "false")
        )
        proc = _draai(shell, tmp_path, bin_dir, log)

        assert proc.returncode == 0, proc.stderr
        regels = log.read_text().splitlines()
        assert any(r == f"create configmap argocd-gpg-keys-cm -n {NAMESPACE}" for r in regels), regels

        patch_cm = next(r for r in regels if r.startswith(f"patch configmap argocd-gpg-keys-cm -n {NAMESPACE}"))
        assert f'"{key_id}"' in patch_cm

        patch_ap = next(r for r in regels if r.startswith(f"patch appproject zad-app-of-apps -n {NAMESPACE}"))
        assert _keyid_entries(patch_ap) == f'{{"spec":{{"signatureKeys":[{{"keyID":"{key_id}"}}]}}}}'

    def test_bestaande_configmap_wordt_niet_opnieuw_aangemaakt(self, tmp_path: Path) -> None:
        fingerprint, public, _secret = _generate_key(tmp_path, "sleutel-twee")
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        log = tmp_path / "kubectl.log"
        _kubectl_stub(bin_dir, log, configmap_exists=True, current_ids="")
        key_file = tmp_path / "key.asc"
        key_file.write_text(public)

        shell = (
            _taakshell("argocd:trust-public-key")
            .replace("{{.KEY_FILE}}", str(key_file))
            .replace("{{.DRY_RUN}}", "false")
        )
        proc = _draai(shell, tmp_path, bin_dir, log)

        assert proc.returncode == 0, proc.stderr
        regels = log.read_text().splitlines()
        assert not [r for r in regels if r.startswith("create configmap")], regels

    def test_een_al_vertrouwde_sleutel_komt_niet_dubbel_in_signaturekeys(self, tmp_path: Path) -> None:
        """Opnieuw draaien met dezelfde sleutel is idempotent: geen tweede keyID-vermelding."""
        fingerprint, public, _secret = _generate_key(tmp_path, "sleutel-drie")
        key_id = fingerprint[-16:]
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        log = tmp_path / "kubectl.log"
        _kubectl_stub(bin_dir, log, current_ids=key_id)
        key_file = tmp_path / "key.asc"
        key_file.write_text(public)

        shell = (
            _taakshell("argocd:trust-public-key")
            .replace("{{.KEY_FILE}}", str(key_file))
            .replace("{{.DRY_RUN}}", "false")
        )
        proc = _draai(shell, tmp_path, bin_dir, log)

        assert proc.returncode == 0, proc.stderr
        patch_ap = next(
            r for r in log.read_text().splitlines() if r.startswith(f"patch appproject zad-app-of-apps -n {NAMESPACE}")
        )
        assert _keyid_entries(patch_ap) == f'{{"spec":{{"signatureKeys":[{{"keyID":"{key_id}"}}]}}}}'

    def test_onbruikbaar_key_file_patcht_niets_en_stopt_met_foutcode(self, tmp_path: Path) -> None:
        """Zonder guard zou de lege fingerprint signatureKeys naar [] patchen, met exitcode 0."""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        log = tmp_path / "kubectl.log"
        _kubectl_stub(bin_dir, log, current_ids="AAAAAAAAAAAAAAAA")
        key_file = tmp_path / "key.asc"
        key_file.write_text("dit is geen GPG-sleutel\n")

        shell = (
            _taakshell("argocd:trust-public-key")
            .replace("{{.KEY_FILE}}", str(key_file))
            .replace("{{.DRY_RUN}}", "false")
        )
        proc = _draai(shell, tmp_path, bin_dir, log)

        assert proc.returncode == 1
        assert "niets te vertrouwen" in proc.stdout
        regels = log.read_text().splitlines()
        assert regels == [f"get appproject zad-app-of-apps -n {NAMESPACE}"], regels

    def test_mislukte_lezing_van_signaturekeys_patcht_het_appproject_niet(self, tmp_path: Path) -> None:
        """Een lege CURRENT door een fout zou de merge-patch de hele array laten vervangen."""
        _fingerprint, public, _secret = _generate_key(tmp_path, "sleutel-vijf")
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        log = tmp_path / "kubectl.log"
        _kubectl_stub(bin_dir, log, current_read_fails=True)
        key_file = tmp_path / "key.asc"
        key_file.write_text(public)

        shell = (
            _taakshell("argocd:trust-public-key")
            .replace("{{.KEY_FILE}}", str(key_file))
            .replace("{{.DRY_RUN}}", "false")
        )
        proc = _draai(shell, tmp_path, bin_dir, log)

        assert proc.returncode == 1
        assert "niets gepatcht" in proc.stdout
        regels = log.read_text().splitlines()
        assert not [r for r in regels if r.startswith("patch appproject")], regels

    def test_een_andere_bestaande_sleutel_blijft_naast_de_nieuwe_staan(self, tmp_path: Path) -> None:
        """De beheerderssleutel en de OPI-sleutel staan samen in signatureKeys."""
        fingerprint, public, _secret = _generate_key(tmp_path, "sleutel-vier")
        key_id = fingerprint[-16:]
        bestaande_id = "AAAAAAAAAAAAAAAA"
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        log = tmp_path / "kubectl.log"
        _kubectl_stub(bin_dir, log, current_ids=bestaande_id)
        key_file = tmp_path / "key.asc"
        key_file.write_text(public)

        shell = (
            _taakshell("argocd:trust-public-key")
            .replace("{{.KEY_FILE}}", str(key_file))
            .replace("{{.DRY_RUN}}", "false")
        )
        proc = _draai(shell, tmp_path, bin_dir, log)

        assert proc.returncode == 0, proc.stderr
        patch_ap = next(
            r for r in log.read_text().splitlines() if r.startswith(f"patch appproject zad-app-of-apps -n {NAMESPACE}")
        )
        verwacht = f'{{"spec":{{"signatureKeys":[{{"keyID":"{bestaande_id}"}},{{"keyID":"{key_id}"}}]}}}}'
        assert _keyid_entries(patch_ap) == verwacht


class TestGitSigningKeyPublic:
    def test_zonder_secret_stopt_met_melding_in_plaats_van_stil_op_errexit(self, tmp_path: Path) -> None:
        """Zonder --ignore-not-found breekt de assignment onder Task's errexit af voor de
        if-check, en valt de melding weg; exitcode 1 blijft dan toevallig gelijk."""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        log = tmp_path / "kubectl.log"
        _kubectl_stub(bin_dir, log, secret_value="")

        shell = _taakshell("git-signing-key:public")
        proc = _draai(shell, tmp_path, bin_dir, log)

        assert proc.returncode == 1
        assert "this cluster signs nothing" in proc.stdout

    def test_met_secret_toont_fingerprint_en_publieke_sleutel(self, tmp_path: Path) -> None:
        fingerprint, _public, secret = _generate_key(tmp_path, "sleutel-public-task")
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        log = tmp_path / "kubectl.log"
        _kubectl_stub(bin_dir, log, secret_value=base64.b64encode(secret.encode()).decode())

        shell = _taakshell("git-signing-key:public")
        proc = _draai(shell, tmp_path, bin_dir, log)

        assert proc.returncode == 0, proc.stderr
        assert f"fingerprint: {fingerprint}" in proc.stdout
        assert "BEGIN PGP PUBLIC KEY BLOCK" in proc.stdout


class TestTrustGitSigningKey:
    def test_zonder_secret_slaat_task_de_taak_over(self, tmp_path: Path) -> None:
        """Een geslaagde status is het overslaan: anders faalt de tweede stap op het sleutelbestand,
        en bootstrap-argo-system met hem."""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        log = tmp_path / "kubectl.log"
        _kubectl_stub(bin_dir, log, secret_value="")

        shell = _statusshell("argocd:trust-git-signing-key").replace("{{.DRY_RUN}}", "false")
        proc = _draai(shell, tmp_path, bin_dir, log)

        assert proc.returncode == 0, proc.stderr

    def test_met_secret_slaat_task_de_taak_niet_over(self, tmp_path: Path) -> None:
        _fingerprint, _public, secret = _generate_key(tmp_path, "sleutel-status")
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        log = tmp_path / "kubectl.log"
        _kubectl_stub(bin_dir, log, secret_value=base64.b64encode(secret.encode()).decode())

        shell = _statusshell("argocd:trust-git-signing-key").replace("{{.DRY_RUN}}", "false")
        proc = _draai(shell, tmp_path, bin_dir, log)

        assert proc.returncode == 1

    def test_dry_run_slaat_de_taak_niet_over_en_leest_het_secret_niet(self, tmp_path: Path) -> None:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        log = tmp_path / "kubectl.log"
        _kubectl_stub(bin_dir, log, secret_value="")

        shell = _statusshell("argocd:trust-git-signing-key").replace("{{.DRY_RUN}}", "true")
        proc = _draai(shell, tmp_path, bin_dir, log)

        assert proc.returncode == 1
        assert log.read_text() == ""

    def test_met_secret_exporteert_alleen_het_publieke_deel(self, tmp_path: Path) -> None:
        fingerprint, _public, secret = _generate_key(tmp_path, "opi-sleutel")
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        log = tmp_path / "kubectl.log"
        _kubectl_stub(bin_dir, log, secret_value=base64.b64encode(secret.encode()).decode())

        shell = _taakshell("argocd:trust-git-signing-key").replace("{{.DRY_RUN}}", "false")
        proc = _draai(shell, tmp_path, bin_dir, log)

        assert proc.returncode == 0, proc.stderr
        exported = (tmp_path / "security" / "opi-git-signing-public.asc").read_text()
        assert "BEGIN PGP PUBLIC KEY BLOCK" in exported
        assert "BEGIN PGP PRIVATE KEY BLOCK" not in exported

        herimport = tmp_path / "herimport"
        herimport.mkdir(mode=0o700)
        subprocess.run(
            ["gpg", "--batch", "--quiet", "--import"],
            input=exported,
            check=True,
            capture_output=True,
            text=True,
            env={"GNUPGHOME": str(herimport)},
        )
        lijst = subprocess.run(
            ["gpg", "--batch", "--list-keys", "--with-colons"],
            check=True,
            capture_output=True,
            text=True,
            env={"GNUPGHOME": str(herimport)},
        ).stdout
        assert fingerprint in lijst

    def test_secret_zonder_sleutel_exporteert_niets_en_stopt_met_foutcode(self, tmp_path: Path) -> None:
        """Zonder guard liep de taak door op een lege fingerprint naar de patch-stap."""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        log = tmp_path / "kubectl.log"
        _kubectl_stub(bin_dir, log, secret_value=base64.b64encode(b"dit is geen sleutel").decode())

        shell = _taakshell("argocd:trust-git-signing-key").replace("{{.DRY_RUN}}", "false")
        proc = _draai(shell, tmp_path, bin_dir, log)

        assert proc.returncode == 1
        assert "geen bruikbare GPG-sleutel" in proc.stdout
        assert not (tmp_path / "security" / "opi-git-signing-public.asc").exists()

    def test_dry_run_raakt_kubectl_niet_aan(self, tmp_path: Path) -> None:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        log = tmp_path / "kubectl.log"
        _kubectl_stub(bin_dir, log)

        shell = _taakshell("argocd:trust-git-signing-key").replace("{{.DRY_RUN}}", "true")
        proc = _draai(shell, tmp_path, bin_dir, log)

        assert proc.returncode == 0, proc.stderr
        assert log.read_text() == ""
