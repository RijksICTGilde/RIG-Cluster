"""De nachtelijke lifecycle-ronde van seaweedfs-storage (RC-381, rij G20).

Deze toetsen lezen MANIFESTEN, geen Python. SeaweedFS bewaart de
NoncurrentVersionExpiration-regel op de bucket, maar ruimt pas op in deze ronde: zonder
hem of met een verkeerde aanroep staat de grens voor altijd zonder effect, en dat valt
niet op totdat iemand de bucketgrootte nameet. Drie fouten in de aanroep kostten elk een
meetgang op de sandbox (RC-381): zonder `-refresh 0` blijft het commando draaien zonder
ooit een pass te doen, en `-filer`/`-master` op de gRPC-poort in plaats van de HTTP-poort
laat `weed shell` hangen zonder fout.
"""

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]

CONTROLLER = REPO_ROOT / "infrastructure/bootstrap/infrastructure/seaweedfs/controller"
CRONJOB_MANIFEST = CONTROLLER / "base/lifecycle-cronjob.yaml"
DEPLOYMENT_MANIFEST = CONTROLLER / "base/deployment.yaml"

NAAM = "seaweedfs-lifecycle"

#: `weed shell` telt er zelf 10000 bij op, zoals SEAWEEDFS_FILER_ADDRESS in de configmap
#: ook de HTTP-poort noemt. Met de gRPC-poort (18888/19333) hangt het commando zonder fout.
FILER_HTTP = "seaweedfs:8888"
MASTER_HTTP = "seaweedfs:9333"
S3_GRPC = "seaweedfs:18333"


def _cronjob(overlay: Path) -> dict:
    """De CronJob zoals kustomize hem oplevert, niet zoals het losse manifest hem schrijft."""
    proc = subprocess.run(["kustomize", "build", str(overlay)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return next(
        d for d in yaml.safe_load_all(proc.stdout) if d and d["kind"] == "CronJob" and d["metadata"]["name"] == NAAM
    )


def _uit_manifest() -> dict:
    return yaml.safe_load(CRONJOB_MANIFEST.read_text())


def _podspec(cronjob: dict) -> dict:
    return cronjob["spec"]["jobTemplate"]["spec"]["template"]["spec"]


def _container(cronjob: dict) -> dict:
    return _podspec(cronjob)["containers"][0]


def _script(cronjob: dict) -> str:
    return _container(cronjob)["command"][2]


@pytest.fixture(scope="module")
def cronjob() -> dict:
    return _uit_manifest()


@pytest.mark.parametrize("overlay", ["base", "overlays/sandboxed-local", "overlays/odcn", "overlays/odcn-staging"])
def test_elke_overlay_rendert_de_cronjob(overlay: str) -> None:
    if shutil.which("kustomize") is None:
        pytest.skip("kustomize ontbreekt")
    assert _cronjob(CONTROLLER / overlay)["metadata"]["name"] == NAAM


def test_het_commando_doet_een_enkele_pass(cronjob: dict) -> None:
    """Zonder `-refresh 0` blijft het commando draaien zonder ooit een pass te doen
    (gemeten: 10 minuten, 50 versies onveranderd)."""
    assert "-refresh 0" in _script(cronjob)


def test_het_commando_praat_de_http_poort_van_filer_en_master(cronjob: dict) -> None:
    """`-filer` en `-master` dragen de HTTP-poort, niet de gRPC-poort: `weed shell` telt er
    zelf 10000 bij op, dus met 18888/19333 dialt het commando 28888/29333 en hangt het."""
    script = _script(cronjob)
    assert f"-filer={FILER_HTTP}" in script
    assert f"-master={MASTER_HTTP}" in script
    assert "-filer=seaweedfs:18888" not in script
    assert "-master=seaweedfs:19333" not in script


def test_het_commando_roept_de_s3_gateway_aan_op_de_grpc_poort(cronjob: dict) -> None:
    """`-s3` is het adres van de S3-gateway voor `s3.lifecycle.run-shard`, en dat is de
    gRPC-poort (18333), niet de S3-poort zelf (8333)."""
    assert f"-s3 {S3_GRPC}" in _script(cronjob)


def test_het_commando_runt_alle_zestien_shards(cronjob: dict) -> None:
    """SeaweedFS verdeelt buckets over precies 16 shards; met een kleiner bereik blijft
    een deel van de buckets ongemoeid."""
    assert "-shards 0-15" in _script(cronjob)


def test_het_commando_gaat_over_stdin_en_niet_in_argv(cronjob: dict) -> None:
    """`weed shell` heeft geen niet-interactieve vorm met het commando in argv; argv komt
    in `ps` en in de logs van de pod terecht."""
    command = _container(cronjob)["command"]
    assert command[:2] == ["sh", "-c"]
    assert "s3.lifecycle.run-shard" not in command


def test_twee_runs_lopen_niet_door_elkaar(cronjob: dict) -> None:
    """Forbid: een halve ronde afbreken en opnieuw starten verdubbelt het werk niet, het
    laat hem gewoon overslaan tot de volgende nacht."""
    assert cronjob["spec"]["concurrencyPolicy"] == "Forbid"


def test_een_onbereikbare_master_laat_de_ronde_niet_voor_altijd_hangen(cronjob: dict) -> None:
    """Een onbereikbare master laat `weed shell` in WaitUntilConnected wachten zonder ooit
    een fout te geven (rij G18); de tijdslimiet is de enige grens."""
    assert cronjob["spec"]["jobTemplate"]["spec"]["activeDeadlineSeconds"] > 0


def test_de_image_staat_niet_op_een_bewegende_tag(cronjob: dict) -> None:
    image = _container(cronjob)["image"]
    assert "@sha256:" in image, image
    assert not image.endswith(":latest"), image


def test_de_ronde_draait_op_dezelfde_digest_als_de_pod(cronjob: dict) -> None:
    """Hetzelfde image op dezelfde digest als de Deployment: een upgrade van de pod zonder
    de CronJob mee te nemen zou de ronde tegen een andere `weed`-versie laten draaien."""
    pod_image = yaml.safe_load(DEPLOYMENT_MANIFEST.read_text())["spec"]["template"]["spec"]["containers"][0]["image"]
    assert _container(cronjob)["image"] == pod_image


def test_de_ronde_heeft_geen_serviceaccount_token(cronjob: dict) -> None:
    assert _podspec(cronjob)["automountServiceAccountToken"] is False


def test_de_ronde_draait_met_een_readonly_root_en_zonder_capabilities(cronjob: dict) -> None:
    sc = _container(cronjob)["securityContext"]
    assert sc["readOnlyRootFilesystem"] is True
    assert sc["runAsNonRoot"] is True
    assert sc["allowPrivilegeEscalation"] is False
    assert sc["capabilities"]["drop"] == ["ALL"]


def test_het_geheim_komt_uit_envfrom_en_niet_uit_argv(cronjob: dict) -> None:
    container = _container(cronjob)
    secret_refs = {ref["secretRef"]["name"] for ref in container.get("envFrom", [])}
    assert "seaweedfs-credentials" in secret_refs

    script = _script(cronjob)
    for vlag in ("--password=", "--secret-access-key=", "--access-key=", "AWS_SECRET_ACCESS_KEY="):
        assert vlag not in script, vlag


@pytest.mark.parametrize("overlay", ["overlays/odcn", "overlays/odcn-staging"])
def test_de_odcn_overlays_halen_de_vaste_uid_ook_van_de_cronjob(overlay: str) -> None:
    """Dezelfde patch als bij de Deployment: OpenShift kent de UID uit het bereik van de
    namespace toe, en een vaste 1000 wordt door de SCC geweigerd."""
    if shutil.which("kustomize") is None:
        pytest.skip("kustomize ontbreekt")
    cronjob = _cronjob(CONTROLLER / overlay)
    podspec = _podspec(cronjob)
    assert "runAsUser" not in podspec["securityContext"]
    assert "runAsGroup" not in podspec["securityContext"]
    container_sc = _container(cronjob)["securityContext"]
    assert "runAsUser" not in container_sc
    assert "runAsGroup" not in container_sc
    # Non-root en read-only blijven staan, alleen het vaste getal gaat weg.
    assert container_sc["runAsNonRoot"] is True
