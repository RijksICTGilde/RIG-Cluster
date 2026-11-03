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
import yaml

_SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import secret_rotate as rotate  # type: ignore[reportMissingImports]  # noqa: E402
from key_rotation import read_key  # type: ignore[reportMissingImports]  # noqa: E402
from opi.core.secret_key import MIN_SECRET_KEY_LENGTH, validate_secret_key  # noqa: E402
from secret_edit import EditFailed, decrypt, generate, values_of  # type: ignore[reportMissingImports]  # noqa: E402

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


def test_welke_rijen_het_envsecret_van_zad_raken() -> None:
    """Sinds de OPI-deployment de vier platformwachtwoorden via secretKeyRef leest is de
    kopie in .env-<cluster>.secrets vervallen; wat hier staat leeft er nog uitsluitend
    (of, bij de backup-S3-sleutel, buiten GitOps)."""
    met_env = {c.key for c in rotate.COMPONENTS if c.zad_env}
    assert met_env == {
        "grafana",
        "platform-repo-pat",
        "minio-backup",
        "secret-key",
        "keycloak-master-oidc",
        "registry",
    }


def test_de_restartlijst_dekt_elke_zad_lezer_en_start_zad_als_laatste() -> None:
    """Voor elk component waarvan ZAD de waarde zelf leest staat de OPI-pod in de
    herstartlijst, na de eigen component."""
    met_zad = {c.key for c in rotate.COMPONENTS if rotate.OPI_DEPLOYMENT in c.workloads}
    assert met_zad == {
        "postgresql",
        # OPI leest het wachtwoord van zijn eigen databaserol als env (RC-410).
        "operations-manager-db",
        "redis",
        "mail-relay",
        "minio",
        "prometheus",
        "grafana",
        "keycloak-admin",
        "platform-repo-pat",
        # Niet argocd-admin: OPI leest dat wachtwoord sinds RC-345 niet meer, hij werkt met
        # het account opi en zijn API-token.
        "argocd-opi-token",
        "secret-key",
        "keycloak-master-oidc",
    }
    minio = rotate.BY_KEY["minio"]
    assert minio.workloads.index("deploy/minio") < minio.workloads.index(rotate.OPI_DEPLOYMENT), (
        "eerst de app, dan de afnemer"
    )


def test_elke_env_lezer_van_het_metrics_token_herstart_bij_de_rotatie() -> None:
    """Wie het token via env leest, draagt het tot een herstart. Staat zo'n workload niet in
    de herstartlijst, dan scrapet Prometheus na een rotatie met een token dat de app nog niet
    kent (401 op keycloak-rig-metrics, target down tot de pod toevallig herstart)."""
    workloads = set(rotate.BY_KEY["prometheus"].workloads)
    for path in _manifesten_met("prometheus-metrics-auth"):
        for doc in yaml.safe_load_all(path.read_text()):
            if not isinstance(doc, dict) or doc.get("kind") not in ("Deployment", "StatefulSet"):
                continue
            if not _leest_secret_via_env(doc, "prometheus-metrics-auth"):
                continue
            soort = "deploy" if doc["kind"] == "Deployment" else "sts"
            naam = f"{soort}/{doc['metadata']['name']}"
            assert naam in workloads, f"{path}: {naam} leest het token via env maar herstart niet"


def _manifesten_met(tekst: str) -> list[Path]:
    wortels = (REPO / "infrastructure/bootstrap", REPO / "bootstrap")
    return [p for wortel in wortels for p in wortel.rglob("*.yaml") if tekst in p.read_text()]


def _leest_secret_via_env(doc: dict, secret: str) -> bool:
    containers = doc["spec"]["template"]["spec"].get("containers", [])
    for container in containers:
        for entry in container.get("env", []):
            ref = (entry.get("valueFrom") or {}).get("secretKeyRef") or {}
            if ref.get("name") == secret:
                return True
    return False


def test_keycloak_db_hoort_bij_cnpg_en_herstart_keycloak() -> None:
    """De keycloak-db-rol roteert via de operator, en pas daarna mag Keycloak herstarten
    (hij leest KC_DB_PASSWORD bij start)."""
    component = rotate.BY_KEY["keycloak-db"]
    assert component.category == "cnpg-secret"
    assert component.database == "keycloak"
    assert component.workloads == ("deploy/keycloak",)


def test_chisel_roteert_via_een_herstart_van_de_server() -> None:
    """De inlog staat in twee velden omdat de generatie per veld vult; alleen `password`
    roteert, en de server leest hem als env, dus een herstart hoort erbij."""
    component = rotate.BY_KEY["chisel"]
    assert component.category == "env-restart"
    assert component.rotate_fields == ("password",)
    assert component.workloads == ("deploy/chisel-server",)
    assert rotate.secret_name_of(component) == "chisel-auth-credentials"


def test_chisel_leest_beide_velden_uit_het_secret_dat_de_rotatie_schrijft() -> None:
    """De secretKeyRef van het Deployment moet de veldnamen van de template volgen: een
    hernoeming aan één kant laat de pod met een ontbrekende sleutel achter."""
    deployment = yaml.safe_load(
        (REPO / "infrastructure/bootstrap/infrastructure/chisel/controller/base/deployment.yaml").read_text()
    )
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    refs = {
        entry["name"]: (entry["valueFrom"]["secretKeyRef"]["name"], entry["valueFrom"]["secretKeyRef"]["key"])
        for entry in container["env"]
        if "valueFrom" in entry
    }
    assert refs == {
        "CHISEL_USERNAME": ("chisel-auth-credentials", "username"),
        "CHISEL_PASSWORD": ("chisel-auth-credentials", "password"),
    }
    assert "--auth=$(CHISEL_USERNAME):$(CHISEL_PASSWORD)" in container["args"]
    assert set(refs.values()) <= {
        ("chisel-auth-credentials", veld) for veld in rotate.template_fields_of(rotate.BY_KEY["chisel"])
    }


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
    assert {c.key for c in rotate.COMPONENTS} - {c.key for c in bulk} == {
        "transip",
        "grafana",
        "platform-repo-pat",
        "keycloak-master-oidc",
        "registry",
        "argocd-webhook-github",
    }


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
    assert set(rotate.TEMPLATELESS) == {
        "transip",
        "grafana",
        "platform-repo-pat",
        "argocd-admin",
        "secret-key",
        "keycloak-master-oidc",
        "registry",
        "argocd-opi-token",
        "argocd-webhook-github",
    }


# Componenten die niets bijzonders hebben: een template in git, een rotatie binnen dit
# cluster, en geen sleutel in het envsecret van ZAD. De test eronder weigert een nieuw
# component dat hier noch in een benoemde set staat, zodat zo'n rij niet stil binnenkomt.
GEWONE_CLUSTERCOMPONENTEN = {
    "postgresql",
    "keycloak-db",
    "mail-db",
    "forgejo-db",
    "redis",
    "mail-relay",
    "keycloak-mail",
    "keycloak-admin",
    "minio",
    "prometheus",
    "pgadmin",
    "platform-backup",
    "operations-manager-db",
    "chisel",
}


def test_elk_component_staat_in_een_benoemde_set_of_in_de_gewone_lijst() -> None:
    """Een nieuwe rij in COMPONENTS komt alleen binnen met een bewuste keuze: of ze raakt het
    envsecret van ZAD, of ze is extern, of ze heeft geen template, of ze staat hier benoemd."""
    benoemd = (
        {c.key for c in rotate.COMPONENTS if c.zad_env}
        | {c.key for c in rotate.COMPONENTS if c.category == "external"}
        | set(rotate.TEMPLATELESS)
        | GEWONE_CLUSTERCOMPONENTEN
    )
    assert {c.key for c in rotate.COMPONENTS} - benoemd == set()
    assert set(rotate.BY_KEY) >= GEWONE_CLUSTERCOMPONENTEN, "verdwenen component in de gewone lijst"


def test_geen_twee_rijen_met_dezelfde_key() -> None:
    sleutels = [c.key for c in rotate.COMPONENTS]
    assert len(sleutels) == len(set(sleutels)), "BY_KEY laat een dubbele rij stil verdwijnen"


# --- de dekking-poort op het envsecret van ZAD -------------------------------------------


def _schrijf_envsecret(overlays: Path, cluster: str, keys: tuple[str, ...]) -> Path:
    path = overlays / cluster / f"{rotate.ZAD_ENV_SECRET}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "apiVersion: v1\nkind: Secret\ndata:\n" + "".join(f"    {name}: ENC[AES256_GCM,data:x]\n" for name in keys),
        encoding="utf-8",
    )
    return path


def _envsecret(tmp_path: Path, *keys: str) -> Path:
    """Een overlaymap in een eigen git-repo, met het envsecret van odcn-production erin.

    Een echte repo en niet een losse map: de poort meet alleen wat git volgt, dus zonder
    index toetst dit de terugvalweg van `tracked_files` in plaats van de poort zelf.
    """
    overlays = tmp_path / "overlays"
    path = _schrijf_envsecret(overlays, "odcn-production", keys)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", str(path)], check=True, capture_output=True)
    return overlays


def test_de_poort_noemt_een_onbekende_sleutel_in_het_envsecret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Een sleutel zonder rotatierij en zonder benoemde reden is een gat: zo wordt een nieuw
    geheim in het envsecret niet stilletjes gemist."""
    monkeypatch.setattr(rotate, "ZAD_ENV_OVERLAYS", _envsecret(tmp_path, "SECRET_KEY", "NIEUW_GEHEIM"))
    uit_het_bestand = [gap for gap in rotate.zad_env_coverage_gaps() if "geen rotatierij" in gap]
    assert len(uit_het_bestand) == 1, uit_het_bestand
    assert "NIEUW_GEHEIM" in uit_het_bestand[0]


def test_de_poort_noemt_een_naam_die_uit_het_envsecret_verdween(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """De twee lijsten mogen niet stil verouderen: wat er niet meer in staat, hoort weg."""
    monkeypatch.setattr(rotate, "ZAD_ENV_OVERLAYS", _envsecret(tmp_path, "SECRET_KEY"))
    gaps = rotate.zad_env_coverage_gaps()
    assert any("DEBUG_MODE" in gap and "ZAD_ENV_CONFIG" in gap for gap in gaps), gaps
    assert any("LOGWATCHER_NTFY_TOPIC" in gap and "ZAD_ENV_NON_ROTATABLE" in gap for gap in gaps), gaps


def test_de_poort_leest_het_envsecret_niet_dat_de_sandbox_lokaal_genereert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`task sandbox:generate-env-secrets` schrijft hetzelfde bestand in de sandbox-overlay,
    uit een `.env` die per machine verschilt en die `.gitignore` buiten de repo houdt. Las de
    poort dat mee, dan weigert edit-secret.py te starten op elke machine met een sandbox."""
    overlays = _envsecret(tmp_path, "SECRET_KEY", *rotate.ZAD_ENV_CONFIG, *rotate.ZAD_ENV_NON_ROTATABLE)
    _schrijf_envsecret(overlays, "sandboxed-local", ("OIDC_CLIENT_SECRET",))
    monkeypatch.setattr(rotate, "ZAD_ENV_OVERLAYS", overlays)

    assert rotate.zad_env_coverage_gaps() == []


def test_de_poort_meldt_het_als_er_geen_envsecret_te_vinden_is(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Een andere bestandsnaam of een ander overlay-pad maakt de poort leeg, en dat moet
    luid zijn: anders blijft de toets groen terwijl er niets meer bewaakt wordt."""
    (tmp_path / "overlays").mkdir()
    monkeypatch.setattr(rotate, "ZAD_ENV_OVERLAYS", tmp_path / "overlays")

    gaps = rotate.zad_env_coverage_gaps()

    assert len(gaps) == 1, gaps
    assert "de poort meet niets" in gaps[0]


def test_elke_sleutel_van_het_echte_envsecret_is_benoemd() -> None:
    """De poort is gesloten voor deze repo: elke sleutel in het envsecret van ZAD heeft een
    rij, staat in ZAD_ENV_CONFIG of in ZAD_ENV_NON_ROTATABLE."""
    assert rotate.zad_env_coverage_gaps() == []


# --- de drie nieuwe rijen ----------------------------------------------------------------


def test_secret_key_wordt_gegenereerd_boven_de_ondergrens_van_opi() -> None:
    """De gegenereerde waarde moet door de startcontrole van OPI komen, anders weigert de
    app te starten op het geheim dat de tool net schreef."""
    kind, length = rotate.BY_KEY["secret-key"].gen_without_template
    assert length >= MIN_SECRET_KEY_LENGTH
    validate_secret_key(generate(kind, length))


def test_secret_key_toont_de_herstart_van_opi_na_de_secret_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """De waarde leeft alleen in het envsecret: envbestand, een mens regenereert het secret,
    en pas als het cluster die waarde draagt herstart OPI."""
    kube = _dry_kube(monkeypatch)
    component = rotate.BY_KEY["secret-key"]
    ctx = _dry_ctx(kube, component)
    ctx.cluster = _cluster()
    steps = rotate.steps_for(component, _cluster(), kube, ctx)
    fases = _fases(steps)
    assert "file" not in fases, "geen template, dus geen bestandsstap in de secrets-boom"
    assert "push" not in fases, "het envsecret heeft geen Argo-eigenaar om naar te pushen"
    assert fases.index("env") < fases.index("manual") < fases.index("sync") < fases.index("restart")
    assert any(rotate.OPI_DEPLOYMENT in line for step in steps for line in step.dry_text)


def test_de_handmatige_stap_noemt_de_deploy_task_per_cluster(monkeypatch: pytest.MonkeyPatch) -> None:
    """De OPI-overlay hangt in geen ArgoCD-Application: zonder deze stap blijft het nieuwe
    envsecret in git staan en loopt de gate erna leeg op zijn time-out."""
    kube = _dry_kube(monkeypatch)
    component = rotate.BY_KEY["secret-key"]
    for naam, verwacht in (
        ("odcn-production", "CLUSTER_TYPE=odcn-production task deploy-operations-manager"),
        ("sandboxed-local", "task sandbox:update-operations-manager"),
    ):
        cluster = rotate.Cluster(name=naam, namespace="ns", folders={"infrastructure": "odcn", "bootstrap": naam})
        steps = rotate.zad_env_steps(component, cluster, defer_restarts=False)
        manual = next(step for step in steps if step.phase == "manual")
        assert any(line.startswith(verwacht) for line in manual.dry_text), f"{naam}: {manual.dry_text}"


def test_de_externe_rijen_voor_het_envsecret_tonen_check_extern_en_verify(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kube = _dry_kube(monkeypatch)
    for key in ("keycloak-master-oidc", "registry"):
        component = rotate.BY_KEY[key]
        ctx = _dry_ctx(kube, component)
        ctx.cluster = _cluster()
        fases = _fases(rotate.steps_for(component, _cluster(), kube, ctx))
        assert fases.index("check") < fases.index("manual") < fases.index("verify") < fases.index("env"), (
            f"{key}: {fases}"
        )


class _ExecKube:
    """Een Kube die elke exec beantwoordt met een vast antwoord en de argumenten onthoudt."""

    dry_run = False
    namespace = "rig-prd-operations"

    def __init__(self, stdout: str, returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.commands: list[list[str]] = []

    def exec(self, workload: str, command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        return subprocess.CompletedProcess(command, self.returncode, self.stdout, "")


def _ctx_met(kube: object, component: rotate.Component, current: str, new: str) -> rotate.Ctx:
    field = component.rotate_fields[0]
    return rotate.Ctx(
        cluster=_cluster(),
        kube=kube,  # type: ignore[arg-type]
        current={field: current},
        new={field: new},
        apply_changes=True,
        rotating=field,
    )


def test_de_oidc_verificatie_draait_op_de_nieuwe_waarde_en_faalt_op_een_401() -> None:
    component = rotate.BY_KEY["keycloak-master-oidc"]
    kube = _ExecKube("AANVAARD 400\n")
    verify = next(s for s in rotate.keycloak_master_oidc_steps(kube, None) if s.phase == "verify")  # type: ignore[arg-type]
    ctx = _ctx_met(kube, component, "oud-geheim", "nieuw-geheim")
    assert verify.run is not None
    assert "AANVAARD" in verify.run(ctx)
    assert "W=nieuw-geheim" in kube.commands[0]

    geweigerd = _ExecKube("GEWEIGERD 401\n")
    with pytest.raises(EditFailed):
        verify.run(_ctx_met(geweigerd, component, "oud-geheim", "nieuw-geheim"))


def test_de_registry_verificatie_draait_op_de_nieuwe_waarde_en_faalt_zonder_200() -> None:
    component = rotate.BY_KEY["registry"]
    kube = _ExecKube("HTTP 200\n")
    ctx = _ctx_met(kube, component, "oud-token", "nieuw-token")
    verify = next(s for s in rotate.registry_steps(kube, ctx) if s.phase == "verify")
    assert verify.run is not None
    assert "HTTP 200" in verify.run(ctx)
    assert "W=nieuw-token" in kube.commands[0]

    mislukt = _ExecKube("HTTP 401\n")
    with pytest.raises(EditFailed):
        verify.run(_ctx_met(mislukt, component, "oud-token", "nieuw-token"))


def test_de_registry_ronde_eindigt_met_het_intrekken_van_het_oude_token() -> None:
    """Een nieuw robot-token komt NAAST het oude, dus zonder die laatste stap verandert een
    rotatie niets. Hij moet dus na de verificatie staan, niet ervoor."""
    component = rotate.BY_KEY["registry"]
    kube = _ExecKube("HTTP 200\n")
    steps = rotate.registry_steps(kube, _ctx_met(kube, component, "oud-token", "nieuw-token"))
    assert _fases(steps) == ["check", "manual", "verify", "manual"]
    assert "OUDE token" in steps[-1].title


def _cluster_met_configmap(tmp_path: Path, naam: str, regels: str) -> rotate.Cluster:
    bootstrap = tmp_path / "bootstrap/rig-system/kustomize/operations-manager/overlays" / naam
    bootstrap.mkdir(parents=True)
    (bootstrap / "configmap.yaml").write_text(regels, encoding="utf-8")
    return rotate.Cluster(name=naam, namespace="ns", folders={"infrastructure": naam, "bootstrap": naam})


def test_registry_config_leest_url_org_en_robotnaam_uit_de_configmap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rotate, "REPO", tmp_path)
    cluster = _cluster_met_configmap(
        tmp_path,
        "een-cluster",
        "REGISTRY_URL=rcr.example.test\nREGISTRY_ORG=rig/zad\nREGISTRY_USERNAME=rig+zad\n",
    )
    assert rotate.registry_config(cluster) == {
        "REGISTRY_URL": "rcr.example.test",
        "REGISTRY_ORG": "rig/zad",
        "REGISTRY_USERNAME": "rig+zad",
    }


def test_registry_config_weigert_zonder_registry_url_of_username(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Zonder beide sleutels is de registry voor dit cluster niet ingericht; de poging moet
    met een leesbare fout stoppen in plaats van met een lege of halve configuratie door te
    gaan naar een echte API-aanroep."""
    monkeypatch.setattr(rotate, "REPO", tmp_path)
    cluster = _cluster_met_configmap(tmp_path, "een-cluster", "REGISTRY_ORG=rig/zad\n")
    with pytest.raises(EditFailed, match=r"REGISTRY_URL.*REGISTRY_USERNAME"):
        rotate.registry_config(cluster)


def test_registry_config_weigert_zonder_configmap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rotate, "REPO", tmp_path)
    cluster = rotate.Cluster(
        name="ontbreekt", namespace="ns", folders={"infrastructure": "x", "bootstrap": "ontbreekt"}
    )
    with pytest.raises(EditFailed, match="niet gevonden"):
        rotate.registry_config(cluster)


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


# --- RC-345: het ArgoCD-token van het account opi -----------------------------------


def _fake_jwt(jti: str | None = "jti-1") -> str:
    """Een JWT die er echt genoeg uitziet voor `token_id_of`: twee punten, een geldige
    base64url-payload met (of zonder) een jti-claim."""
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    claims = {} if jti is None else {"jti": jti}
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"{header}.{payload}.sig"


def test_token_id_of_leest_de_jti_claim_en_weigert_wat_geen_jwt_is() -> None:
    assert rotate.token_id_of(_fake_jwt("abc-123")) == "abc-123"

    with pytest.raises(EditFailed, match="JWT"):
        rotate.token_id_of("dit-is-geen-jwt")

    with pytest.raises(EditFailed, match="jti"):
        rotate.token_id_of(_fake_jwt(None))


class _TokenKube:
    """Een Kube voor de ArgoCD-token- en webhook-stappen: exec en run zijn los te stellen,
    secret_value komt uit een kleine tabel zodat een ontbrekend secret simpel is te zetten."""

    dry_run = False

    def __init__(
        self,
        exec_responder: object = None,
        secret_values: dict[tuple[str, str], str | None] | None = None,
        namespace: str = "rig-prd-operations",
    ) -> None:
        self.namespace = namespace
        self._exec_responder = exec_responder or (lambda cmd: subprocess.CompletedProcess(cmd, 0, "", ""))
        self._secret_values = secret_values or {}
        self.exec_calls: list[list[str]] = []
        self.run_calls: list[tuple[list[str], str | None]] = []

    def secret_value(self, name: str, key: str) -> str | None:
        return self._secret_values.get((name, key), "adminpw")

    def exec(self, workload: str, command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        self.exec_calls.append(command)
        return self._exec_responder(command)  # type: ignore[misc]

    def run(
        self, args: list[str], *, check: bool = True, input_text: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        self.run_calls.append((args, input_text))
        return subprocess.CompletedProcess(args, 0, "", "")


def _opi_token_steps(kube: object) -> list[rotate.Step]:
    component = rotate.BY_KEY["argocd-opi-token"]
    return rotate.argocd_opi_token_steps(component, _cluster(), kube, None)  # type: ignore[arg-type]


def _ctx_voor_opi_token(kube: object, *, current: str | None = None, new: str | None = None) -> rotate.Ctx:
    field = rotate.BY_KEY["argocd-opi-token"].rotate_fields[0]
    return rotate.Ctx(
        cluster=_cluster(),
        kube=kube,  # type: ignore[arg-type]
        current={field: current} if current is not None else {},
        new={field: new} if new is not None else {},
        apply_changes=True,
        rotating=field,
    )


def test_opi_token_check_meldt_eerste_uitgifte_zonder_te_proberen() -> None:
    kube = _TokenKube()
    check = next(s for s in _opi_token_steps(kube) if s.phase == "check")
    assert check.run is not None
    assert "eerste uitgifte" in check.run(_ctx_voor_opi_token(kube))
    assert kube.exec_calls == []


def test_opi_token_check_meldt_drift_als_het_huidige_token_wordt_afgewezen() -> None:
    kube = _TokenKube(lambda cmd: subprocess.CompletedProcess(cmd, 0, "FATA Unauthenticated", ""))
    check = next(s for s in _opi_token_steps(kube) if s.phase == "check")
    assert check.run is not None
    with pytest.raises(EditFailed, match="wijst dit token af"):
        check.run(_ctx_voor_opi_token(kube, current="oud-token"))


def test_opi_token_issue_leest_het_nieuwe_token_en_zijn_id_uit_de_cli_output() -> None:
    token = _fake_jwt("jti-nieuw")

    def _antwoord(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        if "generate-token" in " ".join(cmd):
            return subprocess.CompletedProcess(cmd, 0, f"enkele info-regel\n{token}\n", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    kube = _TokenKube(_antwoord)
    issue = next(s for s in _opi_token_steps(kube) if "aanmaken" in s.title)
    assert issue.run is not None
    ctx = _ctx_voor_opi_token(kube)
    result = issue.run(ctx)

    assert "jti-nieuw" in result
    assert ctx.new[ctx.rotating] == token


def test_opi_token_issue_faalt_als_de_cli_geen_token_teruggeeft() -> None:
    kube = _TokenKube(lambda cmd: subprocess.CompletedProcess(cmd, 0, "FATA permission denied", ""))
    issue = next(s for s in _opi_token_steps(kube) if "aanmaken" in s.title)
    assert issue.run is not None
    with pytest.raises(EditFailed, match="geen token"):
        issue.run(_ctx_voor_opi_token(kube))


def test_opi_token_write_secret_zet_de_namespace_en_het_nieuwe_token() -> None:
    kube = _TokenKube(namespace="rig-system")
    write = next(s for s in _opi_token_steps(kube) if "zetten" in s.title)
    assert write.run is not None
    result = write.run(_ctx_voor_opi_token(kube, new="vers-token"))

    assert rotate.OPI_TOKEN_SECRET in result
    args, input_text = kube.run_calls[-1]
    assert "apply" in args
    assert input_text is not None
    assert "rig-system" in input_text
    assert "vers-token" in input_text


def test_opi_token_verify_nieuw_faalt_als_het_verse_token_wordt_afgewezen() -> None:
    kube = _TokenKube(lambda cmd: subprocess.CompletedProcess(cmd, 0, "FATA invalid session", ""))
    verify = next(s for s in _opi_token_steps(kube) if s.phase == "verify" and "nieuwe" in s.title)
    assert verify.run is not None
    with pytest.raises(EditFailed, match="wijst dit token af"):
        verify.run(_ctx_voor_opi_token(kube, new="vers-token"))


def test_opi_token_revoke_old_trekt_in_en_bewijst_dat_het_oude_token_dood_is() -> None:
    def _antwoord(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        joined = " ".join(cmd)
        if "delete-token" in joined:
            return subprocess.CompletedProcess(cmd, 0, "token deleted", "")
        if "app list" in joined:
            # Na het intrekken wijst ArgoCD het oude token (altijd T=oud-token) af.
            return subprocess.CompletedProcess(cmd, 0, "FATA Unauthenticated", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    kube = _TokenKube(_antwoord)
    revoke = next(s for s in _opi_token_steps(kube) if "intrekken" in s.title)
    assert revoke.run is not None
    result = revoke.run(_ctx_voor_opi_token(kube, current=_fake_jwt("jti-oud")))

    assert "jti-oud" in result
    assert "ingetrokken" in result
    assert any("delete-token" in " ".join(c) for c in kube.exec_calls)


def test_opi_token_revoke_old_faalt_als_het_oude_token_na_intrekken_nog_werkt() -> None:
    def _antwoord(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        joined = " ".join(cmd)
        if "delete-token" in joined:
            return subprocess.CompletedProcess(cmd, 0, "token deleted", "")
        # Het oude token blijft werken: de wissel is niet doorgekomen.
        return subprocess.CompletedProcess(cmd, 0, "[]", "")

    kube = _TokenKube(_antwoord)
    revoke = next(s for s in _opi_token_steps(kube) if "intrekken" in s.title)
    assert revoke.run is not None
    with pytest.raises(EditFailed, match="OUDE waarde"):
        revoke.run(_ctx_voor_opi_token(kube, current=_fake_jwt("jti-oud")))


def test_opi_token_revoke_old_quote_de_jti_uit_het_secret() -> None:
    """De jti komt uit het Secret en gaat een `sh -c` in waar het adminwachtwoord als $W in
    de omgeving staat. Wie dat Secret kan schrijven zou er anders een commando in zetten dat
    bij de volgende rotatie in de argocd-server-pod draait."""

    def _antwoord(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        joined = " ".join(cmd)
        if "delete-token" in joined:
            return subprocess.CompletedProcess(cmd, 0, "token deleted", "")
        return subprocess.CompletedProcess(cmd, 0, "FATA Unauthenticated", "")

    kube = _TokenKube(_antwoord)
    revoke = next(s for s in _opi_token_steps(kube) if "intrekken" in s.title)
    assert revoke.run is not None
    revoke.run(_ctx_voor_opi_token(kube, current=_fake_jwt("x; id > /tmp/pwned #")))

    script = next(" ".join(c) for c in kube.exec_calls if "delete-token" in " ".join(c))
    assert "'x; id > /tmp/pwned #'" in script, script
    assert "opi x; id" not in script, script


def test_opi_token_revoke_old_meldt_niets_in_te_trekken_bij_de_eerste_uitgifte() -> None:
    kube = _TokenKube()
    revoke = next(s for s in _opi_token_steps(kube) if "intrekken" in s.title)
    assert revoke.run is not None
    assert "niets in te trekken" in revoke.run(_ctx_voor_opi_token(kube))
    assert kube.exec_calls == []


def test_gather_ctx_voor_de_eerste_uitgifte_laat_current_leeg_en_vraagt_niets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("builtins.input", lambda *a: (_ for _ in ()).throw(AssertionError("mag niet vragen")))
    component = rotate.BY_KEY["argocd-opi-token"]
    kube = _TokenKube(secret_values={(rotate.OPI_TOKEN_SECRET, "token"): None})

    ctx = rotate.gather_ctx(component, _cluster(), kube, apply_changes=True)  # type: ignore[arg-type]

    assert ctx.current == {}
    assert ctx.new == {component.rotate_fields[0]: ""}


def test_gather_ctx_voor_een_bestaand_token_leest_de_huidige_waarde_en_vraagt_niets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("builtins.input", lambda *a: (_ for _ in ()).throw(AssertionError("mag niet vragen")))
    component = rotate.BY_KEY["argocd-opi-token"]
    kube = _TokenKube(secret_values={(rotate.OPI_TOKEN_SECRET, "token"): "huidig-token"})

    ctx = rotate.gather_ctx(component, _cluster(), kube, apply_changes=True)  # type: ignore[arg-type]

    assert ctx.current == {component.rotate_fields[0]: "huidig-token"}
    assert ctx.new == {component.rotate_fields[0]: ""}


def test_check_component_bij_de_eerste_uitgifte_meldt_eerste_uitgifte_en_niet_overgeslagen() -> None:
    component = rotate.BY_KEY["argocd-opi-token"]
    kube = _TokenKube(secret_values={(rotate.OPI_TOKEN_SECRET, "token"): None}, namespace="rig-system")

    report = "\n".join(rotate.check_component(component, _cluster(), kube))  # type: ignore[arg-type]

    assert "overgeslagen" not in report
    assert "eerste uitgifte" in report


def test_check_component_zonder_issued_by_system_slaat_een_leeg_secret_wel_over() -> None:
    """Contrast met hierboven: zonder `issued_by_system` is een ontbrekend secret wel een
    reden om de check voor dit component over te slaan."""
    component = rotate.BY_KEY["argocd-admin"]
    key = component.current_from_secret
    kube = _TokenKube(secret_values={key: None}, namespace="rig-system")  # type: ignore[arg-type]

    report = "\n".join(rotate.check_component(component, _cluster(), kube))  # type: ignore[arg-type]

    assert "overgeslagen" in report


# --- RC-345: het GitHub-webhookgeheim van ArgoCD ------------------------------------


def _webhook_steps(kube: object) -> list[rotate.Step]:
    return rotate.argocd_webhook_github_steps(kube, None)  # type: ignore[arg-type]


def _ctx_voor_webhook(kube: object, *, current: str | None = None, new: str | None = None) -> rotate.Ctx:
    field = rotate.BY_KEY["argocd-webhook-github"].rotate_fields[0]
    return rotate.Ctx(
        cluster=_cluster(),
        kube=kube,  # type: ignore[arg-type]
        current={field: current} if current is not None else {},
        new={field: new} if new is not None else {},
        apply_changes=True,
        rotating=field,
    )


def test_webhook_check_meldt_dat_elke_webhook_wordt_aangenomen_zonder_huidig_geheim() -> None:
    kube = _TokenKube()
    check = next(s for s in _webhook_steps(kube) if s.phase == "check")
    assert check.run is not None
    assert "elke webhook" in check.run(_ctx_voor_webhook(kube))
    assert kube.exec_calls == []


def test_webhook_patch_zet_het_nieuwe_geheim_op_de_juiste_key_in_argocd_secret() -> None:
    kube = _TokenKube()
    patch = next(s for s in _webhook_steps(kube) if s.phase == "apply")
    assert patch.run is not None
    result = patch.run(_ctx_voor_webhook(kube, new="nieuw-geheim"))

    assert "argocd-secret" in result
    args, input_text = kube.run_calls[-1]
    assert "patch" in args
    assert input_text is not None
    assert "nieuw-geheim" in input_text
    assert rotate.WEBHOOK_KEY in input_text


def test_webhook_verify_faalt_als_de_proef_geen_200_teruggeeft() -> None:
    kube = _TokenKube(lambda cmd: subprocess.CompletedProcess(cmd, 0, "403", ""))
    verify = next(s for s in _webhook_steps(kube) if s.phase == "verify")
    assert verify.run is not None
    with pytest.raises(EditFailed):
        verify.run(_ctx_voor_webhook(kube, new="nieuw-geheim"))


def test_webhook_verify_slaagt_en_wijst_het_oude_geheim_af() -> None:
    def _antwoord(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        env = next((a for a in cmd if a.startswith("S=")), "")
        return subprocess.CompletedProcess(cmd, 0, "200" if env == "S=nieuw-geheim" else "403", "")

    kube = _TokenKube(_antwoord)
    verify = next(s for s in _webhook_steps(kube) if s.phase == "verify")
    assert verify.run is not None
    result = verify.run(_ctx_voor_webhook(kube, current="oud-geheim", new="nieuw-geheim"))

    assert "HTTP 200" in result
    assert "ingetrokken" not in result  # geen rotatiejargon van het token-component


def test_gather_ctx_voor_de_eerste_webhookuitgifte_laat_current_leeg_en_vraagt_de_waarde(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    component = rotate.BY_KEY["argocd-webhook-github"]
    monkeypatch.setattr("builtins.input", lambda *a: "nieuw-geheim")
    kube = _TokenKube(secret_values={component.current_from_secret: None})  # type: ignore[arg-type]

    ctx = rotate.gather_ctx(component, _cluster(), kube, apply_changes=True)  # type: ignore[arg-type]

    assert ctx.current == {}
    assert ctx.new == {component.rotate_fields[0]: "nieuw-geheim"}


def test_check_component_bij_de_eerste_webhookuitgifte_meldt_het_ontbreken_en_slaat_niet_over() -> None:
    component = rotate.BY_KEY["argocd-webhook-github"]
    kube = _TokenKube(secret_values={component.current_from_secret: None}, namespace="rig-system")  # type: ignore[arg-type]

    report = "\n".join(rotate.check_component(component, _cluster(), kube))  # type: ignore[arg-type]

    assert "overgeslagen" not in report
    assert "elke webhook" in report


# --- RC-345: execute(assume_yes=...) voor een non-interactieve ronde ---------------


def _weigerende_input(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "builtins.input", lambda *a: (_ for _ in ()).throw(AssertionError("assume_yes mag niets vragen"))
    )


def test_execute_met_assume_yes_breekt_op_een_handmatige_stap_zonder_te_vragen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _weigerende_input(monkeypatch)
    steps = [rotate.Step("Zet dit klaar", "manual", ["doe iets buiten de tool"], None)]
    ctx = _ctx_voor_opi_token(_TokenKube())

    assert rotate.execute(ctx, steps, assume_yes=True) is False


def test_execute_met_assume_yes_breekt_op_een_mislukte_check_zonder_te_vragen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _weigerende_input(monkeypatch)

    def _faal(ctx: rotate.Ctx) -> str:
        raise EditFailed("drift")

    steps = [rotate.Step("Check", "check", ["x"], _faal)]
    ctx = _ctx_voor_opi_token(_TokenKube())

    assert rotate.execute(ctx, steps, assume_yes=True) is False


def test_execute_met_assume_yes_voert_gewone_stappen_uit_zonder_te_vragen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _weigerende_input(monkeypatch)
    gedaan: list[int] = []
    steps = [rotate.Step("Doe iets", "apply", ["x"], lambda ctx: (gedaan.append(1), "gedaan")[1])]
    ctx = _ctx_voor_opi_token(_TokenKube())

    assert rotate.execute(ctx, steps, assume_yes=True) is True
    assert gedaan == [1]


class TestYesVraagtNiets:
    """--yes is voor een ronde zonder mens: ook de vraag om een nieuwe waarde valt weg."""

    def test_een_genereerbaar_veld_wordt_zonder_vraag_gegenereerd(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def geen_vraag(_prompt: str) -> str:
            raise AssertionError("met assume_yes hoort er niets gevraagd te worden")

        monkeypatch.setattr("builtins.input", geen_vraag)
        component = rotate.BY_KEY["secret-key"]

        waarde = rotate.new_value_for(component, "SECRET_KEY", {"SECRET_KEY": ("random", 48)}, assume_yes=True)

        assert len(waarde) >= 32

    def test_een_waarde_van_buiten_weigert_in_plaats_van_te_wachten(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def geen_vraag(_prompt: str) -> str:
            raise AssertionError("met assume_yes hoort er niets gevraagd te worden")

        monkeypatch.setattr("builtins.input", geen_vraag)
        component = rotate.BY_KEY["grafana"]
        veld = component.rotate_fields[0]

        with pytest.raises(rotate.EditFailed, match="vraagt een waarde van een mens"):
            rotate.new_value_for(component, veld, {}, assume_yes=True)
