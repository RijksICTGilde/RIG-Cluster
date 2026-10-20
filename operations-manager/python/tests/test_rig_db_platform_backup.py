"""De nachtelijke back-up van de platformdatabase.

Deze toetsen lezen MANIFESTEN, geen Python. Wat hier fout kan gaan gaat stil fout: een
CronJob die een secretsleutel noemt die niet bestaat start pas om half drie 's nachts niet,
en een geheim dat als `value` in de podspec staat is uit elk `kubectl get cronjob` te lezen.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]

DATABASE = REPO_ROOT / "infrastructure/bootstrap/infrastructure/postgresql/database"
CRONJOB = DATABASE / "base/platform-backup-cronjob.yaml"
CLUSTER = DATABASE / "base/cluster.yaml"
SECRET_TEMPLATE = REPO_ROOT / "infrastructure/bootstrap/infrastructure/secrets/templates/platform-backup-secret.yaml"
ODCN_SECRETS = REPO_ROOT / "infrastructure/bootstrap/infrastructure/secrets/config/overlays/odcn"
MINIO_NETWORKPOLICY = (
    REPO_ROOT
    / "infrastructure/bootstrap/infrastructure/backup-destination/controller"
    / "overlays/odcn/network-policies/minio-networkpolicy.yaml"
)

NAAM = "rig-db-platform-backup"

#: De rol die de dump leest, en het geheim waar zijn wachtwoord en het Kopia-wachtwoord in
#: staan. Drie lezers (CNPG, de CronJob, de sandbox-generator), dus een naam die aan een
#: kant verandert moet hier opvallen.
LEESROL = "platform_backup"
GEHEIM = "platform-backup-credentials"

#: De databases van het platform zelf. Forgejo draait op productie niet op dit cluster, dus
#: die staat apart: ontbreekt hij, dan slaat het script hem over in plaats van te falen.
VERPLICHTE_DATABASES = ("keycloak", "operations_manager", "mailrelay")
OPTIONELE_DATABASES = ("forgejo",)

NONCURRENT_EXPIRE_DAYS = 210  # Oudere objectversies: na 210 dagen opruimen

#: Een env-variabele met zo'n naam hoort uit een secretKeyRef te komen en nooit uit `value`.
GEHEIME_NAAM = re.compile(r"PASSWORD|SECRET|ACCESS_KEY")


def _cronjob(pad: Path) -> dict:
    """De CronJob zoals kustomize hem oplevert, niet zoals het losse manifest hem schrijft."""
    proc = subprocess.run(["kustomize", "build", str(pad)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return next(
        d for d in yaml.safe_load_all(proc.stdout) if d and d["kind"] == "CronJob" and d["metadata"]["name"] == NAAM
    )


def _uit_manifest() -> dict:
    return yaml.safe_load(CRONJOB.read_text())


def _container(cronjob: dict) -> dict:
    return cronjob["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]


def _script(cronjob: dict) -> str:
    return _container(cronjob)["command"][2]


@pytest.fixture(scope="module")
def cronjob() -> dict:
    return _uit_manifest()


@pytest.mark.parametrize("pad", ["base", "overlays/sandboxed-local", "overlays/odcn"])
def test_elke_overlay_rendert_de_cronjob(pad: str) -> None:
    if shutil.which("kustomize") is None:
        pytest.skip("kustomize ontbreekt")
    assert _cronjob(DATABASE / pad)["metadata"]["name"] == NAAM


def test_het_script_dumpt_de_platformdatabases(cronjob: dict) -> None:
    script = _script(cronjob)
    assert f'REQUIRED_DATABASES="{" ".join(VERPLICHTE_DATABASES)}"' in script
    assert f'OPTIONAL_DATABASES="{" ".join(OPTIONELE_DATABASES)}"' in script


def test_de_bucket_wordt_met_object_lock_aangemaakt(cronjob: dict) -> None:
    """Object lock alleen bij aanmaken gezet, geen default retention (dat blokkeert kopia)."""
    script = _script(cronjob)
    assert 'mc mb --with-lock --ignore-existing "backup/${S3_BUCKET}"' in script
    assert "retention set" not in script


def test_oudere_versies_verlopen_na_de_langste_bewaarregel(cronjob: dict) -> None:
    """Object lock zet versioning aan, dus zonder deze regel groeit de bucket door met de
    blobs die kopia al heeft laten vallen.
    """
    script = _script(cronjob)
    assert f"mc ilm rule add --noncurrent-expire-days {NONCURRENT_EXPIRE_DAYS}" in script

    maandelijks = re.search(r"--keep-monthly=(\d+)", script)
    assert maandelijks, "de bewaarregel noemt geen --keep-monthly"
    maanden = int(maandelijks.group(1))
    assert maanden * 31 < NONCURRENT_EXPIRE_DAYS, f"korter dan de {maanden} maandelijkse momenten"


def test_twee_runs_lopen_niet_door_elkaar(cronjob: dict) -> None:
    """Forbid en niet Replace: een halve dump afbreken levert geen bruikbare back-up op."""
    assert cronjob["spec"]["concurrencyPolicy"] == "Forbid"


def test_de_image_staat_niet_op_een_bewegende_tag(cronjob: dict) -> None:
    """`latest` beweegt mee met elke publish, en van deze image is dat de enige tag."""
    image = _container(cronjob)["image"]
    assert "@sha256:" in image, image
    assert not image.endswith(":latest"), image


def test_geen_geheim_als_waarde_in_de_podspec(cronjob: dict) -> None:
    for env in _container(cronjob)["env"]:
        if not GEHEIME_NAAM.search(env["name"]):
            continue
        assert "value" not in env, f"{env['name']} staat als waarde in de podspec"
        assert env["valueFrom"]["secretKeyRef"], env["name"]


def test_geen_geheim_in_argv(cronjob: dict) -> None:
    """Alles in argv staat in `ps` en in de logs van de pod; kopia en mc lezen uit de env."""
    script = _script(cronjob)
    for vlag in ("--password=", "--secret-access-key=", "--access-key="):
        assert vlag not in script, vlag


def test_de_rol_en_de_cronjob_lezen_hetzelfde_geheim(cronjob: dict) -> None:
    rollen = yaml.safe_load(CLUSTER.read_text())["spec"]["managed"]["roles"]
    rol = next(r for r in rollen if r["name"] == LEESROL)
    assert rol["passwordSecret"]["name"] == GEHEIM
    assert rol["inRoles"] == ["pg_read_all_data"], "de back-up hoort alleen te lezen"

    verwezen = {
        env["valueFrom"]["secretKeyRef"]["key"]
        for env in _container(cronjob)["env"]
        if env.get("valueFrom", {}).get("secretKeyRef", {}).get("name") == GEHEIM
    }
    template = yaml.safe_load(SECRET_TEMPLATE.read_text())
    assert template["metadata"]["name"] == GEHEIM
    assert template["stringData"]["username"] == LEESROL
    assert verwezen <= set(template["stringData"]), verwezen - set(template["stringData"])


def test_de_backuppod_mag_de_s3_bestemming_bereiken() -> None:
    """De tenant-regel in dit bestand dekt rig-prd-operations niet (geen capsule-tenant),
    dus zonder een eigen regel kan de back-uppod niet bij MinIO. Smal op het podlabel,
    niet op de hele namespace: anders mag alles in rig-prd-operations naar binnen.
    """
    policy = yaml.safe_load(MINIO_NETWORKPOLICY.read_text())
    regels = [
        regel
        for regel in policy["spec"]["ingress"]
        for van in regel["from"]
        if van.get("podSelector", {}).get("matchLabels", {}).get("app.kubernetes.io/name") == NAAM
    ]
    assert len(regels) == 1, regels
    van = regels[0]["from"][0]
    assert van["namespaceSelector"]["matchLabels"] == {"kubernetes.io/metadata.name": "rig-prd-operations"}
    assert {poort["port"] for poort in regels[0]["ports"]} == {9000}


def test_het_geheim_wordt_per_cluster_gegenereerd_en_uitgerold() -> None:
    """Zonder de regel in decrypt-sops.yaml rendert odcn het geheim niet, en start de pod niet."""
    templates = yaml.safe_load((SECRET_TEMPLATE.parent / "kustomization.yaml").read_text())
    assert SECRET_TEMPLATE.name in templates["resources"]

    bestand = f"{SECRET_TEMPLATE.name}.sops.yaml"
    assert (ODCN_SECRETS / bestand).exists()
    assert bestand in yaml.safe_load((ODCN_SECRETS / "decrypt-sops.yaml").read_text())["files"]
