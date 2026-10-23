"""Live sandbox-E2E voor de dienst ``seaweedfs-storage`` (RC-331, stap 9).

De eisentabel (``test_sandbox_seaweedfs_eisen.py``) meet wat SeaweedFS kan met de hand
gezette identiteiten. Dit meet de weg van een klik tot een draaiende pod: de kaart, het
projectbestand, wat OPI aanmaakt, wat de pod krijgt, en wat er gebeurt als de dienst weer
uit gaat.

Zeven metingen, in de orde waarin een gebruiker ze zou zien falen:

1. De kaart staat op de dienstenstap. Zonder kaart is er geen weg voor een gebruiker,
   hoe compleet de code ook is.
2. De s3-probe van het ``e2e-allservices``-image is groen: schrijven, teruglezen,
   vergelijken en verwijderen met de geinjecteerde sleutels.
3. De DRAAIENDE pod heeft de ``OBJECT_STORE_*``-variabelen, met de host en poort van
   SeaweedFS. Een manifest is geen pod: ``envFrom`` wordt een keer bij de start
   geinjecteerd.
4. Versiebeheer: twee keer dezelfde key, de oudste versie byte voor byte terug.
5. Isolatie: de sleutels van A komen niet bij de bucket van B, en omgekeerd.
6. Uitzetten: bucket, identiteit en policy weg, en de sleutels van A werken niet meer.
   B is ongewijzigd.
7. De tegenmeting voor de NetworkPolicy: een pod in een projectnamespace bereikt 8333 en
   niet de gRPC-poorten.

Slaat over zonder E2E_BASE_URL. Vraagt JOUW build op de sandbox.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

import pytest
from opi.core.cluster_config import get_seaweedfs_host, get_seaweedfs_port
from opi.services.services_enums import ServiceType
from opi.utils.naming import generate_bucket_name, generate_minio_policy_name, generate_minio_username
from tests.e2e.conftest import FORGEJO_VERIFY_SSL, SANDBOX_TEST_USER
from tests.e2e.helpers import cluster, sandbox_api, seaweedfs
from tests.e2e.helpers.edit_modal import EditModalHelper
from tests.e2e.helpers.lifecycle import CreatedProject, create_project_with_services
from tests.e2e.helpers.wizard import WizardHelper, unique_project_name

if TYPE_CHECKING:
    from collections.abc import Generator

    from playwright.sync_api import BrowserContext
    from tests.e2e.helpers.forgejo import ForgejoClient

logger = logging.getLogger(__name__)

pytestmark = [pytest.mark.e2e, pytest.mark.sandbox, pytest.mark.serial]

_VERIFY_SSL = FORGEJO_VERIFY_SSL
_SERVICE = ServiceType.SEAWEEDFS_STORAGE.value
_CLUSTER = "sandboxed-local"
#: Zo lang mag het provisioneren plus de eerste probe-ronde duren.
_PROVISION_TIMEOUT = 600.0


def _maak_project(
    sandbox_context: BrowserContext, sandbox_url: str, forgejo: ForgejoClient, prefix: str
) -> Generator[CreatedProject]:
    page = sandbox_context.new_page()
    created: CreatedProject | None = None
    try:
        created = create_project_with_services(
            page,
            sandbox_url,
            forgejo,
            unique_project_name(prefix=prefix),
            user_email=SANDBOX_TEST_USER["email"],
            services=[_SERVICE],
        )
        logger.info("Project met %s aangemaakt: %s", _SERVICE, created.name)
        yield created
    finally:
        page.close()
        if created is not None:
            sandbox_api.delete_project_via_api(sandbox_url, created.name, created.api_key, verify_ssl=_VERIFY_SSL)


@pytest.fixture(scope="module")
def project_a(sandbox_context: BrowserContext, sandbox_url: str, forgejo: ForgejoClient) -> Generator[CreatedProject]:
    yield from _maak_project(sandbox_context, sandbox_url, forgejo, "swfa")


@pytest.fixture(scope="module")
def project_b(sandbox_context: BrowserContext, sandbox_url: str, forgejo: ForgejoClient) -> Generator[CreatedProject]:
    yield from _maak_project(sandbox_context, sandbox_url, forgejo, "swfb")


@pytest.fixture(scope="module")
def clientpod() -> Generator[None]:
    """Een pod met `aws` in een namespace die de NetworkPolicy als tenant herkent."""
    if not cluster.kubectl_available():
        pytest.skip("kubectl niet beschikbaar; deze meting hoort op de sandboxmachine")
    seaweedfs.zet_clientpod_neer()
    try:
        yield
    finally:
        seaweedfs.ruim_op()


def _sleutels(project: CreatedProject) -> tuple[str, str, str]:
    """Access key, secret key en bucketnaam uit het secret dat OPI schreef."""
    namespace = f"rig-{project.name}"
    waarden = cluster.secret_values(namespace, f"{project.deployment_name}-seaweedfs")
    return waarden["OBJECT_STORE_USER"], waarden["OBJECT_STORE_PASSWORD"], waarden["OBJECT_STORE_BUCKET_NAME"]


def _dienstnamen(forgejo: ForgejoClient, project_name: str) -> list[str]:
    """De dienstnamen zoals ze in het projectbestand in Forgejo staan."""
    from opi.services.services import service_entry_name

    data = forgejo.get_project_yaml(project_name) or {}
    return [service_entry_name(entry) for entry in data.get("services") or []]


def _draaiende_pod(project: CreatedProject) -> tuple[str, str]:
    namespace = f"rig-{project.name}"
    assert cluster.wait_for(
        lambda: bool(cluster.running_pod_names(namespace, project.deployment_name)),
        timeout=_PROVISION_TIMEOUT,
        interval=5,
    ), f"geen draaiende pod in {namespace}"
    return namespace, cluster.running_pod_names(namespace, project.deployment_name)[0]


@pytest.mark.timeout(300)
def test_de_kaart_staat_op_de_dienstenstap(sandbox_context: BrowserContext, sandbox_url: str) -> None:
    """De kaart bestaat alleen waar de clusterconfiguratie een seaweedfs_host kent."""
    assert get_seaweedfs_host(_CLUSTER) is not None, f"cluster '{_CLUSTER}' kent geen SeaweedFS-adres"
    page = sandbox_context.new_page()
    try:
        wizard = WizardHelper(page, sandbox_url)
        wizard.open_create_wizard()
        wizard.fill_identity(display_name=unique_project_name(prefix="swfkaart"), description="seaweedfs kaart e2e")
        wizard.click_next()
        assert page.locator(f"input[name='services[]'][value='{_SERVICE}']").count() > 0, (
            f"de kaart voor '{_SERVICE}' staat niet op de dienstenstap"
        )
    finally:
        page.close()


@pytest.mark.timeout(900)
def test_de_s3_probe_van_de_pod_is_groen(project_a: CreatedProject) -> None:
    """De applicatie schrijft, leest terug, vergelijkt en verwijdert met zijn eigen sleutels.

    "Pod Healthy" bewijst alleen dat het proces startte. Dit bewijst dat de bucket en de
    policy die OPI aanmaakte doen wat de applicatie ervan verwacht.
    """
    if not cluster.kubectl_available():
        pytest.skip("kubectl niet beschikbaar; deze meting hoort op de sandboxmachine")
    namespace, pod = _draaiende_pod(project_a)

    status: dict = {}

    def _groen() -> bool:
        nonlocal status
        try:
            code, body = cluster.http_get_via_port_forward(namespace, pod, 8080, "/status", timeout=15.0)
        except Exception:
            return False
        if code != 200 or '"services"' not in body:
            return False
        status = json.loads(body)
        return bool(status.get("ready"))

    assert cluster.wait_for(_groen, timeout=_PROVISION_TIMEOUT), f"/status werd niet ready in '{namespace}'"
    # Doel-id "minio", want de dienst geeft dezelfde variabelen en het image bindt op die
    # namen (zie KIND_MAP in scripts/generate_probe_spec.py).
    probe = (status.get("services") or {}).get("minio")
    assert probe is not None, f"/status kent geen s3-doel: {status}"
    assert probe.get("bound") is True, f"de s3-probe is niet gebonden: {probe}"
    assert probe.get("ok") is True, f"de s3-probe is niet groen: {probe}"


@pytest.mark.timeout(900)
def test_de_draaiende_pod_kreeg_het_adres_van_seaweedfs(project_a: CreatedProject) -> None:
    if not cluster.kubectl_available():
        pytest.skip("kubectl niet beschikbaar; deze meting hoort op de sandboxmachine")
    namespace, pod = _draaiende_pod(project_a)
    _, _, bucket = _sleutels(project_a)

    host = cluster.env_in_pod(namespace, pod, "OBJECT_STORE_HOST", probe="rc331-host")
    poort = cluster.env_in_pod(namespace, pod, "OBJECT_STORE_PORT", probe="rc331-poort")
    in_pod_bucket = cluster.env_in_pod(namespace, pod, "OBJECT_STORE_BUCKET_NAME", probe="rc331-bucket")

    assert host == get_seaweedfs_host(_CLUSTER), f"pod kreeg OBJECT_STORE_HOST={host!r}"
    assert poort == str(get_seaweedfs_port(_CLUSTER)), f"pod kreeg OBJECT_STORE_PORT={poort!r}"
    assert in_pod_bucket == bucket, f"pod kreeg bucket {in_pod_bucket!r}, OPI maakte {bucket!r}"
    assert bucket == generate_bucket_name(project_a.name, project_a.deployment_name)


@pytest.mark.timeout(600)
def test_versiebeheer_geeft_de_oudste_versie_terug(project_a: CreatedProject, clientpod: None) -> None:
    """Waar versiebeheer voor bestaat: de oude inhoud byte voor byte terug.

    De wizard zet `enable-versioning` niet aan, dus dit zet hem met de admin aan en meet
    daarna met de PROJECTsleutels, zoals een applicatie het zou doen.
    """
    access_key, secret_key, bucket = _sleutels(project_a)
    seaweedfs.schrijf_sleutels({"a": (access_key, secret_key)})
    aan = seaweedfs.weed_shell(f"s3.bucket.versioning -name {bucket} -enable")
    assert aan.ok, aan.rapport()

    seaweedfs.aws("a", f"echo eerste > /tmp/v1 && aws s3api put-object --bucket {bucket} --key v --body /tmp/v1")
    seaweedfs.aws("a", f"echo tweede > /tmp/v2 && aws s3api put-object --bucket {bucket} --key v --body /tmp/v2")
    versies = seaweedfs.aws("a", f"aws s3api list-object-versions --bucket {bucket} --prefix v")
    assert versies.ok, f"de projectsleutels kunnen de versies niet opsommen: {versies.rapport()}"
    lijst = sorted((versies.json() or {}).get("Versions", []), key=lambda item: item.get("LastModified", ""))
    assert len(lijst) >= 2, f"geen twee versies na twee PutObjects: {versies.rapport()}"

    oudste = lijst[0]["VersionId"]
    terug = seaweedfs.aws(
        "a", f"aws s3api get-object --bucket {bucket} --key v --version-id {oudste} /tmp/terug && cat /tmp/terug"
    )
    assert terug.ok, terug.rapport()
    assert "eerste" in terug.stdout, f"de oudste versie geeft andere inhoud terug: {terug.rapport()}"


@pytest.mark.timeout(900)
def test_de_sleutels_van_a_komen_niet_bij_de_bucket_van_b(
    project_a: CreatedProject, project_b: CreatedProject, clientpod: None
) -> None:
    a_key, a_secret, a_bucket = _sleutels(project_a)
    b_key, b_secret, b_bucket = _sleutels(project_b)
    assert a_bucket != b_bucket
    seaweedfs.schrijf_sleutels({"a": (a_key, a_secret), "b": (b_key, b_secret)})
    seaweedfs.aws("b", f"echo van-b > /tmp/ib && aws s3api put-object --bucket {b_bucket} --key ib --body /tmp/ib")

    pogingen = {
        "A leest de bucket van B": ("a", f"aws s3api list-objects-v2 --bucket {b_bucket}"),
        "A haalt een object van B": ("a", f"aws s3api get-object --bucket {b_bucket} --key ib /tmp/uit"),
        "A schrijft in de bucket van B": (
            "a",
            f"echo van-a > /tmp/ia && aws s3api put-object --bucket {b_bucket} --key ia --body /tmp/ia",
        ),
        "A verwijdert bij B": ("a", f"aws s3api delete-object --bucket {b_bucket} --key ib"),
        "B leest de bucket van A": ("b", f"aws s3api list-objects-v2 --bucket {a_bucket}"),
        "B schrijft in de bucket van A": (
            "b",
            f"echo van-b > /tmp/ib2 && aws s3api put-object --bucket {a_bucket} --key ib2 --body /tmp/ib2",
        ),
    }
    gelukt = {}
    for wat, (profiel, script) in pogingen.items():
        uitvoer = seaweedfs.aws(profiel, script)
        if uitvoer.ok:
            gelukt[wat] = uitvoer.rapport(200)
    assert not gelukt, f"deze aanroepen hadden geweigerd moeten worden: {gelukt}"

    na = seaweedfs.aws("b", f"aws s3api list-objects-v2 --bucket {b_bucket}")
    sleutels = [item["Key"] for item in (na.json() or {}).get("Contents", [])]
    assert sleutels == ["ib"], f"de bucket van B is veranderd: {sleutels}"


@pytest.mark.timeout(900)
def test_de_dienst_uitzetten_ruimt_alles_op(
    project_a: CreatedProject,
    project_b: CreatedProject,
    clientpod: None,
    sandbox_context: BrowserContext,
    sandbox_url: str,
    forgejo: ForgejoClient,
) -> None:
    """Uitzetten is direct, zonder bewaartermijn: dat is de keuze van de proef.

    Deze meting komt als laatste: hij haalt de dienst van A af, dus alles erboven heeft
    hem nog nodig.
    """
    a_key, a_secret, a_bucket = _sleutels(project_a)
    _, _, b_bucket = _sleutels(project_b)
    gebruiker = generate_minio_username(project_a.name, project_a.deployment_name)
    policy = generate_minio_policy_name(project_a.name, project_a.deployment_name)
    seaweedfs.schrijf_sleutels({"a": (a_key, a_secret)})

    page = sandbox_context.new_page()
    try:
        modal = EditModalHelper(page, sandbox_url, project_a.name)
        modal.open_detail_page()
        modal.open_edit_modal("modal-edit-services", "Services beheren")
        assert modal.is_service_selected(_SERVICE), f"'{_SERVICE}' staat niet aan op {project_a.name}"
        modal.toggle_service(_SERVICE)
        # Drie keer meten in plaats van een keer. Deze meting faalde eerder op de bucket,
        # en de oorzaak zat drie stappen eerder: de klik, de opslag en het opruimen zijn
        # elk een eigen aanname. Pas met alle drie wijst het falen naar de juiste stap.
        assert not modal.is_service_selected(_SERVICE), "de klik kwam niet aan op de kaart"
        modal.submit_step()
        modal.advance_to_review()
        modal.confirm_review()
        modal.wait_for_progress_complete(timeout=180000)
    finally:
        page.close()

    assert cluster.wait_for(
        lambda: _SERVICE not in _dienstnamen(forgejo, project_a.name),
        timeout=120,
        interval=5,
    ), f"het projectbestand van {project_a.name} noemt {_SERVICE} nog: {_dienstnamen(forgejo, project_a.name)}"

    assert cluster.wait_for(
        lambda: a_bucket not in seaweedfs.weed_shell("s3.bucket.list").stdout,
        timeout=300,
        interval=5,
    ), f"de bucket {a_bucket} staat er nog na het uitzetten van de dienst"
    assert not seaweedfs.weed_shell(f"s3.user.show -name {gebruiker}").ok, f"de identiteit {gebruiker} bestaat nog"
    assert policy not in seaweedfs.weed_shell("s3.policy -list").stdout, f"de policy {policy} staat er nog"

    met_oude_sleutels = seaweedfs.aws("a", f"aws s3api list-objects-v2 --bucket {a_bucket}")
    assert not met_oude_sleutels.ok, f"de sleutels van A werken nog: {met_oude_sleutels.rapport()}"
    assert b_bucket in seaweedfs.weed_shell("s3.bucket.list").stdout, "de bucket van B is meegegaan"


@pytest.mark.timeout(600)
def test_een_tenant_bereikt_alleen_de_s3_poort(clientpod: None) -> None:
    """De tegenmeting voor de NetworkPolicy: zonder dit zegt de regel niets.

    Uit een namespace met het tenantlabel, zoals OPI er een per project maakt.
    """
    host = get_seaweedfs_host(_CLUSTER)
    assert host is not None
    s3 = seaweedfs.netwerk_bereikt(host, get_seaweedfs_port(_CLUSTER))
    assert "BEREIKT" in s3.stdout, f"een tenant bereikt de S3-poort niet: {s3.rapport()}"

    dicht = {}
    for poort in (9333, 8888, 8080, 19333, 18888, 18080, 8181, 9101):
        uitvoer = seaweedfs.netwerk_bereikt(host, poort)
        if "BEREIKT" in uitvoer.stdout:
            dicht[poort] = uitvoer.rapport(200)
    assert not dicht, f"deze poorten horen voor een tenant dicht te zijn: {dicht}"
