"""De eisentabel van SeaweedFS 4.48, per rij een test, gemeten op de sandbox (RC-331).

Dit is de gate: connector en service zijn pas gebouwd nadat vaststond dat SeaweedFS kan
wat MinIO hier doet. Elke test schrijft zijn uitkomst naar `seaweedfs-eisen.md`, en de
suite blijft daarna staan als regressie: een rij die "werkt" meldt moet rood worden zodra
dat niet meer zo is.

De module is serieel: de rijen bouwen op elkaar. De meting zelf loopt via `kubectl exec`
in de pod, want dat is de kortste weg naar een shell; OPI doet dat niet, dat draait zijn
`weed shell` in de eigen pod (rij G18 meet die weg). Slaat over zonder kubectl of zonder
draaiende seaweedfs-pod.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest
from opi.utils.passwords import generate_secure_password
from tests.e2e.helpers import cluster, seaweedfs

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

logger = logging.getLogger(__name__)

pytestmark = [pytest.mark.e2e, pytest.mark.sandbox, pytest.mark.serial]

#: De identiteiten en buckets van de meting. De namen zijn die van het plan: een underscore
#: in de access key (zoals ``generate_minio_username`` die maakt) en een streepje in de
#: bucketnaam (zoals ``generate_bucket_name``).
GEBRUIKER_A = "t_a"
BUCKET_A = "t-a"
GEBRUIKER_B = "t_b"
BUCKET_B = "t-b"
#: Een bucket zonder eigenaar, voor rij G2.
BUCKET_A_GEEN_EIGENAAR = "t-a-geen-eigenaar"


#: Het policy-document dat `grant_bucket_access` vandaag voor MinIO bouwt, letterlijk. De
#: hele isolatie van deze proef hangt eraan dat SeaweedFS hier hetzelfde van maakt.
def policy_document(bucket: str) -> dict:
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": [
                    "s3:ListBucket",
                    "s3:ListBucketVersions",
                    "s3:GetBucketVersioning",
                    "s3:GetBucketLocation",
                ],
                "Resource": [f"arn:aws:s3:::{bucket}"],
            },
            {
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:GetObjectVersion", "s3:PutObject", "s3:DeleteObject"],
                "Resource": [f"arn:aws:s3:::{bucket}/*"],
            },
        ],
    }


def policy_naam(gebruiker: str, bucket: str) -> str:
    """Zoals ``generate_minio_policy_name`` hem maakt."""
    return f"{gebruiker}-{bucket}-policy"


@dataclass
class Rapport:
    """Het verslag van de meting: per rij de uitkomst en de ruwe uitvoer."""

    pad: Path

    def open(self) -> None:
        """Begint een nieuw verslag, of vult aan: een deelrun mag de eerdere rijen houden."""
        if self.pad.exists():
            return
        self.pad.write_text(
            "# Eisentabel SeaweedFS 4.48 op de sandbox (RC-331)\n\n"
            "Gegenereerd door `tests/e2e/test_sandbox_seaweedfs_eisen.py`.\n",
            encoding="utf-8",
        )

    def rij(self, naam: str, uitkomst: str, ruw: str) -> None:
        with self.pad.open("a", encoding="utf-8") as bestand:
            bestand.write(f"\n## {naam}\n\n**{uitkomst}**\n\n```\n{ruw}\n```\n")
        logger.info("%s: %s", naam, uitkomst)


@pytest.fixture(scope="module")
def rapport(artifact_dir: Path) -> Rapport:
    verslag = Rapport(artifact_dir / "seaweedfs-eisen.md")
    verslag.open()
    return verslag


@pytest.fixture(scope="module")
def server() -> None:
    """Alleen de guard. De PODNAAM wordt per rij opnieuw opgezocht, want G15 vervangt hem."""
    if not cluster.kubectl_available():
        pytest.skip("kubectl niet beschikbaar; deze meting hoort op de sandboxmachine")
    if not cluster.running_pod_names(seaweedfs.NAMESPACE, seaweedfs.SERVER_APP):
        pytest.skip("geen draaiende seaweedfs-pod; rol eerst het infrastructuurcomponent uit")


@pytest.fixture(scope="module")
def admin(server: None) -> tuple[str, str]:
    return seaweedfs.admin_sleutels()


@pytest.fixture(scope="module")
def clientpod(admin: tuple[str, str]) -> Generator[None]:
    """De clientpod en de weed-pod, met de admin-sleutels erin, en opruimen na afloop."""
    seaweedfs.zet_clientpod_neer()
    seaweedfs.zet_weedpod_neer()
    seaweedfs.schrijf_sleutels({"admin": admin})
    yield
    seaweedfs.ruim_op()


@dataclass
class Identiteit:
    naam: str
    bucket: str
    secret_key: str


@pytest.fixture(scope="module")
def identiteiten(clientpod: None, admin: tuple[str, str]) -> list[Identiteit]:
    """Twee identiteiten met elk een eigen bucket, policy en versioning.

    Dit is de stand die de dienst straks per deployment aanmaakt, met de hand gezet zodat
    elke rij eronder iets heeft om tegen te meten.
    """
    gemaakt = []
    for naam, bucket in ((GEBRUIKER_A, BUCKET_A), (GEBRUIKER_B, BUCKET_B)):
        sleutel = generate_secure_password(min_uppercase=3, min_lowercase=3, min_digits=3, total_length=20)
        # Eerst weg: een tweede gang van de meting zou anders een bestaande gebruiker met een
        # ANDER wachtwoord treffen, en dan faalt elke rij eronder op de sleutel.
        seaweedfs.weed_shell(f"s3.user.delete -name {naam}")
        seaweedfs.weed_shell(f"s3.user.create -name {naam} -access_key {naam} -secret_key {sleutel}")
        seaweedfs.weed_shell(f"s3.bucket.create -name {bucket} -owner {naam}")
        seaweedfs.weed_shell(f"s3.bucket.versioning -name {bucket} -enable")
        _zet_policy(naam, bucket)
        gemaakt.append(Identiteit(naam, bucket, sleutel))

    seaweedfs.schrijf_sleutels(
        {
            "admin": admin,
            "a": (GEBRUIKER_A, gemaakt[0].secret_key),
            "b": (GEBRUIKER_B, gemaakt[1].secret_key),
        }
    )
    return gemaakt


def _put_policy(naam: str, bucket: str) -> seaweedfs.Uitvoer:
    """Het document in de pod zetten (via stdin) en opslaan onder ``naam``."""
    pad = f"/tmp/{naam}.json"
    seaweedfs.exec_in(
        seaweedfs.NAMESPACE,
        seaweedfs.server_pod(),
        f"cat > {pad}",
        stdin=json.dumps(policy_document(bucket)),
    )
    return seaweedfs.weed_shell(f"s3.policy -put -name {naam} -file {pad}")


def _zet_policy(gebruiker: str, bucket: str, *, naam: str = "") -> seaweedfs.Uitvoer:
    """Het document opslaan en aan ``gebruiker`` koppelen."""
    naam = naam or policy_naam(gebruiker, bucket)
    _put_policy(naam, bucket)
    return seaweedfs.weed_shell(f"s3.policy.attach -policy {naam} -user {gebruiker}")


# --------------------------------------------------------------------------------------
# Stap 0 en 1: wat er draait, en of het blijft draaien
# --------------------------------------------------------------------------------------


def test_stap0_wat_er_draait(server: None, rapport: Rapport) -> None:
    """Versie, commit en de lijst `s3.*`-commando's van precies deze build."""
    server = seaweedfs.server_pod()
    versie = seaweedfs.exec_in(seaweedfs.NAMESPACE, server, "weed version")
    binary = seaweedfs.exec_in(seaweedfs.NAMESPACE, server, "wc -c < /usr/bin/weed; ldd /usr/bin/weed 2>&1 | head -2")
    hulp = seaweedfs.weed_shell("help")
    s3_commandos = sorted(regel.split()[0] for regel in hulp.tekst.splitlines() if regel.strip().startswith("s3."))
    rapport.rij(
        "Stap 0: wat er draait",
        f"{versie.stdout.splitlines()[0] if versie.stdout else 'geen versie-uitvoer'}",
        f"{versie.rapport()}\n\nbinary:\n{binary.rapport()}\n\ns3-commando's: {s3_commandos}",
    )
    assert "4.48" in versie.stdout, f"andere versie dan 4.48: {versie.rapport()}"
    for nodig in ("s3.bucket.create", "s3.user.create", "s3.policy", "s3.policy.attach"):
        assert any(naam == nodig for naam in s3_commandos), f"{nodig} ontbreekt in deze build: {s3_commandos}"


def test_stap1_pod_is_ready(server: None, rapport: Rapport) -> None:
    """Het component zoals het manifest het neerzet: Ready, non-root, read-only root."""
    server = seaweedfs.server_pod()
    pod = cluster.get_json_strict("get", "pod", server, "-n", seaweedfs.NAMESPACE)
    spec = pod["spec"]
    container = spec["containers"][0]
    identiteit = seaweedfs.exec_in(seaweedfs.NAMESPACE, server, "id -u; id -g; touch /niet-schrijfbaar 2>&1 | head -1")
    rapport.rij(
        "Stap 1: de pod",
        "Ready",
        f"image: {container['image']}\ncommand: {container['command']}\n"
        f"pod-securityContext: {spec.get('securityContext')}\n"
        f"container-securityContext: {container.get('securityContext')}\n\n{identiteit.rapport()}",
    )
    assert container["image"].endswith(seaweedfs.SEAWEEDFS_IMAGE.split("@", 1)[1])
    assert identiteit.stdout.splitlines()[:2] == ["1000", "1000"], identiteit.rapport()


# --------------------------------------------------------------------------------------
# G: wat een deployment van de dienst krijgt
# --------------------------------------------------------------------------------------


def test_g1_bucket_aanmaken_is_herhaalbaar(clientpod: None, rapport: Rapport) -> None:
    seaweedfs.weed_shell("s3.bucket.delete -name t-g1")
    eerste = seaweedfs.weed_shell("s3.bucket.create -name t-g1")
    tweede = seaweedfs.weed_shell("s3.bucket.create -name t-g1")
    bestaat_al = "already exists" in tweede.tekst
    rapport.rij(
        "G1 bucket aanmaken, herhaalbaar",
        "werkt anders: een tweede keer is een FOUT (exit 1) met 'already exists'"
        if bestaat_al and not tweede.ok
        else ("werkt" if tweede.ok else "ontbreekt"),
        f"eerste:\n{eerste.rapport()}\n\ntweede:\n{tweede.rapport()}",
    )
    assert eerste.ok, eerste.rapport()
    assert tweede.ok or bestaat_al, (
        "een tweede aanmaak is niet als 'bestaat al' te herkennen, dus provisioning kan niet "
        f"replay-safe: {tweede.rapport()}"
    )


def test_g2_bucket_met_eigenaar(identiteiten: list[Identiteit], rapport: Rapport) -> None:
    """Maakt `-owner` verschil, of volstaat de policy?"""
    zonder = seaweedfs.weed_shell(f"s3.bucket.create -name {BUCKET_A_GEEN_EIGENAAR}")
    _zet_policy(GEBRUIKER_A, BUCKET_A_GEEN_EIGENAAR)
    met_eigenaar = seaweedfs.aws("a", f"aws s3api list-objects-v2 --bucket {BUCKET_A}")
    zonder_eigenaar = seaweedfs.aws("a", f"aws s3api list-objects-v2 --bucket {BUCKET_A_GEEN_EIGENAAR}")
    rapport.rij(
        "G2 bucket met eigenaar",
        "werkt" if met_eigenaar.ok and zonder_eigenaar.ok else "werkt anders: de eigenaar doet mee",
        f"aanmaken zonder -owner:\n{zonder.rapport()}\n\nmet -owner (t-a):\n{met_eigenaar.rapport()}\n\n"
        f"zonder -owner:\n{zonder_eigenaar.rapport()}",
    )
    assert zonder.ok, f"een bucket zonder -owner is niet aan te maken: {zonder.rapport()}"
    assert met_eigenaar.ok, f"de identiteit komt niet bij zijn eigen bucket: {met_eigenaar.rapport()}"
    assert zonder_eigenaar.ok, (
        f"zonder -owner komt de identiteit er niet bij, dus de eigenaar doet mee: {zonder_eigenaar.rapport()}"
    )


def test_g3_bucket_met_inhoud_verwijderen(identiteiten: list[Identiteit], rapport: Rapport) -> None:
    seaweedfs.weed_shell("s3.bucket.create -name t-g3")
    seaweedfs.weed_shell("s3.bucket.versioning -name t-g3 -enable")
    seaweedfs.aws("admin", "echo een > /tmp/g3 && aws s3api put-object --bucket t-g3 --key k --body /tmp/g3")
    seaweedfs.aws("admin", "echo twee > /tmp/g3b && aws s3api put-object --bucket t-g3 --key k --body /tmp/g3b")
    weg = seaweedfs.weed_shell("s3.bucket.delete -name t-g3")
    lijst = seaweedfs.weed_shell("s3.bucket.list")
    na = seaweedfs.aws("admin", "aws s3api list-object-versions --bucket t-g3")
    rapport.rij(
        "G3 bucket met inhoud verwijderen",
        "werkt" if weg.ok and "t-g3" not in lijst.stdout and not na.ok else "werkt anders",
        f"delete:\n{weg.rapport()}\n\nbucket.list:\n{lijst.rapport()}\n\nversies na afloop:\n{na.rapport()}",
    )
    assert weg.ok, weg.rapport()
    assert "t-g3" not in lijst.stdout, f"bucket staat er nog: {lijst.rapport()}"
    assert not na.ok, f"de versies van een verwijderde bucket zijn nog op te vragen: {na.rapport()}"


def test_g4_buckets_opsommen(identiteiten: list[Identiteit], rapport: Rapport) -> None:
    lijst = seaweedfs.weed_shell("s3.bucket.list")
    namen = [regel.split("\t")[0].strip() for regel in lijst.stdout.splitlines() if regel.strip()]
    rapport.rij(
        "G4 buckets opsommen",
        "werkt anders: geen JSON, wel tab-gescheiden regels die per bucket beginnen met de naam",
        f"{lijst.rapport()}\n\ngeparseerde namen: {namen}",
    )
    assert BUCKET_A in namen, f"bucket A staat niet in de lijst: {namen}"
    assert BUCKET_B in namen, f"bucket B staat niet in de lijst: {namen}"


def test_g5_versioning_aanzetten_en_lezen(identiteiten: list[Identiteit], rapport: Rapport) -> None:
    aan = seaweedfs.weed_shell(f"s3.bucket.versioning -name {BUCKET_A} -enable")
    status = seaweedfs.weed_shell(f"s3.bucket.versioning -name {BUCKET_A}")
    via_s3 = seaweedfs.aws("a", f"aws s3api get-bucket-versioning --bucket {BUCKET_A}")
    rapport.rij(
        "G5 versioning aanzetten en de status lezen",
        "werkt" if "Enabled" in status.stdout and "Enabled" in via_s3.stdout else "werkt anders",
        f"enable:\n{aan.rapport()}\n\nstatus:\n{status.rapport()}\n\nGetBucketVersioning:\n{via_s3.rapport()}",
    )
    assert "Enabled" in status.stdout, status.rapport()
    assert "Enabled" in via_s3.stdout, (
        f"GetBucketVersioning met de projectsleutels zegt geen Enabled: {via_s3.rapport()}"
    )


def test_g6_versioning_suspenden(identiteiten: list[Identiteit], rapport: Rapport) -> None:
    seaweedfs.weed_shell("s3.bucket.create -name t-g6")
    seaweedfs.weed_shell("s3.bucket.versioning -name t-g6 -enable")
    uit = seaweedfs.weed_shell("s3.bucket.versioning -name t-g6 -suspend")
    status = seaweedfs.weed_shell("s3.bucket.versioning -name t-g6")
    seaweedfs.aws("admin", "echo een > /tmp/g6a && aws s3api put-object --bucket t-g6 --key k --body /tmp/g6a")
    seaweedfs.aws("admin", "echo twee > /tmp/g6b && aws s3api put-object --bucket t-g6 --key k --body /tmp/g6b")
    versies = seaweedfs.aws("admin", "aws s3api list-object-versions --bucket t-g6")
    aantal = len((versies.json() or {}).get("Versions", [])) if versies.ok else -1
    rapport.rij(
        "G6 versioning suspenden",
        "werkt" if "Suspended" in status.stdout and aantal == 1 else f"werkt anders: {aantal} versies na suspend",
        f"suspend:\n{uit.rapport()}\n\nstatus:\n{status.rapport()}\n\nversies: {aantal}\n{versies.rapport()}",
    )
    assert "Suspended" in status.stdout, status.rapport()
    assert aantal == 1, f"na suspend schrijft een PutObject toch een nieuwe versie: {aantal} versies"


def test_g7_oude_versie_terughalen(identiteiten: list[Identiteit], rapport: Rapport) -> None:
    """De rij waar versiebeheer voor bestaat: de oude inhoud byte voor byte terug."""
    seaweedfs.aws("a", f"echo eerste > /tmp/g7a && aws s3api put-object --bucket {BUCKET_A} --key g7 --body /tmp/g7a")
    seaweedfs.aws("a", f"echo tweede > /tmp/g7b && aws s3api put-object --bucket {BUCKET_A} --key g7 --body /tmp/g7b")
    met_projectsleutels = seaweedfs.aws("a", f"aws s3api list-object-versions --bucket {BUCKET_A} --prefix g7")
    # Met de admin erbij, zodat "A mag niet opsommen" en "er zijn geen versies" niet op
    # elkaar lijken.
    met_admin = seaweedfs.aws("admin", f"aws s3api list-object-versions --bucket {BUCKET_A} --prefix g7")
    lijst = sorted(
        (met_admin.json() or {}).get("Versions", []) if met_admin.ok else [],
        key=lambda item: item.get("LastModified", ""),
    )
    oudste = lijst[0]["VersionId"] if lijst else ""
    leeg = seaweedfs.Uitvoer("", "geen versie-id om op te halen", 1)
    terug = (
        seaweedfs.aws(
            "a",
            f"aws s3api get-object --bucket {BUCKET_A} --key g7 --version-id {oudste} /tmp/g7-terug >/dev/null "
            "&& cat /tmp/g7-terug",
        )
        if oudste
        else leeg
    )
    seaweedfs.aws("a", f"aws s3api delete-object --bucket {BUCKET_A} --key g7")
    na_delete = (
        seaweedfs.aws(
            "a",
            f"aws s3api get-object --bucket {BUCKET_A} --key g7 --version-id {oudste} /tmp/g7-na >/dev/null "
            "&& cat /tmp/g7-na",
        )
        if oudste
        else leeg
    )
    rapport.rij(
        "G7 een oude versie terughalen",
        "werkt"
        if met_projectsleutels.ok and terug.stdout.strip() == "eerste" and na_delete.stdout.strip() == "eerste"
        else "werkt anders: zie de uitvoer",
        f"ListObjectVersions met de projectsleutels:\n{met_projectsleutels.rapport(400)}\n\n"
        f"ListObjectVersions met de admin:\n{met_admin.rapport(600)}\n\n"
        f"versies: {[item.get('VersionId') for item in lijst]}\n\nGetObject met versie-id:\n{terug.rapport()}\n\n"
        f"na een delete zonder versie:\n{na_delete.rapport()}",
    )
    assert len(lijst) >= 2, f"geen twee versies van dezelfde key: {met_admin.rapport()}"
    assert terug.stdout.strip() == "eerste", f"de oude versie komt niet terug: {terug.rapport()}"
    assert na_delete.stdout.strip() == "eerste", (
        f"na een delete zonder versie is de oude versie niet meer op te vragen: {na_delete.rapport()}"
    )
    # Deze assert is de guard op het besluit van 18-10-2026: haalt iemand
    # `s3:ListBucketVersions` uit het gedeelde document, dan komt G7 hier rood terug.
    assert met_projectsleutels.ok, (
        f"de identiteit mag zijn eigen versies niet opsommen: {met_projectsleutels.rapport()}"
    )


def test_g8_gebruiker_met_eigen_access_key(identiteiten: list[Identiteit], rapport: Rapport) -> None:
    """De access key IS de naam, met een underscore erin, en het wachtwoord kiezen wij."""
    sleutel = generate_secure_password(min_uppercase=3, min_lowercase=3, min_digits=3, total_length=20)
    seaweedfs.weed_shell("s3.user.delete -name t_g8")
    gemaakt = seaweedfs.weed_shell(f"s3.user.create -name t_g8 -access_key t_g8 -secret_key {sleutel}")
    seaweedfs.schrijf_sleutels({"g8": ("t_g8", sleutel)})
    werkt = seaweedfs.aws("g8", "aws s3api list-buckets")
    rapport.rij(
        "G8 gebruiker met een access key die wij kiezen",
        "werkt" if gemaakt.ok else "ontbreekt",
        f"create:\n{gemaakt.rapport()}\n\n"
        f"de sleutel doet iets (ListBuckets, mag weigeren maar moet authenticeren):\n{werkt.rapport()}\n\n"
        "wachtwoord uit generate_secure_password: alfanumeriek, 20 tekens, dus geen teken dat "
        "een shell of een URL anders leest",
    )
    assert gemaakt.ok, gemaakt.rapport()
    assert "InvalidAccessKeyId" not in werkt.tekst, f"de door ons gekozen sleutel is onbekend: {werkt.rapport()}"
    assert "SignatureDoesNotMatch" not in werkt.tekst, f"het door ons gekozen wachtwoord klopt niet: {werkt.rapport()}"


def test_g9_bestaat_de_gebruiker(identiteiten: list[Identiteit], rapport: Rapport) -> None:
    bestaat = seaweedfs.weed_shell(f"s3.user.show -name {GEBRUIKER_A}")
    bestaat_niet = seaweedfs.weed_shell("s3.user.show -name t_bestaat_niet")
    lijst = seaweedfs.weed_shell("s3.user.list")
    rapport.rij(
        "G9 bestaat de gebruiker; lijst van gebruikers",
        "werkt" if bestaat.ok and not bestaat_niet.ok else "werkt anders",
        f"bestaand:\n{bestaat.rapport()}\n\nniet-bestaand:\n{bestaat_niet.rapport()}\n\nlijst:\n{lijst.rapport()}",
    )
    assert bestaat.ok, bestaat.rapport()
    assert not bestaat_niet.ok, f"een onbekende gebruiker geeft geen fout: {bestaat_niet.rapport()}"
    assert GEBRUIKER_A in lijst.stdout, lijst.rapport()


def test_g10_wachtwoord_vervangen(identiteiten: list[Identiteit], rapport: Rapport) -> None:
    """Verwijderen en opnieuw aanmaken, zoals `_validate_and_fix_credentials` doet."""
    oud = generate_secure_password(min_uppercase=3, min_lowercase=3, min_digits=3, total_length=20)
    nieuw = generate_secure_password(min_uppercase=3, min_lowercase=3, min_digits=3, total_length=20)
    seaweedfs.weed_shell("s3.user.delete -name t_g10")
    seaweedfs.weed_shell(f"s3.user.create -name t_g10 -access_key t_g10 -secret_key {oud}")
    _zet_policy("t_g10", BUCKET_A)
    seaweedfs.weed_shell("s3.user.delete -name t_g10")
    seaweedfs.weed_shell(f"s3.user.create -name t_g10 -access_key t_g10 -secret_key {nieuw}")
    na_create = seaweedfs.weed_shell("s3.user.show -name t_g10")
    seaweedfs.schrijf_sleutels({"g10oud": ("t_g10", oud), "g10nieuw": ("t_g10", nieuw)})
    met_oud = seaweedfs.aws("g10oud", f"aws s3api list-objects-v2 --bucket {BUCKET_A}")
    met_nieuw = seaweedfs.aws("g10nieuw", f"aws s3api list-objects-v2 --bucket {BUCKET_A}")
    koppeling_bleef = policy_naam("t_g10", BUCKET_A) in na_create.stdout
    # De manager koppelt na het opnieuw aanmaken altijd opnieuw; dat die weg werkt is wat
    # `_validate_and_fix_credentials` bij SeaweedFS nodig heeft.
    _zet_policy("t_g10", BUCKET_A)
    na_opnieuw_koppelen = seaweedfs.aws("g10nieuw", f"aws s3api list-objects-v2 --bucket {BUCKET_A}")
    rapport.rij(
        "G10 wachtwoord vervangen door verwijderen en opnieuw aanmaken",
        f"werkt; de policy-koppeling {'blijft staan' if koppeling_bleef else 'is opnieuw nodig'} na het opnieuw "
        "aanmaken",
        f"user.show na opnieuw aanmaken:\n{na_create.rapport()}\n\nmet de oude sleutel:\n{met_oud.rapport()}\n\n"
        f"met de nieuwe sleutel:\n{met_nieuw.rapport()}\n\n"
        f"met de nieuwe sleutel na opnieuw koppelen:\n{na_opnieuw_koppelen.rapport()}",
    )
    assert not met_oud.ok, f"de oude sleutel werkt nog: {met_oud.rapport()}"
    assert met_nieuw.ok == koppeling_bleef, (
        "de uitkomst over de policy-koppeling klopt niet met wat de nieuwe sleutel kan: "
        f"user.show zegt {koppeling_bleef}, de aanroep {met_nieuw.rapport()}"
    )
    assert na_opnieuw_koppelen.ok, (
        f"na opnieuw koppelen komt de nieuwe sleutel er niet bij: {na_opnieuw_koppelen.rapport()}"
    )


def test_g11_policy_maken_koppelen_ontkoppelen(identiteiten: list[Identiteit], rapport: Rapport) -> None:
    """Het document van `grant_bucket_access`, en wat SeaweedFS ervan overhoudt."""
    naam = policy_naam(GEBRUIKER_A, BUCKET_A)
    opgeslagen = seaweedfs.weed_shell(f"s3.policy -get -name {naam}")
    gelezen = json.loads(opgeslagen.stdout) if opgeslagen.ok and opgeslagen.stdout.startswith("{") else {}
    los = seaweedfs.weed_shell(f"s3.policy.detach -policy {naam} -user {GEBRUIKER_A}")
    na_detach = seaweedfs.aws("a", f"aws s3api list-objects-v2 --bucket {BUCKET_A}")
    weer_vast = _zet_policy(GEBRUIKER_A, BUCKET_A)
    na_attach = seaweedfs.aws("a", f"aws s3api list-objects-v2 --bucket {BUCKET_A}")
    # Verwijderen meet op een eigen policy, niet op die van A: die heeft de rest van de
    # meting nog nodig. Stap 6 geeft de manager CleanupStrategy.IMMEDIATE en leunt op deze
    # handeling, dus hij hoort gemeten en geassert te zijn.
    wegwerp = policy_naam("t_g11", BUCKET_A)
    _put_policy(wegwerp, BUCKET_A)
    weg = seaweedfs.weed_shell(f"s3.policy -delete -name {wegwerp}")
    na_verwijderen = seaweedfs.weed_shell(f"s3.policy -get -name {wegwerp}")
    acties = [actie for statement in gelezen.get("Statement", []) for actie in (statement.get("Action") or [])]
    rapport.rij(
        "G11 policy maken, koppelen, ontkoppelen, verwijderen",
        "werkt" if opgeslagen.ok and los.ok and weer_vast.ok and not na_detach.ok and na_attach.ok else "werkt anders",
        f"opgeslagen document:\n{opgeslagen.rapport()}\n\nacties die terugkomen: {acties}\n\n"
        f"detach:\n{los.rapport()}\n\nna detach (hoort te weigeren):\n{na_detach.rapport()}\n\n"
        f"na attach:\n{na_attach.rapport()}\n\nverwijderen van {wegwerp}:\n{weg.rapport()}\n\n"
        f"opvragen na verwijderen (hoort te falen):\n{na_verwijderen.rapport()}",
    )
    assert opgeslagen.ok, f"het document is niet terug te lezen: {opgeslagen.rapport()}"
    assert sorted(acties) == sorted(
        actie for statement in policy_document(BUCKET_A)["Statement"] for actie in statement["Action"]
    ), f"SeaweedFS houdt een ander document over dan wij erin zetten: {acties}"
    assert not na_detach.ok, f"na detach komt de identiteit er nog bij: {na_detach.rapport()}"
    assert weer_vast.ok, f"opnieuw koppelen lukt niet: {weer_vast.rapport()}"
    assert na_attach.ok, f"na opnieuw koppelen komt de identiteit er niet bij: {na_attach.rapport()}"
    assert weg.ok, f"een policy is niet te verwijderen: {weg.rapport()}"
    assert not na_verwijderen.ok or wegwerp not in na_verwijderen.stdout, (
        f"de policy staat er na verwijderen nog: {na_verwijderen.rapport()}"
    )


def test_g12_alles_wat_het_document_toestaat(identiteiten: list[Identiteit], rapport: Rapport) -> None:
    """Elke handeling die de policy toestaat, met de sleutels van de identiteit zelf."""
    metingen = {
        "ListObjectsV2": f"aws s3api list-objects-v2 --bucket {BUCKET_A}",
        "GetBucketLocation": f"aws s3api get-bucket-location --bucket {BUCKET_A}",
        "GetBucketVersioning": f"aws s3api get-bucket-versioning --bucket {BUCKET_A}",
        "PutObject": f"echo klein > /tmp/g12 && aws s3api put-object --bucket {BUCKET_A} --key g12 --body /tmp/g12",
        "GetObject": f"aws s3api get-object --bucket {BUCKET_A} --key g12 /tmp/g12-terug",
        "DeleteObject": f"aws s3api delete-object --bucket {BUCKET_A} --key g12",
        "CopyObject": (
            f"echo bron > /tmp/g12c && aws s3api put-object --bucket {BUCKET_A} --key g12c --body /tmp/g12c "
            f"&& aws s3api copy-object --bucket {BUCKET_A} --key g12c-kopie --copy-source {BUCKET_A}/g12c"
        ),
        "multipart 24 MiB": (
            f"dd if=/dev/urandom of=/tmp/g12-groot bs=1M count=24 2>/dev/null "
            f"&& aws s3 cp /tmp/g12-groot s3://{BUCKET_A}/g12-groot"
        ),
    }
    uitkomsten = {naam: seaweedfs.aws("a", script, timeout=300.0) for naam, script in metingen.items()}
    presigned = seaweedfs.aws("a", f"aws s3 presign s3://{BUCKET_A}/g12c")
    opgehaald = seaweedfs.mc(f"curl -s -o /dev/null -w '%{{http_code}}' '{presigned.stdout.strip()}'")
    mislukt = [naam for naam, uitvoer in uitkomsten.items() if not uitvoer.ok]
    rapport.rij(
        "G12 de eigen bucket: alles wat het document toestaat",
        "werkt" if not mislukt and opgehaald.stdout.strip() == "200" else f"werkt anders: {mislukt} mislukte(n)",
        "\n\n".join(f"{naam}:\n{uitvoer.rapport(300)}" for naam, uitvoer in uitkomsten.items())
        + f"\n\npresigned GET: HTTP {opgehaald.stdout.strip()}",
    )
    assert not mislukt, f"deze handelingen mislukten terwijl de policy ze toestaat: {mislukt}"


def test_g13_sigv4_path_style_en_regio(identiteiten: list[Identiteit], rapport: Rapport) -> None:
    """De vorm waarin MinIOSecret een applicatie de opslag geeft: SigV4, us-east-1, path-style, http.

    De tweede helft van de rij (minio-go) loopt hier via `mc`: dat IS een minio-go-client, en
    hij praat in deze meting met dezelfde sleutels tegen hetzelfde adres. De meting met het
    `e2e-allservices`-image hoort bij stap 9 van het plan, waar de dienst de variabelen zelf
    injecteert.
    """
    met_aws = seaweedfs.aws("a", f"aws s3api list-objects-v2 --bucket {BUCKET_A}")
    met_mc = seaweedfs.mc(f"mc ls a/{BUCKET_A}")
    rapport.rij(
        "G13 SigV4 met regio us-east-1, path-style, http",
        "werkt" if met_aws.ok and met_mc.ok else "werkt anders",
        f"aws (AWS_ENDPOINT_URL={seaweedfs.S3_ENDPOINT}, regio us-east-1, addressing_style=path):\n"
        f"{met_aws.rapport()}\n\nmc (minio-go):\n{met_mc.rapport()}",
    )
    assert met_aws.ok, met_aws.rapport()
    assert met_mc.ok, f"een minio-go-client komt er niet bij: {met_mc.rapport()}"


def test_g14_geen_anonieme_toegang(identiteiten: list[Identiteit], rapport: Rapport) -> None:
    lijst = seaweedfs.aws_anoniem("aws s3api list-buckets --no-sign-request")
    schrijf = seaweedfs.aws_anoniem(
        f"echo x > /tmp/g14 && aws s3api put-object --no-sign-request --bucket {BUCKET_A} --key g14 --body /tmp/g14"
    )
    lees = seaweedfs.aws_anoniem(f"aws s3api get-object --no-sign-request --bucket {BUCKET_A} --key g12c /tmp/g14-uit")
    rapport.rij(
        "G14 zonder identiteit geen anonieme toegang",
        "werkt" if not (lijst.ok or schrijf.ok or lees.ok) else "ONTBREEKT: anoniem komt er door",
        f"ListBuckets:\n{lijst.rapport(200)}\n\nPutObject:\n{schrijf.rapport(200)}\n\nGetObject:\n{lees.rapport(200)}",
    )
    assert not schrijf.ok, f"anoniem schrijven lukt: {schrijf.rapport()}"
    assert not lees.ok, f"anoniem lezen lukt: {lees.rapport()}"
    assert not lijst.ok, f"anoniem opsommen lukt: {lijst.rapport()}"


def test_g15_data_blijft_na_een_herstart(identiteiten: list[Identiteit], rapport: Rapport) -> None:
    """Object, versie, gebruikers en policies moeten de pod overleven."""
    seaweedfs.aws("a", f"echo blijf > /tmp/g15 && aws s3api put-object --bucket {BUCKET_A} --key g15 --body /tmp/g15")
    oude_pod = seaweedfs.server_pod()
    seaweedfs.verwijder(["pod", oude_pod, "-n", seaweedfs.NAMESPACE])
    assert cluster.wait_for(
        lambda: bool(
            [naam for naam in cluster.running_pod_names(seaweedfs.NAMESPACE, seaweedfs.SERVER_APP) if naam != oude_pod]
        ),
        timeout=300,
        interval=5,
    ), "er kwam geen nieuwe seaweedfs-pod"
    assert seaweedfs.wacht_op_server(), "de nieuwe pod kwam niet Ready"
    object_terug = seaweedfs.aws(
        "a", f"aws s3api get-object --bucket {BUCKET_A} --key g15 /tmp/g15-terug >/dev/null && cat /tmp/g15-terug"
    )
    versies = seaweedfs.aws("a", f"aws s3api list-object-versions --bucket {BUCKET_A} --prefix g7")
    gebruikers = seaweedfs.weed_shell("s3.user.list")
    policies = seaweedfs.weed_shell("s3.policy -list")
    rapport.rij(
        "G15 data blijft na een herstart",
        "werkt" if object_terug.stdout.strip() == "blijf" and GEBRUIKER_A in gebruikers.stdout else "werkt anders",
        f"object:\n{object_terug.rapport()}\n\nversies:\n{versies.rapport(300)}\n\n"
        f"gebruikers:\n{gebruikers.rapport(300)}\n\npolicies:\n{policies.rapport(300)}",
    )
    assert object_terug.stdout.strip() == "blijf", object_terug.rapport()
    assert GEBRUIKER_A in gebruikers.stdout, f"de gebruikers zijn weg na een herstart: {gebruikers.rapport()}"
    assert policy_naam(GEBRUIKER_A, BUCKET_A) in policies.stdout, (
        f"de policies zijn weg na een herstart: {policies.rapport()}"
    )


def test_g16_health_endpoints(server: None, rapport: Rapport) -> None:
    """Welk pad zegt 'levend' en welk 'klaar voor S3'."""
    server = seaweedfs.server_pod()
    metingen = {
        "8333/healthz": "http://127.0.0.1:8333/healthz",
        "8333/status": "http://127.0.0.1:8333/status",
        "9333/cluster/healthz": "http://127.0.0.1:9333/cluster/healthz",
    }
    uitkomsten = {
        naam: seaweedfs.exec_in(
            seaweedfs.NAMESPACE,
            server,
            f"wget -q -T 5 -S -O /dev/null {url} 2>&1 | head -2",
        )
        for naam, url in metingen.items()
    }
    luisteraars = seaweedfs.exec_in(seaweedfs.NAMESPACE, server, "netstat -ltn | tail -n +3")
    pod = cluster.get_json_strict("get", "pod", server, "-n", seaweedfs.NAMESPACE)
    probes = pod["spec"]["containers"][0]
    rapport.rij(
        "G16 health-endpoints voor liveness en readiness",
        "werkt: /healthz op de S3-poort antwoordt, en die poort bestaat pas als de gateway er is",
        "\n\n".join(f"{naam}:\n{uitvoer.rapport(200)}" for naam, uitvoer in uitkomsten.items())
        + f"\n\nliveness: {probes.get('livenessProbe')}\nreadiness: {probes.get('readinessProbe')}"
        + f"\n\nlisteners:\n{luisteraars.rapport(600)}",
    )
    assert "200" in uitkomsten["8333/healthz"].tekst, uitkomsten["8333/healthz"].rapport()


def test_g17_pod_hardening(server: None, rapport: Rapport) -> None:
    """Dezelfde hardening als minio, plus een read-only root."""
    server = seaweedfs.server_pod()
    pod = cluster.get_json_strict("get", "pod", server, "-n", seaweedfs.NAMESPACE)
    spec = pod["spec"]
    container = spec["containers"][0]
    beveiliging = container["securityContext"]
    schrijfpoging = seaweedfs.exec_in(
        seaweedfs.NAMESPACE, server, "touch /mag-niet 2>&1 | head -1; touch /tmp/mag-wel && echo TMP-OK"
    )
    rapport.rij(
        "G17 pod-hardening als bij minio",
        "werkt",
        f"pod: {spec.get('securityContext')}\ncontainer: {beveiliging}\n"
        f"automountServiceAccountToken: {spec.get('automountServiceAccountToken')}\n\n{schrijfpoging.rapport()}",
    )
    assert spec["securityContext"]["runAsNonRoot"] is True
    assert spec["securityContext"]["runAsUser"] == 1000
    assert spec.get("automountServiceAccountToken") is False
    assert beveiliging["allowPrivilegeEscalation"] is False
    assert beveiliging["capabilities"]["drop"] == ["ALL"]
    assert beveiliging["readOnlyRootFilesystem"] is True
    assert beveiliging["seccompProfile"]["type"] == "RuntimeDefault"
    assert "TMP-OK" in schrijfpoging.stdout, f"/tmp is niet schrijfbaar: {schrijfpoging.rapport()}"


def test_g18_beheer_vanuit_de_opi_pod(identiteiten: list[Identiteit], rapport: Rapport) -> None:
    """De echte beheerweg: de connector in de OPI-pod, met de `weed`-binary uit dat image.

    Dit is de code die provisioning draait, niet een nabootsing ervan: hetzelfde
    `SeaweedfsConnector`-object, met de settings en de JWT-sleutel van de draaiende pod.
    De tegenmeting erbij: een pod in dezelfde namespace zonder het OPI-label komt niet bij
    de gRPC-poorten, want die grens is de NetworkPolicy.
    """
    binnen_de_pod = seaweedfs.weed_shell("s3.user.list")
    vanuit_opi = seaweedfs.connector_in_opi_pod("list_buckets()")
    host = seaweedfs.FILER_SERVICE.split(":")[0]
    poorten = {
        pod: {
            poort: seaweedfs.poort_bereikt_uit(seaweedfs.NAMESPACE, pod, host, poort) for poort in (8333, 18888, 19333)
        }
        for pod in (seaweedfs.opi_pod(), seaweedfs.WEED_POD)
    }
    bereikt = {pod: [p for p, uitvoer in rij.items() if "BEREIKT" in uitvoer.stdout] for pod, rij in poorten.items()}
    opi_mag = bereikt[seaweedfs.opi_pod()]
    vreemde_mag = bereikt[seaweedfs.WEED_POD]
    rapport.rij(
        "G18 beheer vanuit de OPI-pod",
        f"werkt: de connector in de OPI-pod somt {BUCKET_A} op, en alleen die pod komt bij de gRPC-poorten"
        if vanuit_opi.ok and BUCKET_A in vanuit_opi.stdout and 18888 in opi_mag and 18888 not in vreemde_mag
        else "werkt anders: zie de uitvoer",
        f"SeaweedfsConnector().list_buckets() in de OPI-pod:\n{vanuit_opi.rapport(600)}\n\n"
        f"s3.user.list in de SeaweedFS-pod zelf:\n{binnen_de_pod.rapport(300)}\n\n"
        f"poorten vanuit de OPI-pod: {opi_mag}\n"
        f"poorten vanuit een pod in rig-system ZONDER het OPI-label: {vreemde_mag}\n\n"
        "De connector draait `weed shell` als subprocess in de OPI-pod, met de binary uit het "
        "OPI-image: OPI krijgt daarmee geen pods/exec, en de grens op de gRPC-poorten is de "
        "NetworkPolicy plus de JWT op de filer. Zonder -master blijft `weed shell` in "
        "WaitUntilConnected hangen in plaats van een fout te geven, dus de connector geeft "
        "beide adressen mee.",
    )
    assert binnen_de_pod.ok, f"`weed shell` in de pod zelf lukt niet: {binnen_de_pod.rapport()}"
    assert GEBRUIKER_A in binnen_de_pod.stdout, binnen_de_pod.rapport()
    assert vanuit_opi.ok, f"de connector in de OPI-pod faalt: {vanuit_opi.rapport()}"
    assert BUCKET_A in vanuit_opi.stdout, vanuit_opi.rapport()
    assert 18888 in opi_mag, f"de OPI-pod komt niet bij de filer-gRPC: {poorten[seaweedfs.opi_pod()][18888].rapport()}"
    assert 19333 in opi_mag, f"de OPI-pod komt niet bij de master-gRPC: {poorten[seaweedfs.opi_pod()][19333].rapport()}"
    grpc_voor_vreemden = [poort for poort in (18888, 19333) if poort in vreemde_mag]
    assert not grpc_voor_vreemden, f"een pod zonder het OPI-label komt bij de gRPC-poorten: {grpc_voor_vreemden}"


def test_g19_een_fout_is_als_fout_herkenbaar(identiteiten: list[Identiteit], rapport: Rapport) -> None:
    """Zonder dit kan de connector een mislukking niet van een succes onderscheiden."""
    foute_aanroepen = {
        "bucket.create met een ongeldige naam": "s3.bucket.create -name Ongeldige_Naam",
        "bucket.delete van een onbekende bucket": "s3.bucket.delete -name t-bestaat-niet",
        "user.delete van een onbekende gebruiker": "s3.user.delete -name t_bestaat_niet",
        "policy.attach op een onbekende policy": f"s3.policy.attach -policy t-bestaat-niet -user {GEBRUIKER_A}",
        "versioning op een onbekende bucket": "s3.bucket.versioning -name t-bestaat-niet -enable",
        "onbekend commando": "s3.bestaat.niet",
    }
    uitkomsten = {naam: seaweedfs.weed_shell(cmd) for naam, cmd in foute_aanroepen.items()}
    stil_geslaagd = [naam for naam, uitvoer in uitkomsten.items() if uitvoer.ok]
    rapport.rij(
        "G19 een fout van weed shell is als fout herkenbaar",
        "werkt: exit 1 en 'error:' op stderr" if not stil_geslaagd else f"werkt anders: {stil_geslaagd} geven exit 0",
        "\n\n".join(f"{naam}:\n{uitvoer.rapport(200)}" for naam, uitvoer in uitkomsten.items()),
    )
    assert not uitkomsten["onbekend commando"].ok, "een onbekend commando geeft exit 0"
    assert not stil_geslaagd, (
        f"deze aanroepen falen stil, dus de connector kan ze niet van een succes onderscheiden: {stil_geslaagd}"
    )


# --------------------------------------------------------------------------------------
# I: isolatie
# --------------------------------------------------------------------------------------


def test_i1_identiteit_a_kan_niet_bij_bucket_b(identiteiten: list[Identiteit], rapport: Rapport) -> None:
    seaweedfs.aws("b", f"echo van-b > /tmp/i1 && aws s3api put-object --bucket {BUCKET_B} --key i1 --body /tmp/i1")
    pogingen = {
        "ListObjectsV2": f"aws s3api list-objects-v2 --bucket {BUCKET_B}",
        "GetObject": f"aws s3api get-object --bucket {BUCKET_B} --key i1 /tmp/i1-uit",
        "PutObject": f"echo van-a > /tmp/i1a && aws s3api put-object --bucket {BUCKET_B} --key i1a --body /tmp/i1a",
        "DeleteObject": f"aws s3api delete-object --bucket {BUCKET_B} --key i1",
        "GetBucketVersioning": f"aws s3api get-bucket-versioning --bucket {BUCKET_B}",
        "ListObjectVersions": f"aws s3api list-object-versions --bucket {BUCKET_B}",
        "CopyObject B naar A": (
            f"aws s3api copy-object --bucket {BUCKET_A} --key gestolen --copy-source {BUCKET_B}/i1"
        ),
        "CopyObject A naar B": (
            f"aws s3api copy-object --bucket {BUCKET_B} --key geduwd --copy-source {BUCKET_A}/g12c"
        ),
    }
    uitkomsten = {naam: seaweedfs.aws("a", script) for naam, script in pogingen.items()}
    doorgelaten = [naam for naam, uitvoer in uitkomsten.items() if uitvoer.ok]
    na_afloop = seaweedfs.aws("b", f"aws s3api list-objects-v2 --bucket {BUCKET_B}")
    sleutels = [item["Key"] for item in (na_afloop.json() or {}).get("Contents", [])] if na_afloop.ok else []
    rapport.rij(
        "I1 identiteit A kan niet bij bucket B",
        "werkt" if not doorgelaten else f"ONTBREEKT: {doorgelaten} komen door",
        "\n\n".join(f"{naam}:\n{uitvoer.rapport(200)}" for naam, uitvoer in uitkomsten.items())
        + f"\n\nbucket B na afloop: {sleutels}",
    )
    assert not doorgelaten, f"identiteit A komt bij bucket B via: {doorgelaten}"
    assert sleutels == ["i1"], f"bucket B is gewijzigd: {sleutels}"


def test_i2_identiteit_a_ziet_de_andere_buckets_niet(identiteiten: list[Identiteit], rapport: Rapport) -> None:
    lijst = seaweedfs.aws("a", "aws s3api list-buckets")
    namen = [item["Name"] for item in (lijst.json() or {}).get("Buckets", [])] if lijst.ok else []
    rapport.rij(
        "I2 identiteit A ziet de andere buckets niet",
        "werkt: geweigerd"
        if not lijst.ok
        else (f"werkt anders: ListBuckets geeft {namen}" if namen == [BUCKET_A] else f"ONTBREEKT: {namen}"),
        f"{lijst.rapport()}\n\ngenoemde buckets: {namen}",
    )
    assert BUCKET_B not in namen, f"identiteit A ziet bucket B: {namen}"


def test_i3_identiteit_a_kan_zichzelf_geen_rechten_geven(identiteiten: list[Identiteit], rapport: Rapport) -> None:
    pogingen = {
        "CreateBucket": "aws s3api create-bucket --bucket t-c",
        "iam create-access-key": "aws iam create-access-key --user-name t_a",
        "iam create-user": "aws iam create-user --user-name t_nieuw",
        "iam put-user-policy": (
            'echo \'{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":"s3:*",'
            '"Resource":"*"}]}\' > /tmp/i3.json && aws iam put-user-policy --user-name t_a '
            "--policy-name alles --policy-document file:///tmp/i3.json"
        ),
        "iam attach-user-policy": (
            "aws iam attach-user-policy --user-name t_a --policy-arn arn:aws:iam::aws:policy/AdministratorAccess"
        ),
    }
    uitkomsten = {naam: seaweedfs.aws("a", script) for naam, script in pogingen.items()}
    doorgelaten = [naam for naam, uitvoer in uitkomsten.items() if uitvoer.ok]
    rapport.rij(
        "I3 identiteit A kan zichzelf geen rechten geven",
        "werkt" if not doorgelaten else f"ONTBREEKT: {doorgelaten} lukken",
        "\n\n".join(f"{naam}:\n{uitvoer.rapport(200)}" for naam, uitvoer in uitkomsten.items()),
    )
    assert not doorgelaten, f"identiteit A kan dit wel: {doorgelaten}"


def test_i4_alleen_de_s3_poort_is_voor_tenants_bereikbaar(identiteiten: list[Identiteit], rapport: Rapport) -> None:
    """Eerst of dit cluster NetworkPolicies handhaaft, dan wat de poortmeting betekent."""
    host = "seaweedfs.rig-system.svc.cluster.local"
    poorten = [8333, 9333, 8888, 8080, 19333, 18888, 18080, 8181, 9101, 9327]
    uitkomsten = {poort: seaweedfs.netwerk_bereikt(host, poort) for poort in poorten}
    bereikt = [poort for poort, uitvoer in uitkomsten.items() if "BEREIKT" in uitvoer.stdout]
    # Of dit cluster NetworkPolicies handhaaft, blijkt uit de meting zelf en niet uit de
    # naam van de CNI: alleen de poort die de regel openzet antwoordt, dus er wordt
    # gehandhaafd. (De aanname "kindnet doet niets met NetworkPolicies" is op dit cluster
    # gemeten onjuist.)
    dicht = [poort for poort in poorten if poort not in bereikt]
    gehandhaafd = bool(dicht) and 18888 not in bereikt
    rapport.rij(
        "I4 alleen de S3-poort is voor tenants bereikbaar",
        f"werkt: alleen 8333 antwoordt, dicht zijn {dicht}"
        if gehandhaafd and bereikt == [8333]
        else f"werkt anders: bereikbaar vanuit een tenant: {bereikt}",
        f"gehandhaafd: {gehandhaafd}\nbereikbaar vanuit {seaweedfs.TENANT_NAMESPACE}: {bereikt}\n\n"
        + "\n".join(f"{poort}: {uitvoer.stdout.strip()}" for poort, uitvoer in uitkomsten.items())
        + "\n\nDe regel zelf staat in infrastructure/bootstrap/infrastructure/seaweedfs/controller/base/"
        "network-policy.yaml: 8333 vanuit namespaces met created-by=operations-manager, de gRPC-poorten "
        "alleen vanuit de OPI-pod. Op een cluster met een handhavende CNI is dat wat de poorten dichtzet.",
    )
    assert 8333 in bereikt, f"de S3-poort antwoordt niet vanuit een tenant-namespace: {uitkomsten[8333].rapport()}"
    assert gehandhaafd, (
        f"geen enkele poort is dicht, dus dit cluster handhaaft geen NetworkPolicies en deze rij zegt niets: {bereikt}"
    )
    assert 18888 not in bereikt, f"de filer-gRPC is bereikbaar vanuit een tenant: {bereikt}"


def test_i5_de_filer_iam_is_niet_open(identiteiten: list[Identiteit], rapport: Rapport) -> None:
    """Met `jwt.filer_signing.key` gezet moet een aanroep ZONDER die sleutel geweigerd worden.

    Gemeten vanuit de SeaweedFS-pod zelf, met en zonder de sleutel in de omgeving: de
    `checkAdminAuth` van de filer kijkt naar het Bearer-token en niet naar de herkomst, dus
    de herkomst van deze meting doet niet mee; de weg van OPI zelf meet G18.
    """
    met_sleutel = seaweedfs.weed_shell("s3.user.list")
    zonder_sleutel = seaweedfs.weed_shell("s3.user.list", jwt=False)
    rapport.rij(
        "I5 de filer-IAM is niet open",
        "werkt" if met_sleutel.ok and not zonder_sleutel.ok else "ONTBREEKT: zonder sleutel komt de aanroep door",
        f"met WEED_JWT_FILER_SIGNING_KEY:\n{met_sleutel.rapport(300)}\n\nzonder:\n{zonder_sleutel.rapport(300)}",
    )
    assert met_sleutel.ok, met_sleutel.rapport()
    assert not zonder_sleutel.ok, (
        f"de IAM-gRPC van de filer neemt een aanroep zonder sleutel aan: {zonder_sleutel.rapport()}"
    )


# --------------------------------------------------------------------------------------
# K, B, M: kloon, back-up en metrieken. Gemeten voor het volgende plan, niet bedraad.
# --------------------------------------------------------------------------------------


def test_k1_bucketkopie_op_dezelfde_server(identiteiten: list[Identiteit], rapport: Rapport) -> None:
    seaweedfs.weed_shell("s3.bucket.create -name t-k1")
    met_mc = seaweedfs.mc(f"mc mirror --quiet admin/{BUCKET_A} admin/t-k1")
    bron = seaweedfs.mc(f"mc ls --recursive admin/{BUCKET_A} | wc -l")
    doel = seaweedfs.mc("mc ls --recursive admin/t-k1 | wc -l")
    met_aws = seaweedfs.aws("admin", f"aws s3 sync s3://{BUCKET_A} s3://t-k1-sync 2>&1 | tail -2", timeout=300.0)
    rapport.rij(
        "K1 bucketkopie op dezelfde server",
        "werkt" if met_mc.ok and bron.stdout.strip() == doel.stdout.strip() else "werkt anders",
        f"mc mirror:\n{met_mc.rapport(300)}\n\nobjecten bron/doel: {bron.stdout.strip()}/{doel.stdout.strip()}\n\n"
        f"aws s3 sync (doelbucket bestaat niet, dus dit meet alleen de fout):\n{met_aws.rapport(300)}",
    )
    assert met_mc.ok, met_mc.rapport()


def test_k2_bucketkopie_van_een_andere_s3_server(identiteiten: list[Identiteit], rapport: Rapport) -> None:
    """Van de sandbox-MinIO naar SeaweedFS en terug, met `mc mirror`."""
    minio = cluster.secret_values(seaweedfs.NAMESPACE, "minio-admin-credentials")
    seaweedfs.schrijf_sleutels(
        {
            "admin": seaweedfs.admin_sleutels(),
            "a": (GEBRUIKER_A, next(i.secret_key for i in identiteiten if i.naam == GEBRUIKER_A)),
            "b": (GEBRUIKER_B, next(i.secret_key for i in identiteiten if i.naam == GEBRUIKER_B)),
        }
    )
    minio_host = "minio.rig-system.svc.cluster.local:9000"
    env = f"export MC_HOST_minio=http://{minio['MINIO_ROOT_USER']}:{minio['MINIO_ROOT_PASSWORD']}@{minio_host}\n"
    seaweedfs.exec_in(
        seaweedfs.TENANT_NAMESPACE,
        seaweedfs.CLIENT_POD,
        "cat >> /tmp/mc-env",
        container=seaweedfs.MC_CONTAINER,
        stdin=env,
    )
    seaweedfs.weed_shell("s3.bucket.create -name t-k2")
    heen = seaweedfs.mc(
        "mc mb --ignore-existing minio/rc331-k2 && echo heen > /tmp/k2 "
        "&& mc cp --quiet /tmp/k2 minio/rc331-k2/k2 && mc mirror --quiet minio/rc331-k2 admin/t-k2"
    )
    terug = seaweedfs.mc("mc mirror --quiet admin/t-k2 minio/rc331-k2-terug")
    som_minio = seaweedfs.mc("mc cat minio/rc331-k2/k2 | sha256sum")
    som_seaweed = seaweedfs.mc("mc cat admin/t-k2/k2 | sha256sum")
    seaweedfs.mc("mc rb --force minio/rc331-k2 ; mc rb --force minio/rc331-k2-terug")
    rapport.rij(
        "K2 bucketkopie van of naar een andere S3-server",
        "werkt" if heen.ok and som_minio.stdout == som_seaweed.stdout else "werkt anders",
        f"heen:\n{heen.rapport(300)}\n\nterug:\n{terug.rapport(300)}\n\n"
        f"sha256 minio: {som_minio.stdout}\nsha256 seaweedfs: {som_seaweed.stdout}",
    )
    assert heen.ok, heen.rapport()
    assert som_minio.stdout == som_seaweed.stdout, "de kopie is niet byte-gelijk"


def test_b1_backup_met_de_projectsleutels(identiteiten: list[Identiteit], rapport: Rapport) -> None:
    """De stappen uit `backup-bucket-pod.yaml.jinja`, met de sleutels van de identiteit."""
    poort = seaweedfs.mc(f"mc ls a/{BUCKET_A}")
    spiegel = seaweedfs.mc(
        f"mkdir -p /tmp/b1 && mc mirror --preserve --quiet a/{BUCKET_A} /tmp/b1 "
        "&& ls /tmp/b1 | head -5 && find /tmp/b1 -type f | wc -l"
    )
    rapport.rij(
        "B1 back-up met de projectsleutels",
        "werkt" if poort.ok and spiegel.ok else "werkt anders",
        f"mc ls als gate:\n{poort.rapport(300)}\n\nmc mirror --preserve:\n{spiegel.rapport(300)}",
    )
    assert poort.ok, f"de gate van de back-uppod (`mc ls` met de projectsleutels) faalt: {poort.rapport()}"
    assert spiegel.ok, spiegel.rapport()


def test_b2_restore_met_admin_sleutels(identiteiten: list[Identiteit], rapport: Rapport) -> None:
    """De stappen uit `restore-bucket-pod.yaml.jinja`."""
    stappen = seaweedfs.mc(
        "mc mb --ignore-existing admin/t-b2 "
        "&& mc rm --recursive --force admin/t-b2 ; "
        "mc mirror --preserve --overwrite --quiet /tmp/b1 admin/t-b2 && mc ls admin/t-b2 | head -3"
    )
    rapport.rij(
        "B2 restore met admin-sleutels",
        "werkt" if stappen.ok else "werkt anders",
        stappen.rapport(500),
    )
    assert stappen.ok, stappen.rapport()


def test_b3_de_mirror_variant(identiteiten: list[Identiteit], rapport: Rapport) -> None:
    """De stappen uit `backup-bucket-mirror-pod.yaml.jinja`: mb, mirror, du, cp."""
    stappen = seaweedfs.mc(
        "mc mb --ignore-existing admin/t-b3 "
        f"&& mc mirror --preserve --overwrite --quiet admin/{BUCKET_A} admin/t-b3 "
        "&& mc du admin/t-b3 && echo kopie > /tmp/b3 && mc cp --quiet /tmp/b3 admin/t-b3/b3"
    )
    rapport.rij(
        "B3 de mirror-variant",
        "werkt" if stappen.ok else "werkt anders",
        stappen.rapport(500),
    )
    assert stappen.ok, stappen.rapport()


def _metrieken_met_collection() -> seaweedfs.Uitvoer:
    """De reeksen met een gevulde collection-label, uit de OPI-pod.

    Niet uit de clientpod: de NetworkPolicy laat de metrics-poort alleen vanuit rig-system
    toe (I4), dus uit een tenant-namespace meet je het beleid en niet de reeks.
    """
    return seaweedfs.exec_in(
        seaweedfs.NAMESPACE,
        seaweedfs.opi_pod(),
        f"curl -s --max-time 10 {seaweedfs.METRICS_URL} | grep -E 'collection=\"[^\"]' || true",
    )


def _grootte_regels(metrieken: seaweedfs.Uitvoer) -> list[str]:
    """De reeksen die de GROOTTE van bucket A noemen."""
    return [
        regel for regel in metrieken.stdout.splitlines() if "disk_size" in regel and f'collection="{BUCKET_A}"' in regel
    ]


def test_m1_bucketgrootte_als_metriek(identiteiten: list[Identiteit], rapport: Rapport) -> None:
    # Eerst zelf data neerzetten: een collection krijgt pas een volume, en dus pas een
    # reeks, als er iets in staat. Zonder deze stap meet de rij of een eerdere rij al iets
    # geschreven had, en dat maakt hem afhankelijk van de selectie waarmee je draait.
    seaweedfs.aws("a", f"echo m1 > /tmp/m1 && aws s3api put-object --bucket {BUCKET_A} --key m1 --body /tmp/m1")
    # Op disk_size wachten en niet op de bucketnaam: de reeks `volumes` staat er meteen, de
    # GROOTTE komt een scrape later, en de grootte is wat deze rij meet.
    cluster.wait_for(lambda: _grootte_regels(_metrieken_met_collection()), timeout=180, interval=5)
    metrieken = _metrieken_met_collection()
    namen = sorted({regel.split("{")[0] for regel in metrieken.stdout.splitlines() if regel.strip()})
    grootte = [naam for naam in namen if "disk_size" in naam]
    aantal = [naam for naam in namen if "object" in naam.lower() or "count" in naam.lower()]
    rapport.rij(
        "M1 bucketgrootte en objectaantal per bucket als metriek",
        f"werkt anders: wel grootte per bucket ({grootte}), geen objectaantal"
        if grootte and not aantal
        else ("werkt" if grootte and aantal else f"ONTBREEKT: geen serie per bucket, wel {namen}"),
        f"series met een gevulde collection-label: {namen}\n\n{metrieken.rapport(600)}",
    )
    assert grootte, f"geen reeks met de grootte per bucket, dus de opslagmetrieken hebben geen bron: {namen}"
    assert _grootte_regels(metrieken), (
        f"geen grootte-reeks voor de net gevulde bucket {BUCKET_A}: {metrieken.rapport(600)}"
    )


def test_m2_gebruik_in_rust(server: None, rapport: Rapport) -> None:
    """Geheugen en CPU uit de cgroup van de pod: deze sandbox heeft geen metrics-server."""
    cgroup = seaweedfs.exec_in(
        seaweedfs.NAMESPACE,
        seaweedfs.server_pod(),
        "cat /sys/fs/cgroup/memory.current /sys/fs/cgroup/memory.peak 2>/dev/null; cat /sys/fs/cgroup/cpu.stat",
    )
    regels = cgroup.stdout.splitlines()
    huidig = int(regels[0]) // (1024 * 1024) if regels and regels[0].isdigit() else -1
    piek = int(regels[1]) // (1024 * 1024) if len(regels) > 1 and regels[1].isdigit() else -1
    rapport.rij(
        "M2 gebruik in rust past op de sandbox",
        f"werkt: {huidig} MiB in gebruik, piek {piek} MiB (na de hele meting, inclusief een object van 24 MiB)",
        f"kubectl top bestaat hier niet (geen metrics-server), dus uit de cgroup:\n{cgroup.rapport(400)}",
    )
    assert huidig > 0, f"geen geheugenmeting uit de cgroup: {cgroup.rapport()}"


# --------------------------------------------------------------------------------------
# Stap 3: opruimen wat de meting achterlaat
# --------------------------------------------------------------------------------------


def test_stap3_de_meting_laat_niets_achter(identiteiten: list[Identiteit], rapport: Rapport) -> None:
    """Na de meting staan er alleen nog de admin-identiteit en geen testbuckets."""
    for naam in (GEBRUIKER_A, GEBRUIKER_B, "t_g8", "t_g10"):
        seaweedfs.weed_shell(f"s3.user.delete -name {naam}")
    for policy in seaweedfs.weed_shell("s3.policy -list").stdout.split():
        if policy.endswith("-policy"):
            seaweedfs.weed_shell(f"s3.policy -delete -name {policy}")
    for regel in seaweedfs.weed_shell("s3.bucket.list").stdout.splitlines():
        naam = regel.split("\t")[0].strip()
        if naam.startswith("t-"):
            seaweedfs.weed_shell(f"s3.bucket.delete -name {naam}")

    buckets = seaweedfs.weed_shell("s3.bucket.list")
    gebruikers = seaweedfs.weed_shell("s3.user.list")
    policies = seaweedfs.weed_shell("s3.policy -list")
    resten = [naam for naam in (GEBRUIKER_A, GEBRUIKER_B, "t_g8", "t_g10") if naam in gebruikers.stdout]
    policy_resten = [naam for naam in policies.stdout.split() if naam.endswith("-policy")]
    schoon = "t-" not in buckets.stdout and not resten and not policy_resten
    rapport.rij(
        "Stap 3: opruimen",
        "schoon" if schoon else "er staat nog iets van de meting",
        f"buckets:\n{buckets.rapport(300)}\n\ngebruikers:\n{gebruikers.rapport(300)}\n\n"
        f"policies:\n{policies.rapport(300)}",
    )
    assert "t-" not in buckets.stdout, f"testbuckets staan er nog: {buckets.rapport()}"
    assert not resten, f"testgebruikers staan er nog: {resten}\n{gebruikers.rapport()}"
    assert not policy_resten, f"policies van de meting staan er nog: {policy_resten}"
