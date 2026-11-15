"""SeaweedFS connector: `weed shell` as a subprocess, with the command on stdin.

The management path of seaweedfs-storage. `weed shell` talks over the filer gRPC and needs
BOTH addresses: without ``-master`` it hangs in ``WaitUntilConnected`` instead of failing
(measured, row G18). The IAM API on the S3 port is off, so this is the only way.

The command goes over stdin and never into argv: a password in argv is readable in ``/proc``
by every other reader in the pod. One command per process, because in a piped run the exit
code of the process cannot be attributed to any one of the lines.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import tempfile
from typing import Any

from opi.connectors.http import client_session
from opi.connectors.minio_mc import MinioExecutionError, create_minio_connector
from opi.core.cluster_config import get_seaweedfs_host, get_seaweedfs_port
from opi.core.config import settings
from opi.core.subprocess_env import child_env
from opi.utils.naming import validate_bucket_name, validate_object_store_username, validate_policy_name
from opi.utils.s3_policy import ALL_BUCKET_PERMISSIONS, bucket_access_policy

logger = logging.getLogger(__name__)

#: The admin bearer `weed shell` reads from its own environment. It is a platform secret in
#: PLATFORM_SECRET_NAMES, so child_env filters it out and it is passed in here explicitly.
JWT_KEY_ENV = "WEED_JWT_FILER_SIGNING_KEY"

#: How long the anonymous check on the S3 port may take.
ANONYMOUS_CHECK_TIMEOUT_SECONDS = 10.0

#: How long one `weed shell` command may take. An unreachable master leaves `weed shell`
#: waiting in WaitUntilConnected without ever failing (row G18), and the caller waits along;
#: this turns it into an error.
SHELL_TIMEOUT_SECONDS = 30.0

#: The alias under which `mc` reaches the S3 port of SeaweedFS, next to MinIO's
#: "default-minio". Only for the lifecycle rule; all other management goes over `weed shell`.
S3_ALIAS = "default-seaweedfs"

#: The text `weed shell` puts "already exists" in, so creating is replay-safe. Measured
#: literally on 4.48: `bucket <name> already exists` and `code = AlreadyExists desc = user
#: <name> already exists`.
_ALREADY_EXISTS = ("already exists",)

#: And where it puts "does not exist", so deleting is replay-safe too. Measured:
#: `bucket not found: ...`, `code = NotFound desc = user <name> not found` and on detach
#: `get user "<name>": ... not found`. Only the text separates this from a real error, because
#: every wrong call exits 1 (row G19). `s3.policy -delete` of an unknown policy exits 0 and
#: never gets here.
_NOT_FOUND = ("not found",)


def _jwt_key() -> dict[str, str] | None:
    """The JWT key as an environment mapping, or None when it is not set."""
    value = os.environ.get(JWT_KEY_ENV)
    return {JWT_KEY_ENV: value} if value else None


class SeaweedfsConfigurationError(Exception):
    """This cluster has no SeaweedFS addresses in its configuration."""


class SeaweedfsValidationError(Exception):
    """A name we supply does not meet the rules."""


class SeaweedfsExecutionError(Exception):
    """`weed shell` returned an error."""


class SeaweedfsConnector:
    """Buckets, identities and policies in SeaweedFS, over `weed shell`."""

    _instance: SeaweedfsConnector | None = None

    def __new__(cls) -> SeaweedfsConnector:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    @staticmethod
    def is_configured() -> bool:
        """Whether this cluster has SeaweedFS. The service only exists where it does."""
        return bool(settings.SEAWEEDFS_FILER_ADDRESS and settings.SEAWEEDFS_MASTER_ADDRESS)

    async def _shell(self, command: str) -> tuple[str, str, int]:
        """One `weed shell` command, over stdin. Returns (stdout, stderr, exit code)."""
        if not self.is_configured():
            raise SeaweedfsConfigurationError(
                "SEAWEEDFS_FILER_ADDRESS en SEAWEEDFS_MASTER_ADDRESS zijn niet gezet; "
                "dit cluster levert seaweedfs-storage niet"
            )

        process = await asyncio.create_subprocess_exec(
            "weed",
            "shell",
            f"-filer={settings.SEAWEEDFS_FILER_ADDRESS}",
            f"-master={settings.SEAWEEDFS_MASTER_ADDRESS}",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=child_env(_jwt_key()),
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(input=f"{command}\n".encode()), timeout=SHELL_TIMEOUT_SECONDS
            )
        except TimeoutError as e:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            raise SeaweedfsExecutionError(
                f"weed shell gaf binnen {SHELL_TIMEOUT_SECONDS:.0f} seconden geen antwoord "
                f"(filer {settings.SEAWEEDFS_FILER_ADDRESS}, master {settings.SEAWEEDFS_MASTER_ADDRESS})"
            ) from e
        return stdout.decode().strip(), stderr.decode().strip(), process.returncode or 0

    async def _run(self, command: str, *, action: str, tolerated: tuple[str, ...] = ()) -> str:
        """Run a command and turn a failure into an exception.

        ``tolerated`` holds texts that are not a failure but a normal outcome ("already
        exists" on create, "not found" on delete). The command stays out of the message: it
        may hold a key.
        """
        stdout, stderr, code = await self._shell(command)
        if code == 0:
            return stdout
        text = f"{stdout}\n{stderr}".lower()
        if any(fragment in text for fragment in tolerated):
            logger.debug("%s: %s", action, stderr or stdout)
            return stdout
        raise SeaweedfsExecutionError(f"{action} mislukte (exit {code}): {stderr or stdout}")

    @staticmethod
    def _bucket(name: str) -> str:
        try:
            return validate_bucket_name(name)
        except ValueError as e:
            raise SeaweedfsValidationError(str(e)) from e

    @staticmethod
    def _username(name: str) -> str:
        try:
            return validate_object_store_username(name)
        except ValueError as e:
            raise SeaweedfsValidationError(str(e)) from e

    @staticmethod
    def _policy(name: str) -> str:
        try:
            return validate_policy_name(name)
        except ValueError as e:
            raise SeaweedfsValidationError(str(e)) from e

    async def create_bucket(self, name: str, owner: str | None = None) -> None:
        """Create a bucket. An existing bucket is a normal outcome (row G1)."""
        bucket = self._bucket(name)
        command = f"s3.bucket.create -name {bucket}"
        if owner:
            command += f" -owner {self._username(owner)}"
        await self._run(command, action=f"bucket {bucket} aanmaken", tolerated=_ALREADY_EXISTS)

    async def delete_bucket(self, name: str) -> None:
        """Delete a bucket with everything in it (row G3: the versions go too)."""
        bucket = self._bucket(name)
        await self._run(f"s3.bucket.delete -name {bucket}", action=f"bucket {bucket} verwijderen", tolerated=_NOT_FOUND)

    async def list_buckets(self) -> list[str]:
        """The bucket names. The output is not JSON but tab-separated lines (row G4)."""
        output = await self._run("s3.bucket.list", action="buckets opsommen")
        return [line.split("\t")[0].strip() for line in output.splitlines() if line.strip()]

    async def get_versioning(self, name: str) -> bool:
        """Whether versioning is on. `s3.bucket.versioning` without a flag reads the state."""
        bucket = self._bucket(name)
        output = await self._run(f"s3.bucket.versioning -name {bucket}", action=f"versioning van {bucket} lezen")
        return "enabled" in output.lower()

    async def ensure_noncurrent_version_rule(
        self, name: str, max_versions: int, expire_days: int | None = None
    ) -> dict[str, Any]:
        """The limit on old versions as a lifecycle rule on a bucket, idempotent.

        Over the S3 port with the admin keys and not over `weed shell`: 4.48 has no lifecycle
        command, only `s3.bucket.lifecycle.fastpath` and `s3.lifecycle.run-shard`. The rule
        document and its idempotency live in ``MinioConnector``, this delegates to it.

        SeaweedFS applies the rule in its lifecycle round. That round does not start by
        itself; the component's CronJob ``seaweedfs-lifecycle`` runs it once a night (see
        features/seaweedfs-objectopslag.md, row G20).

        Raises:
            SeaweedfsConfigurationError: this cluster has no SeaweedFS or no admin keys.
            SeaweedfsExecutionError: the rule cannot be read or written.
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
        flag = "-enable" if enabled else "-suspend"
        await self._run(
            f"s3.bucket.versioning -name {bucket} {flag}",
            action=f"versioning van {bucket} {'aanzetten' if enabled else 'suspenden'}",
        )

    async def create_user(self, name: str, access_key: str, secret_key: str) -> None:
        """Create an identity with the keys we choose (row G8)."""
        username = self._username(name)
        await self._run(
            f"s3.user.create -name {username} -access_key {self._username(access_key)} -secret_key {secret_key}",
            action=f"identiteit {username} aanmaken",
            tolerated=_ALREADY_EXISTS,
        )

    async def delete_user(self, name: str) -> None:
        username = self._username(name)
        await self._run(
            f"s3.user.delete -name {username}", action=f"identiteit {username} verwijderen", tolerated=_NOT_FOUND
        )

    async def user_exists(self, name: str) -> bool:
        """`s3.user.show` returns JSON, and exit 1 on an unknown name (row G9)."""
        _, _, code = await self._shell(f"s3.user.show -name {self._username(name)}")
        return code == 0

    async def put_policy(self, name: str, document: dict[str, Any]) -> None:
        """Store a policy document. `s3.policy -put` reads it from a file."""
        policy = self._policy(name)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, dir=settings.TEMP_DIR) as handle:
            json.dump(document, handle, indent=2)
            path = handle.name
        try:
            await self._run(f"s3.policy -put -name {policy} -file {path}", action=f"policy {policy} opslaan")
        finally:
            with contextlib.suppress(OSError):
                os.unlink(path)

    async def attach_policy(self, policy: str, user: str) -> None:
        name = self._policy(policy)
        username = self._username(user)
        await self._run(
            f"s3.policy.attach -policy {name} -user {username}", action=f"policy {name} koppelen aan {username}"
        )

    async def detach_policy(self, policy: str, user: str) -> None:
        name = self._policy(policy)
        username = self._username(user)
        await self._run(
            f"s3.policy.detach -policy {name} -user {username}",
            action=f"policy {name} ontkoppelen van {username}",
            tolerated=_NOT_FOUND,
        )

    async def delete_policy(self, name: str) -> None:
        policy = self._policy(name)
        await self._run(
            f"s3.policy -delete -name {policy}", action=f"policy {policy} verwijderen", tolerated=_NOT_FOUND
        )

    async def anonymous_access_allowed(self, s3_endpoint: str) -> bool:
        """Whether the S3 port accepts an unauthenticated ListBuckets.

        Without a single identity SeaweedFS sets ``isAuthEnabled`` to false and answers this
        call with 200 and the full bucket list (row G14). With an identity it is 403. So this
        measures whether the admin key pair does its work, not whether a secret exists.
        """
        async with (
            client_session(ANONYMOUS_CHECK_TIMEOUT_SECONDS) as session,
            session.get(f"{s3_endpoint}/") as response,
        ):
            return response.status == 200

    async def grant_bucket_access(self, user: str, bucket: str, policy_name: str) -> None:
        """Store the shared document and attach it: the same isolation as on MinIO."""
        await self.put_policy(policy_name, bucket_access_policy(self._bucket(bucket), ALL_BUCKET_PERMISSIONS))
        await self.attach_policy(policy_name, user)


def create_seaweedfs_connector() -> SeaweedfsConnector:
    """The SeaweedfsConnector singleton."""
    return SeaweedfsConnector()
