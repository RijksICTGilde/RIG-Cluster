"""SeaweedFS-connector: `weed shell` als subprocess, met het commando op stdin.

De beheerweg van de proef seaweedfs-storage (RC-331). `weed shell` praat over de
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

from opi.core.config import settings
from opi.utils.naming import validate_bucket_name, validate_object_store_username, validate_policy_name
from opi.utils.s3_policy import ALL_BUCKET_PERMISSIONS, bucket_access_policy
from opi.utils.subprocess_env import child_env

logger = logging.getLogger(__name__)

#: De admin-Bearer die `weed shell` zelf uit zijn omgeving leest. Hij staat als platformgeheim
#: in PLATFORM_SECRET_NAMES, dus child_env filtert hem en hij gaat hier expliciet mee.
JWT_SLEUTEL_ENV = "WEED_JWT_FILER_SIGNING_KEY"

#: Hoe lang een `weed shell`-commando mag duren. Een onbereikbare master laat `weed shell`
#: in WaitUntilConnected wachten zonder ooit een fout te geven (rij G18), en dan hangt de
#: aanroeper mee; dit maakt er een fout van.
SHELL_TIMEOUT_SECONDS = 30.0

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

    async def grant_bucket_access(self, user: str, bucket: str, policy_name: str) -> None:
        """Het gedeelde document opslaan en koppelen: dezelfde isolatie als bij MinIO."""
        await self.put_policy(policy_name, bucket_access_policy(self._bucket(bucket), ALL_BUCKET_PERMISSIONS))
        await self.attach_policy(policy_name, user)


def create_seaweedfs_connector() -> SeaweedfsConnector:
    """Factory, zoals ``create_minio_connector``."""
    return SeaweedfsConnector()
