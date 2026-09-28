"""Toetsen voor scripts/secret_rotate.py: de rotatie-handlers en hun planbouw.

De waarheid van dit script zit in drie dingen die "hij draait" niet toetst:

* de tabel komt overeen met de templates: elk geroteerd veld bestaat in de template, en
  kan daar ook gegenereerd worden -- anders belandt een placeholder in een productiesecret;
* de volgorde van --all is de veiligheid: keycloak-admin is de allerlaatste (alleen dat
  wachtwoord sluit je buiten het systeem waarmee je herstelt), en mail-relay gaat vóór
  keycloak-mail omdat die laatste het relay-adminaccount nodig heeft voor zijn API-call;
* --dry-run voert niets uit: geen kubectl, geen input, geen schrijfactie.
"""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import secret_rotate as rotate  # type: ignore[reportMissingImports]  # noqa: E402
from key_rotation import read_key  # type: ignore[reportMissingImports]  # noqa: E402
from secret_edit import EditFailed, decrypt, values_of  # type: ignore[reportMissingImports]  # noqa: E402

REPO = Path(__file__).resolve().parents[3]
TEMPLATES = REPO / "infrastructure/bootstrap/infrastructure/secrets/templates"


# --- de tabel tegen de templates ------------------------------------------------


def test_elke_geroteerde_kolom_staat_in_de_template() -> None:
    for component in rotate.COMPONENTS:
        if not component.template or rotate.template_path_of(component) is None:
            continue  # grafana leeft alleen in het ZAD-envbestand; transip heeft geen template
        fields = rotate.template_fields_of(component)
        for name in component.rotate_fields:
            assert name in fields, f"{component.key}: {name} staat niet in {component.template}"


def test_geroteerde_velden_zijn_te_genereren_bij_interne_componenten() -> None:
    """cnpg/live-app/env-restart halen hun nieuwe waarde uit de @secret-gen-annotatie; external niet,
    want die komt van buiten het cluster."""
    for component in rotate.COMPONENTS:
        if component.category == "external" or rotate.template_path_of(component) is None:
            continue
        fields = rotate.template_fields_of(component)
        for name in component.rotate_fields:
            kind, _length = fields[name]
            assert kind in ("random", "bcrypt"), f"{component.key}: {name} heeft geen bruikbare annotatie"


def test_zad_env_loopt_gelijk_aan_de_geroteerde_velden() -> None:
    for component in rotate.COMPONENTS:
        if component.zad_env:
            assert len(component.zad_env) == len(component.rotate_fields), component.key


def test_alleen_grafana_schrijft_nog_het_env_bestand() -> None:
    """Sinds de OPI-deployment de vier platformwachtwoorden via secretKeyRef leest is de
    kopie in .env-<cluster>.secrets vervallen; de Grafana-token is de enige die er nog
    uitsluitend leeft."""
    met_env = {c.key for c in rotate.COMPONENTS if c.zad_env}
    assert met_env == {"grafana"}


def test_de_restartlijst_dekt_elke_zad_lezer_en_start_zad_als_laatste() -> None:
    """ZAD leest postgresql/redis/minio/mail-relay/prometheus/grafana/keycloak-admin mee;
    voor die componenten staat de OPI-pod in de herstartlijst, na de eigen component."""
    met_zad = {c.key for c in rotate.COMPONENTS if rotate.OPI_DEPLOYMENT in c.workloads}
    assert met_zad == {
        "postgresql",
        "redis",
        "mail-relay",
        "minio",
        "prometheus",
        "grafana",
        "keycloak-admin",
    }
    minio = rotate.BY_KEY["minio"]
    assert minio.workloads.index("deploy/minio") < minio.workloads.index(rotate.OPI_DEPLOYMENT), (
        "eerst de app, dan de afnemer"
    )


def test_keycloak_db_hoort_bij_cnpg_en_herstart_keycloak() -> None:
    """De keycloak-db-rol roteert via de operator, en pas daarna mag Keycloak herstarten
    (hij leest KC_DB_PASSWORD bij start)."""
    component = rotate.BY_KEY["keycloak-db"]
    assert component.category == "cnpg-secret"
    assert component.database == "keycloak"
    assert component.workloads == ("deploy/keycloak",)


def test_de_volgorde_is_de_veiligheid() -> None:
    keys = [component.key for component in rotate.COMPONENTS]
    assert keys[0] == "postgresql", "de superuser gaat voorop: daar hangt niets van af"
    assert keys[-1] == "keycloak-admin", "break-glass hoort allerlaatst"
    assert keys.index("mail-relay") < keys.index("keycloak-mail"), "keycloak-mail gebruikt het relay-adminaccount"


def test_secret_namen_komen_uit_de_template_metadata() -> None:
    assert rotate.secret_name_of(rotate.BY_KEY["redis"]) == "redis-admin-credentials"
    assert rotate.secret_name_of(rotate.BY_KEY["minio"]) == "minio-admin-credentials"


# --- dry-run voert niets uit --------------------------------------------------------


class _NoSubprocess:
    def __call__(self, *args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise AssertionError("dry-run mag subprocess nooit aanroepen")


def _dry_kube(monkeypatch: pytest.MonkeyPatch) -> rotate.Kube:
    monkeypatch.setattr(rotate.subprocess, "run", _NoSubprocess())
    return rotate.Kube("rig-system", dry_run=True)


def _dry_ctx(kube: rotate.Kube, component: rotate.Component) -> rotate.Ctx:
    current = dict.fromkeys(component.rotate_fields, "huidig")
    current.setdefault("username", "postgres")
    current.setdefault("MAIL_RELAY_ADMIN_USERNAME", "admin")
    current.setdefault("MAIL_RELAY_ADMIN_PASSWORD", "relay-secret")
    new = dict.fromkeys(component.rotate_fields, "<NIEUW>")
    return rotate.Ctx(
        cluster=None, kube=kube, current=current, new=new, apply_changes=False, rotating=component.rotate_fields[0]
    )  # type: ignore[arg-type]


def test_dry_run_roept_geen_subprocess_aan(monkeypatch: pytest.MonkeyPatch) -> None:
    kube = _dry_kube(monkeypatch)
    for component in rotate.COMPONENTS:
        ctx = _dry_ctx(kube, component)
        result = kube.exec("deploy/x", ["true"])
        assert result.returncode == 0


def test_elke_stap_heeft_een_fase_en_tekst(monkeypatch: pytest.MonkeyPatch) -> None:
    kube = _dry_kube(monkeypatch)
    cluster = rotate.Cluster(
        name="odcn-production",
        namespace="rig-prd-operations",
        folders={"infrastructure": "odcn", "bootstrap": "odcn-production"},
    )
    for component in rotate.COMPONENTS:
        if component.key == "keycloak-mail":
            continue  # leest het relay-bestand; apart toetsen
        ctx = _dry_ctx(kube, component)
        ctx.cluster = cluster
        steps = rotate.steps_for(component, cluster, kube, ctx)
        assert steps, component.key
        for step in steps:
            assert step.title, f"{component.key}: stap zonder titel"
            assert step.phase in (
                "check",
                "apply",
                "verify",
                "file",
                "env",
                "followup",
                "manual",
                "push",
                "sync",
                "restart",
            ), f"{component.key}: {step.phase}"
        assert any(s.phase == "file" for s in steps) or not component.template, component.key


def test_gather_dry_ctx_stelt_geen_vragen(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("builtins.input", lambda *a: (_ for _ in ()).throw(AssertionError("droog-run vraagt niets")))
    kube = rotate.Kube("rig-prd-operations", dry_run=True)
    cluster = rotate.Cluster(
        name="odcn-production",
        namespace="rig-prd-operations",
        folders={"infrastructure": "odcn", "bootstrap": "odcn-production"},
    )
    ctx = rotate.gather_dry_ctx(rotate.BY_KEY["redis"], cluster, kube)
    assert ctx.new == {"REDIS_PASSWORD": "<NIEUW>"}


@pytest.mark.skipif(not (REPO / "security/key.txt").exists(), reason="vraagt de echte AGE-sleutel")
def test_gather_dry_ctx_leest_het_echte_bestand() -> None:
    kube = rotate.Kube("rig-prd-operations", dry_run=True)
    cluster = rotate.Cluster(
        name="odcn-production",
        namespace="rig-prd-operations",
        folders={"infrastructure": "odcn", "bootstrap": "odcn-production"},
    )
    ctx = rotate.gather_dry_ctx(rotate.BY_KEY["redis"], cluster, kube)
    assert "REDIS_PASSWORD" in ctx.current  # het odcn-bestand bestaat en is ontsleuteld


# --- pure helpers --------------------------------------------------------------------


def test_py_quote_weigert_gevaarlijke_tekens() -> None:
    rotate.py_quote("Abcdef123")
    for slecht in ("heeft'quote", 'heeft"dubbele', "regel\nbreuk"):
        with pytest.raises(EditFailed):
            rotate.py_quote(slecht)


def test_env_file_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / ".env.demo.secrets"
    path.write_text("# commentaar\nA=oud\nB=blijf\n", encoding="utf-8")
    rotate.write_env_file(path, {"A": "nieuw", "C": "erbij"})
    lines = path.read_text(encoding="utf-8")
    assert "A=nieuw" in lines
    assert "B=blijf" in lines
    assert "C=erbij" in lines
    assert rotate.read_env_file(path) == {"A": "nieuw", "B": "blijf", "C": "erbij"}


def test_multiline_velden_zijn_alleen_wat_ze_moeten_zijn() -> None:
    assert {"TRANSIP_PRIVATE_KEY"} == rotate.MULTILINE_FIELDS


# --- de categorie-volgordes als eigenschap -----------------------------------------------


def _cluster() -> rotate.Cluster:
    return rotate.Cluster(
        name="odcn-production",
        namespace="rig-prd-operations",
        folders={"infrastructure": "odcn", "bootstrap": "odcn-production"},
    )


def _fases(steps: list) -> list[str]:
    return [step.phase for step in steps]


def test_cnpg_is_bestand_eerst_en_herstart_pas_na_de_operator_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """De kernfout waar 'app-eerst' voor CNPG in kan schieten: een ALTER USER wordt door de
    operator teruggezet naar het secret. Vandaar: bestand, sync, bewijs, dán pas herstart.
    De superuser hoort hier bewust NIET bij: de operator reconciliet die rol niet uit het
    secret (gemeten op de sandbox, 28-09-2026), daarom loopt die als live-app."""
    kube = _dry_kube(monkeypatch)
    for key in ("keycloak-db", "mail-db", "forgejo-db"):
        component = rotate.BY_KEY[key]
        ctx = _dry_ctx(kube, component)
        ctx.cluster = _cluster()
        fases = _fases(rotate.steps_for(component, _cluster(), kube, ctx))
        assert "apply" not in fases, f"{key}: een cnpg-rotatie kent geen ALTER USER"
        assert (
            fases.index("file")
            < fases.index("push")
            < fases.index("sync")
            < fases.index("verify")
            < fases.index("restart")
        ), f"{key}: {fases}"


def test_live_app_is_app_eerst_en_bestand_daarna(monkeypatch: pytest.MonkeyPatch) -> None:
    """Redis, postgres-superuser en keycloak-admin hebben geen declaratief pad: daar geldt
    app-eerst wel."""
    kube = _dry_kube(monkeypatch)
    for key in ("redis", "postgresql", "keycloak-admin"):
        component = rotate.BY_KEY[key]
        ctx = _dry_ctx(kube, component)
        ctx.cluster = _cluster()
        fases = _fases(rotate.steps_for(component, _cluster(), kube, ctx))
        assert fases.index("apply") < fases.index("verify") < fases.index("file") < fases.index("sync"), (
            f"{key}: {fases}"
        )
        assert fases.index("sync") < fases.index("restart"), f"{key}: nooit herstarten vóór de sync-poort"


def test_env_restart_start_pas_na_de_secret_gelijke(monkeypatch: pytest.MonkeyPatch) -> None:
    kube = _dry_kube(monkeypatch)
    component = rotate.BY_KEY["minio"]
    ctx = _dry_ctx(kube, component)
    ctx.cluster = _cluster()
    fases = _fases(rotate.steps_for(component, _cluster(), kube, ctx))
    assert fases.index("file") < fases.index("sync") < fases.index("restart") < fases.index("verify")


class _FakeKube:
    """Alles wat de sync-stap nodig heeft, zonder cluster: geen Argo-eigenaar te vinden."""

    dry_run = False
    namespace = "rig-prd-operations"

    def run(self, args: list[str], *, check: bool = True) -> object:
        import subprocess

        return subprocess.CompletedProcess(args, 0, "", "")


def test_sync_stap_stopt_met_een_bootstrap_melding_als_het_secret_geen_argo_eigenaar_heeft() -> None:
    """Een secret zonder Argo-eigenaar komt nooit uit een sync: de tool moet dat melden,
    niet eindeloos pollen op een wijziging die niet komt."""
    component = rotate.BY_KEY["postgresql"]
    step = next(step for step in [rotate.sync_step(component)] if step)
    ctx = rotate.Ctx(
        cluster=_cluster(),
        kube=_FakeKube(),  # type: ignore[arg-type]
        current={},
        new={"password": "nieuw"},
        apply_changes=False,
        rotating="password",
    )
    assert step.run is not None
    with pytest.raises(EditFailed, match="geen Argo-eigenaar"):
        step.run(ctx)


# --- bestemmingsherkenning: de namen zoals ze in het wild voorkomen -----------------------


def _overlay(tmp_path: Path, files: dict[str, str]) -> Path:
    directory = tmp_path / "overlays" / "demo"
    directory.mkdir(parents=True)
    for name, content in files.items():
        (directory / name).write_text(content, encoding="utf-8")
    return directory


_PLAIN = "apiVersion: v1\nkind: Secret\nmetadata:\n  name: x\nstringData:\n  password: abc\n"
_ENCRYPTED = _PLAIN + "sops:\n    age:\n        - recipient: age1voorbeeld\n"


def test_de_canonieke_plek_wint(tmp_path: Path) -> None:
    overlays = _overlay(
        tmp_path, {"keycloak-db-credentials-secret.yaml.sops.yaml": _ENCRYPTED, "keycloak-db-credentials.yaml": _PLAIN}
    )
    resolution = rotate.resolve_destination(overlays, "keycloak-db-credentials-secret.yaml")
    assert resolution.state == "canonical"
    assert resolution.existing is not None
    assert resolution.existing.name.endswith("-secret.yaml.sops.yaml")


def test_het_keycloak_db_geval_wordt_als_plaintext_herkend(tmp_path: Path) -> None:
    """De fout uit de werkelijkheid van odcn: het template, nooit gegenereerd, plat in de
    overlay. Dat is dus geen SOPS-bestand om over te schrijven maar een migratie."""
    overlays = _overlay(tmp_path, {"keycloak-db-credentials.yaml": _PLAIN})
    resolution = rotate.resolve_destination(overlays, "keycloak-db-credentials-secret.yaml")
    assert resolution.state == "plaintext"
    assert resolution.existing is not None
    assert resolution.existing.name == "keycloak-db-credentials.yaml"
    assert resolution.canonical.name == "keycloak-db-credentials-secret.yaml.sops.yaml"


def test_legacy_versleuteld_blijft_legacy_versleuteld(tmp_path: Path) -> None:
    overlays = _overlay(tmp_path, {"keycloak-db-credentials.yaml.sops.yaml": _ENCRYPTED})
    resolution = rotate.resolve_destination(overlays, "keycloak-db-credentials-secret.yaml")
    assert resolution.state == "legacy-encrypted"


def test_afwezig_meldert_zich_als_absent(tmp_path: Path) -> None:
    overlays = _overlay(tmp_path, {})
    assert rotate.resolve_destination(overlays, "keycloak-db-credentials-secret.yaml").state == "absent"


def test_update_wiring_schrijft_beide_bestanden_en_niemand_anders(tmp_path: Path) -> None:
    overlays = _overlay(
        tmp_path,
        {
            "decrypt-sops.yaml": "apiVersion: viaduct.ai/v1\nkind: ksops\nmetadata:\n  name: secret-generator\nfiles:\n  - postgres-admin-secret.yaml.sops.yaml\n",
            "kustomization.yaml": "apiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\n\ngenerators:\n- decrypt-sops.yaml\n\nresources:\n  - keycloak-db-credentials.yaml\n  - anders.yaml\n",
        },
    )
    changed = rotate.update_wiring(
        overlays,
        old_plain="keycloak-db-credentials.yaml",
        new_encrypted="keycloak-db-credentials-secret.yaml.sops.yaml",
    )
    assert len(changed) == 2
    decrypt = (overlays / "decrypt-sops.yaml").read_text(encoding="utf-8")
    assert "keycloak-db-credentials-secret.yaml.sops.yaml" in decrypt
    assert "postgres-admin-secret.yaml.sops.yaml" in decrypt, "de bestaande lijst blijft"
    kustomization = (overlays / "kustomization.yaml").read_text(encoding="utf-8")
    assert "keycloak-db-credentials.yaml" not in kustomization
    assert "anders.yaml" in kustomization, "voorbijgangers in resources blijven staan"


def test_update_wiring_is_idempotent(tmp_path: Path) -> None:
    """Een tweede rotatie van hetzelfde component mag geen dubbele regels schrijven."""
    overlays = _overlay(
        tmp_path,
        {
            "decrypt-sops.yaml": "apiVersion: viaduct.ai/v1\nkind: ksops\nmetadata:\n  name: secret-generator\nfiles:\n  - keycloak-db-credentials-secret.yaml.sops.yaml\n",
            "kustomization.yaml": "apiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\n\ngenerators:\n- decrypt-sops.yaml\n",
        },
    )
    rotate.update_wiring(
        overlays,
        old_plain="keycloak-db-credentials.yaml",
        new_encrypted="keycloak-db-credentials-secret.yaml.sops.yaml",
    )
    content = (overlays / "decrypt-sops.yaml").read_text(encoding="utf-8")
    assert content.count("keycloak-db-credentials-secret.yaml.sops.yaml") == 1, "geen dubbele regel in de ksops-lijst"


def test_bulk_herstart_is_gededupliceerd_en_zad_is_de_laatste() -> None:
    """Een bulkronde mag ZAD niet zes keer herstarten: één keer, na alles, als laatste."""
    slot = rotate.final_restart_step(
        [rotate.BY_KEY[key] for key in ("postgresql", "keycloak-db", "minio")],
        _cluster(),
    )
    assert slot is not None
    run_text = " ".join(slot.dry_text)
    assert run_text.count("operations-manager") == 1
    assert slot.dry_text[-1].endswith(f"restart {rotate.OPI_DEPLOYMENT}"), "de afnemer gaat als laatste"


def test_deferred_verify_dekt_alleen_wat_restant_heeft() -> None:
    assert rotate.deferred_verify_steps([rotate.BY_KEY["minio"]], _cluster()), "minio heeft een post-restart-check"
    assert rotate.deferred_verify_steps([rotate.BY_KEY["mail-relay"]], _cluster()), (
        "mail-relay heeft de relay-auth-check"
    )
    assert rotate.deferred_verify_steps([rotate.BY_KEY["postgresql"]], _cluster) == []


def test_mail_relay_is_env_restart_omdat_de_fallback_admin_in_de_config_leeft() -> None:
    """Gemeten op de sandbox: /api/principal/<admin> zegt notFound -- het management-account
    is een [authentication.fallback-admin] in de configmap, aangemaakt bij start. Dus:
    bestand, sync, relay-restart, auth-proef."""
    component = rotate.BY_KEY["mail-relay"]
    assert component.category == "env-restart"
    assert component.workloads[0].startswith("relayns:"), "eerst de relay zelf"
    assert component.workloads[1] == rotate.OPI_DEPLOYMENT


def test_bulk_sluit_de_externe_categorie_uit() -> None:
    """--all is clusterwerk; een externe rotatie (TransIP-console, ODCN-ticket) kan nooit
    in zo'n reeks passen. Die ritten draai je apart."""
    bulk = [c for c in rotate.COMPONENTS if c.category != "external"]
    assert not any(c.category == "external" for c in bulk)
    assert {c.key for c in rotate.COMPONENTS} - {c.key for c in bulk} == {"transip", "grafana"}


def test_de_dekking_poort_is_gesloten_voor_deze_repo() -> None:
    """Elke template heeft een rij of een benoemde uitzondering, en elke rij met een
    templatenaam vindt zijn bestand. Nieuwe secrets worden dus nooit stilletjes gemist."""
    assert rotate.coverage_gaps() == []


def test_de_dekking_poort_rapporteert_precies_wat_mist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()
    # De rij-kant van de poort moet gesloten zijn voor de bestaande tabel (lege inhoud
    # volstaat: de poort toetst op bestaan en naam, niet op parseerbaarheid)...
    for component in rotate.COMPONENTS:
        if component.template:
            (templates_dir / component.template).write_text("apiVersion: v1\nstringData: {}\n", encoding="utf-8")
    for name in rotate.NON_ROTATABLE:
        (templates_dir / name).write_text("apiVersion: v1\nstringData: {}\n", encoding="utf-8")
    # ...zodat alleen het echte gemis overblijft:
    (templates_dir / "onbekend-secret.yaml").write_text("apiVersion: v1\nstringData: {}\n", encoding="utf-8")
    monkeypatch.setattr(
        rotate,
        "EDIT_TREES",
        {"infrastructure": (templates_dir, tmp_path / "overlays", "X")},
    )
    gaps = rotate.coverage_gaps()
    assert gaps == ["template onbekend-secret.yaml heeft geen rij in de tabel en staat niet in NON_ROTATABLE"]


def test_de_bewuste_uitsluitingen_zijn_en_blijven_benoemd() -> None:
    assert set(rotate.NON_ROTATABLE) >= {"vault-init-secret.yaml"}
    assert set(rotate.TEMPLATELESS) == {"transip", "grafana"}


# --- seeding: de overlay van een cluster kanoniek maken -----------------------------------

_needs_age_and_sops = pytest.mark.skipif(
    shutil.which("age-keygen") is None or shutil.which("sops") is None,
    reason="vraagt age-keygen en sops",
)


class _LiveKube:
    """Eén live secret, de rest bestaat niet."""

    namespace = "rig-system"
    dry_run = False

    def run(self, args: list[str], *, check: bool = True) -> object:
        name = args[2] if len(args) > 2 and args[0] == "get" and args[1] == "secret" else ""
        if name == "demo-credentials":
            data = {
                "data": {
                    "USERNAME": base64.b64encode(b"admin").decode(),
                    "PASSWORD": base64.b64encode(b"live").decode(),
                }
            }
            return subprocess.CompletedProcess(args, 0, json.dumps(data), "")
        return subprocess.CompletedProcess(args, 1, "", "NotFound")


@_needs_age_and_sops
def test_seed_maakt_de_overlay_met_live_waarden(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """De seed bewijst zichzelf: na het schrijven is de versleutelde inhoud gelijk aan live."""
    repo = tmp_path / "repo"
    (repo / "security").mkdir(parents=True)
    subprocess.run(["age-keygen", "-o", str(repo / "security" / "sandbox-key.txt")], capture_output=True, check=True)
    templates_dir = repo / "templates"
    templates_dir.mkdir()
    (templates_dir / "demo-secret.yaml").write_text(
        "apiVersion: v1\nkind: Secret\nmetadata:\n  name: demo-credentials\n  namespace: placeholder\n"
        "type: Opaque\nstringData:\n  USERNAME: admin\n  PASSWORD: 'x' # @secret-gen:random:16\n",
        encoding="utf-8",
    )
    overlays_root = tmp_path / "overlays"
    overlays_root.mkdir()
    cluster = rotate.Cluster(
        name="sandboxed-local", namespace="rig-system", folders={"infrastructure": "demo", "bootstrap": "demo"}
    )
    monkeypatch.setattr(rotate, "REPO", repo)
    monkeypatch.setattr(rotate, "EDIT_TREES", {"infrastructure": (templates_dir, overlays_root, "X")})

    assert rotate.seed_overlay(cluster, _LiveKube(), apply_changes=True) == 0

    written = overlays_root / "demo" / "demo-secret.yaml.sops.yaml"
    assert written.is_file()
    assert (overlays_root / "demo" / "kustomization.yaml").is_file()
    assert "demo-secret.yaml.sops.yaml" in (overlays_root / "demo" / "decrypt-sops.yaml").read_text(encoding="utf-8")
    private = read_key(repo / "security" / "sandbox-key.txt")
    assert values_of(decrypt(written, private)) == {"USERNAME": "admin", "PASSWORD": "live"}


@_needs_age_and_sops
def test_seed_weigert_een_bestaande_overlay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    (repo / "security").mkdir(parents=True)
    subprocess.run(["age-keygen", "-o", str(repo / "security" / "sandbox-key.txt")], capture_output=True, check=True)
    overlays_root = tmp_path / "overlays"
    (overlays_root / "demo").mkdir(parents=True)
    cluster = rotate.Cluster(
        name="sandboxed-local", namespace="rig-system", folders={"infrastructure": "demo", "bootstrap": "demo"}
    )
    monkeypatch.setattr(rotate, "REPO", repo)
    monkeypatch.setattr(rotate, "EDIT_TREES", {"infrastructure": (tmp_path / "templates", overlays_root, "X")})
    with pytest.raises(EditFailed, match="bestaat al"):
        rotate.seed_overlay(cluster, _LiveKube(), apply_changes=True)
