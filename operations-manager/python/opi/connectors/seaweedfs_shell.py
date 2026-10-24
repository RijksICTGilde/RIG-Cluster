"""SeaweedFS-connector: `weed shell` als subprocess, met het commando op stdin.

De beheerweg van seaweedfs-storage (RC-331). `weed shell` praat over de
filer-gRPC en heeft BEIDE adressen nodig: zonder ``-master`` blijft hij in
``WaitUntilConnected`` hangen in plaats van een fout te geven (gemeten, rij G18). De
IAM-API op de S3-poort staat uit, dus dit is de enige weg.

Het commando gaat over stdin en nooit in argv: een wachtwoord in argv is voor iedere
andere lezer in de pod te zien in ``/proc``. Eén commando per proces, want in een gepijpte
run is de exitcode van het proces niet aan een van de regels toe te wijzen.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import tempfile
from typing import Any

import aiohttp

from opi.connectors.minio_mc import MinioExecutionError, create_minio_connector
from opi.core.cluster_config import get_seaweedfs_host, get_seaweedfs_port
from opi.core.config import settings
from opi.utils.naming import validate_bucket_name, validate_object_store_username, validate_policy_name
from opi.utils.s3_policy import ALL_BUCKET_PERMISSIONS, bucket_access_policy
from opi.utils.subprocess_env import child_env

logger = logging.getLogger(__name__)

#: De admin-Bearer die `weed shell` zelf uit zijn omgeving leest. Hij staat als platformgeheim
#: in PLATFORM_SECRET_NAMES, dus child_env filtert hem en hij gaat hier expliciet mee.
JWT_SLEUTEL_ENV = "WEED_JWT_FILER_SIGNING_KEY"

#: Hoe lang de anonieme toets op de S3-poort mag duren.
ANONIEME_TOETS_TIMEOUT_SECONDS = 10.0

#: Hoe lang een `weed shell`-commando mag duren. Een onbereikbare master laat `weed shell`
#: in WaitUntilConnected wachten zonder ooit een fout te geven (rij G18), en dan hangt de
#: aanroeper mee; dit maakt er een fout van.
SHELL_TIMEOUT_SECONDS = 30.0

#: De alias waaronder `mc` bij de S3-poort van SeaweedFS komt, naast "default-minio" van
#: MinIO. Alleen voor de lifecycle-regel; al het andere beheer loopt over `weed shell`.
S3_ALIAS = "default-seaweedfs"

#: De tekst waarin `weed shell` "bestaat al" legt, zodat aanmaken replay-safe is. Letterlijk
#: nagemeten op 4.48: `bucket <naam> already exists` en `code = AlreadyExists desc = user
#: <naam> already exists`.
_BESTAAT_AL = ("already exists",)

#: En waar hij "bestaat niet" in legt, zodat verwijderen dat ook is. Nagemeten:
#: `bucket not found: ...`, `code = NotFound desc = user <naam> not found` en bij detach
#: `get user "<naam>": ... not found`. Alleen de tekst onderscheidt dit van een echte fout,
#: want elke foute aanroep geeft exit 1 (rij G19). `s3.policy -delete` van een onbekende
#: policy geeft exit 0 en komt hier niet langs.
_BESTAAT_NIET = ("not found",)


def _jwt_sleutel() -> dict[str, str] | None:
    """De JWT-sleutel uit onze eigen omgeving, als er een is."""
    waarde = os.environ.get(JWT_SLEUTEL_ENV)
    return {JWT_SLEUTEL_ENV: waarde} if waarde else None


class SeaweedfsConfigurationError(Exception):
    """Dit cluster heeft geen SeaweedFS-adressen in zijn configuratie."""


class SeaweedfsValidationError(Exception):
    """Een naam die wij aanleveren voldoet niet aan de regels."""


class SeaweedfsExecutionError(Exception):
    """`weed shell` gaf een fout terug."""


class SeaweedfsConnector:
    """Beheer van buckets, identiteiten en policies in SeaweedFS via `weed shell`."""

    _instance: SeaweedfsConnector | None = None

    def __new__(cls) -> SeaweedfsConnector:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    @staticmethod
    def is_configured() -> bool:
        """Of dit cluster SeaweedFS heeft. De dienst bestaat alleen waar dat zo is."""
        return bool(settings.SEAWEEDFS_FILER_ADDRESS and settings.SEAWEEDFS_MASTER_ADDRESS)

    async def _shell(self, commando: str) -> tuple[str, str, int]:
        """Eén `weed shell`-commando, via stdin. Geeft (stdout, stderr, exitcode)."""
        if not self.is_configured():
            raise SeaweedfsConfigurationError(
                "SEAWEEDFS_FILER_ADDRESS en SEAWEEDFS_MASTER_ADDRESS zijn niet gezet; "
                "dit cluster levert seaweedfs-storage niet"
            )

        proces = await asyncio.create_subprocess_exec(
            "weed",
            "shell",
            f"-filer={settings.SEAWEEDFS_FILER_ADDRESS}",
            f"-master={settings.SEAWEEDFS_MASTER_ADDRESS}",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=child_env(_jwt_sleutel()),
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proces.communicate(input=f"{commando}\n".encode()), timeout=SHELL_TIMEOUT_SECONDS
            )
        except TimeoutError as e:
            with contextlib.suppress(ProcessLookupError):
                proces.kill()
            raise SeaweedfsExecutionError(
                f"weed shell gaf binnen {SHELL_TIMEOUT_SECONDS:.0f} seconden geen antwoord "
                f"(filer {settings.SEAWEEDFS_FILER_ADDRESS}, master {settings.SEAWEEDFS_MASTER_ADDRESS})"
            ) from e
        return stdout.decode().strip(), stderr.decode().strip(), proces.returncode or 0

    async def _uitvoeren(self, commando: str, *, wat: str, verdraagt: tuple[str, ...] = ()) -> str:
        """Draai een commando en maak een fout een uitzondering.

        ``verdraagt`` zijn teksten die geen fout zijn maar een normale uitkomst ("bestaat
        al" bij aanmaken, "bestaat niet" bij verwijderen). Het commando staat niet in de
        melding: er kan een sleutel in staan.
        """
        stdout, stderr, code = await self._shell(commando)
        if code == 0:
            return stdout
        tekst = f"{stdout}\n{stderr}".lower()
        if any(fragment in tekst for fragment in verdraagt):
            logger.debug("%s: %s", wat, stderr or stdout)
            return stdout
        raise SeaweedfsExecutionError(f"{wat} mislukte (exit {code}): {stderr or stdout}")

    @staticmethod
    def _bucket(naam: str) -> str:
        try:
            return validate_bucket_name(naam)
        except ValueError as e:
            raise SeaweedfsValidationError(str(e)) from e

    @staticmethod
    def _gebruiker(naam: str) -> str:
        try:
            return validate_object_store_username(naam)
        except ValueError as e:
            raise SeaweedfsValidationError(str(e)) from e

    @staticmethod
    def _policy(naam: str) -> str:
        try:
            return validate_policy_name(naam)
        except ValueError as e:
            raise SeaweedfsValidationError(str(e)) from e

    # Buckets

    async def create_bucket(self, name: str, owner: str | None = None) -> None:
        """Maak een bucket aan. Een bestaande bucket is een normale uitkomst (rij G1)."""
        bucket = self._bucket(name)
        commando = f"s3.bucket.create -name {bucket}"
        if owner:
            commando += f" -owner {self._gebruiker(owner)}"
        await self._uitvoeren(commando, wat=f"bucket {bucket} aanmaken", verdraagt=_BESTAAT_AL)

    async def delete_bucket(self, name: str) -> None:
        """Verwijder een bucket met alles erin (rij G3: ook de versies gaan mee)."""
        bucket = self._bucket(name)
        await self._uitvoeren(
            f"s3.bucket.delete -name {bucket}", wat=f"bucket {bucket} verwijderen", verdraagt=_BESTAAT_NIET
        )

    async def list_buckets(self) -> list[str]:
        """De bucketnamen. De uitvoer is geen JSON maar tab-gescheiden regels (rij G4)."""
        uitvoer = await self._uitvoeren("s3.bucket.list", wat="buckets opsommen")
        return [regel.split("\t")[0].strip() for regel in uitvoer.splitlines() if regel.strip()]

    async def get_versioning(self, name: str) -> bool:
        """Of versioning aanstaat. `s3.bucket.versioning` zonder vlag leest de stand."""
        bucket = self._bucket(name)
        uitvoer = await self._uitvoeren(f"s3.bucket.versioning -name {bucket}", wat=f"versioning van {bucket} lezen")
        return "enabled" in uitvoer.lower()

    async def ensure_noncurrent_version_rule(
        self, name: str, max_versions: int, expire_days: int | None = None
    ) -> dict[str, Any]:
        """De grens op oude versies als lifecycle-regel op een bucket, idempotent (RC-380).

        Over de S3-poort met de admin-sleutels en niet over `weed shell`: 4.48 heeft geen
        lifecycle-commando, alleen `s3.bucket.lifecycle.fastpath` en
        `s3.lifecycle.run-shard`. De S3-client van dit project is `mc` in
        ``MinioConnector``, en het regel-document en zijn idempotentie staan daar al, dus
        hier staat geen tweede exemplaar.

        SeaweedFS voert de regel uit in zijn lifecycle-ronde. Die ronde start niet vanzelf;
        de CronJob ``seaweedfs-lifecycle`` van het component draait hem eenmaal per nacht
        (zie features/seaweedfs-objectopslag.md, rij G20).

        Raises:
            SeaweedfsConfigurationError: dit cluster heeft geen SeaweedFS of geen admin-sleutels.
            SeaweedfsExecutionError: de regel is niet te lezen of te schrijven.
        """
        bucket = self._bucket(name)
        host = get_seaweedfs_host(settings.CLUSTER_MANAGER)
        if host is None or not (settings.SEAWEEDFS_ADMIN_ACCESS_KEY and settings.SEAWEEDFS_ADMIN_SECRET_KEY):
            raise SeaweedfsConfigurationError(
                "de S3-admin-sleutels van SeaweedFS staan niet in de omgeving "
                "(SEAWEEDFS_ADMIN_ACCESS_KEY/SEAWEEDFS_ADMIN_SECRET_KEY); "
                "zonder die sleutels is er geen weg naar de lifecycle-regel"
            )

        mc = create_minio_connector()
        if not mc.is_alias_configured(S3_ALIAS) and not await mc.configure_alias(
            S3_ALIAS,
            f"{host}:{get_seaweedfs_port(settings.CLUSTER_MANAGER)}",
            access_key=settings.SEAWEEDFS_ADMIN_ACCESS_KEY,
            secret_key=settings.SEAWEEDFS_ADMIN_SECRET_KEY,
        ):
            raise SeaweedfsExecutionError(f"de S3-poort van SeaweedFS is niet bereikbaar als alias {S3_ALIAS}")

        try:
            return await mc.ensure_noncurrent_version_rule(S3_ALIAS, bucket, max_versions, expire_days)
        except MinioExecutionError as e:
            raise SeaweedfsExecutionError(f"de grens op oude versies van bucket {bucket} zetten mislukte: {e}") from e

    async def set_versioning(self, name: str, enabled: bool) -> None:
        bucket = self._bucket(name)
        vlag = "-enable" if enabled else "-suspend"
        await self._uitvoeren(
            f"s3.bucket.versioning -name {bucket} {vlag}",
            wat=f"versioning van {bucket} {'aanzetten' if enabled else 'suspenden'}",
        )

    # Identiteiten

    async def create_user(self, name: str, access_key: str, secret_key: str) -> None:
        """Maak een identiteit met de sleutels die wij kiezen (rij G8)."""
        gebruiker = self._gebruiker(name)
        await self._uitvoeren(
            f"s3.user.create -name {gebruiker} -access_key {self._gebruiker(access_key)} -secret_key {secret_key}",
            wat=f"identiteit {gebruiker} aanmaken",
            verdraagt=_BESTAAT_AL,
        )

    async def delete_user(self, name: str) -> None:
        gebruiker = self._gebruiker(name)
        await self._uitvoeren(
            f"s3.user.delete -name {gebruiker}", wat=f"identiteit {gebruiker} verwijderen", verdraagt=_BESTAAT_NIET
        )

    async def user_exists(self, name: str) -> bool:
        """`s3.user.show` geeft JSON, en exit 1 op een onbekende naam (rij G9)."""
        _, _, code = await self._shell(f"s3.user.show -name {self._gebruiker(name)}")
        return code == 0

    # Policies

    async def put_policy(self, name: str, document: dict[str, Any]) -> None:
        """Sla een policy-document op. `s3.policy -put` leest het uit een bestand."""
        policy = self._policy(name)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, dir=settings.TEMP_DIR) as bestand:
            json.dump(document, bestand, indent=2)
            pad = bestand.name
        try:
            await self._uitvoeren(f"s3.policy -put -name {policy} -file {pad}", wat=f"policy {policy} opslaan")
        finally:
            with contextlib.suppress(OSError):
                os.unlink(pad)

    async def attach_policy(self, policy: str, user: str) -> None:
        naam = self._policy(policy)
        gebruiker = self._gebruiker(user)
        await self._uitvoeren(
            f"s3.policy.attach -policy {naam} -user {gebruiker}", wat=f"policy {naam} koppelen aan {gebruiker}"
        )

    async def detach_policy(self, policy: str, user: str) -> None:
        naam = self._policy(policy)
        gebruiker = self._gebruiker(user)
        await self._uitvoeren(
            f"s3.policy.detach -policy {naam} -user {gebruiker}",
            wat=f"policy {naam} ontkoppelen van {gebruiker}",
            verdraagt=_BESTAAT_NIET,
        )

    async def delete_policy(self, name: str) -> None:
        policy = self._policy(name)
        await self._uitvoeren(
            f"s3.policy -delete -name {policy}", wat=f"policy {policy} verwijderen", verdraagt=_BESTAAT_NIET
        )

    async def anonymous_access_allowed(self, s3_endpoint: str) -> bool:
        """Of de S3-poort een ongeauthenticeerde ListBuckets aanneemt.

        Zonder ook maar een identiteit zet SeaweedFS ``isAuthEnabled`` op false en antwoordt
        deze aanroep met 200 en de volledige bucketlijst (RC-331, rij G14). Mét identiteit
        is het 403. Dit is dus de meting of het admin-sleutelpaar zijn werk doet, en niet
        of een secret bestaat.
        """
        timeout = aiohttp.ClientTimeout(total=ANONIEME_TOETS_TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(timeout=timeout) as sessie, sessie.get(f"{s3_endpoint}/") as antwoord:
            return antwoord.status == 200

    async def grant_bucket_access(self, user: str, bucket: str, policy_name: str) -> None:
        """Het gedeelde document opslaan en koppelen: dezelfde isolatie als bij MinIO."""
        await self.put_policy(policy_name, bucket_access_policy(self._bucket(bucket), ALL_BUCKET_PERMISSIONS))
        await self.attach_policy(policy_name, user)


def create_seaweedfs_connector() -> SeaweedfsConnector:
    """Factory, zoals ``create_minio_connector``."""
    return SeaweedfsConnector()
