"""OPI's MinIO-connector en -manager tegen een echte MinIO in de gepinde versie.

Elke methode van ``MinioConnector`` die ``opi/`` aanroept, draait hier ten minste een keer,
en het terugleeskanaal is ``mc`` met zijn eigen alias (zie ``minio_live.Mc``). Waar het om
het gevolg gaat, meet de toets het gevolg: met de sleutels van een deployment lukt schrijven
in de eigen bucket en lukt het niet in die van een ander.

Draai hem met ``task test-minio``; zonder ``MINIO_LIVE_HOST`` slaat de groep zichzelf over.
``tests/test_live_group_coverage.py`` bewaakt dat er geen methode ontbreekt.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from opi.connectors.minio_mc import MinioConnector, MinioExecutionError, create_minio_connector
from opi.core.config import settings
from opi.core.startup import check_minio_availability
from opi.handlers.project_file_handler import ProjectFileHandler
from opi.manager.minio_manager import MinioManager
from opi.utils.naming import generate_bucket_name, generate_minio_username
from tests.integration.minio_live import (
    ADMIN_ALIAS,
    OTHER_SECRET,
    USER_SECRET,
    Mc,
    admin_access_key,
    admin_mc,
    admin_secret_key,
    assert_local,
    drop_by_prefix,
    host,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = [pytest.mark.requires_infra, pytest.mark.minio_live]

#: Alles wat deze groep maakt begint hiermee, en de opruiming gaat op dit voorvoegsel.
PREFIX = "zadlive"
PROJECT = PREFIX
CLUSTER = "sandboxed-local"
DEPLOYMENTS = ("d1", "d2")

USER = f"{PREFIX}-user"
OTHER_USER = f"{PREFIX}-other"
BUCKET = f"{PREFIX}-bucket"
OTHER_BUCKET = f"{PREFIX}-other-bucket"


@pytest.fixture
def connector() -> Iterator[MinioConnector]:
    """De connector zoals OPI hem bouwt, met de alias die de manager ook gebruikt."""
    assert_local()
    built = create_minio_connector()
    if not built.is_mc_available:
        pytest.fail("mc staat niet in de PATH; task test-minio zet de mc van de OPI-image erin")
    drop_by_prefix(PREFIX)
    yield built
    drop_by_prefix(PREFIX)


@pytest.fixture
async def alias(connector: MinioConnector) -> str:
    assert (
        await connector.configure_alias(
            alias=ADMIN_ALIAS, host=host(), access_key=admin_access_key(), secret_key=admin_secret_key()
        )
        is True
    )
    assert connector.is_alias_configured(ADMIN_ALIAS) is True
    return ADMIN_ALIAS


async def test_configure_alias_answers_false_on_credentials_that_do_not_work(
    connector: MinioConnector,
) -> None:
    """De alias is geen lokale administratie: hij test de verbinding en meldt een fout."""
    assert (
        await connector.configure_alias(
            alias="zadlive-wrong", host=host(), access_key=admin_access_key(), secret_key=OTHER_SECRET
        )
        is False
    )
    assert connector.is_alias_configured("zadlive-wrong") is False


async def test_a_user_with_a_bucket_policy_writes_only_in_its_own_bucket(connector: MinioConnector, alias: str) -> None:
    assert (await connector.create_user(alias, USER, USER_SECRET))["status"] == "created"
    assert (await connector.create_user(alias, OTHER_USER, OTHER_SECRET))["status"] == "created"
    assert (await connector.create_bucket(alias, BUCKET))["status"] == "created"
    assert (await connector.create_bucket(alias, OTHER_BUCKET))["status"] == "created"

    assert {user["username"] for user in await connector.list_users(alias)} >= {USER, OTHER_USER}
    assert {bucket["name"] for bucket in await connector.list_buckets(alias)} >= {BUCKET, OTHER_BUCKET}

    result = await connector.grant_bucket_access(alias, USER, BUCKET)
    assert result["status"] == "granted"
    policy = result["policy_name"]
    assert policy in admin_mc().policies_of(USER)

    # Het gevolg, niet de aanroep: de eigen bucket lukt, die van een ander niet.
    mine = Mc(alias="zadlive-user", access_key=USER, secret_key=USER_SECRET)
    assert mine.put(BUCKET, "regel.txt", "inhoud")[1] == 0
    assert mine.read(BUCKET, "regel.txt") == "inhoud"
    output, code = mine.put(OTHER_BUCKET, "regel.txt", "inhoud")
    assert code != 0, f"de sleutels van {USER} schrijven in {OTHER_BUCKET}: {output}"

    assert await connector.bucket_has_objects(alias, BUCKET) is True
    assert await connector.bucket_has_objects(alias, OTHER_BUCKET) is False


async def test_detaching_the_policy_takes_the_access_away(connector: MinioConnector, alias: str) -> None:
    await connector.create_user(alias, USER, USER_SECRET)
    await connector.create_bucket(alias, BUCKET)
    policy = (await connector.grant_bucket_access(alias, USER, BUCKET))["policy_name"]

    mine = Mc(alias="zadlive-user", access_key=USER, secret_key=USER_SECRET)
    assert mine.put(BUCKET, "voor.txt", "inhoud")[1] == 0

    assert (await connector.detach_policy(alias, policy, USER))["status"] == "detached"
    assert policy not in admin_mc().policies_of(USER)
    output, code = mine.put(BUCKET, "na.txt", "inhoud")
    assert code != 0, f"{USER} schrijft nog in {BUCKET} na het losmaken van het beleid: {output}"

    assert (await connector.remove_policy(alias, policy))["status"] == "success"
    assert policy not in {
        entry["policy"] for entry in admin_mc().json_lines("admin", "policy", "list", "readback", "--json")
    }


async def test_attach_policy_puts_an_existing_policy_back(connector: MinioConnector, alias: str) -> None:
    """De verwerking draait opnieuw op een gebruiker die zijn beleid kwijt is."""
    await connector.create_user(alias, USER, USER_SECRET)
    await connector.create_bucket(alias, BUCKET)
    policy = (await connector.grant_bucket_access(alias, USER, BUCKET))["policy_name"]
    await connector.detach_policy(alias, policy, USER)

    assert (await connector.attach_policy(alias, policy, USER))["status"] == "attached"
    assert policy in admin_mc().policies_of(USER)
    mine = Mc(alias="zadlive-user", access_key=USER, secret_key=USER_SECRET)
    assert mine.put(BUCKET, "weer.txt", "inhoud")[1] == 0


async def test_versioning_and_the_noncurrent_rule_land_on_the_bucket(connector: MinioConnector, alias: str) -> None:
    """De stand van versioning leest de connector uit mc; dat veld verhuisde al eens (RC-331)."""
    await connector.create_bucket(alias, BUCKET)
    assert (await connector.get_bucket_versioning_status(alias, BUCKET))["status"] == "Unversioned"

    assert (await connector.ensure_bucket_versioning(alias, BUCKET, True))["status"] == "enabled"
    assert (await connector.get_bucket_versioning_status(alias, BUCKET))["enabled"] is True
    assert (await connector.ensure_bucket_versioning(alias, BUCKET, True))["status"] == "unchanged"

    created = await connector.ensure_noncurrent_version_rule(alias, BUCKET, max_versions=3)
    assert created["status"] == "created"
    assert created["rule"]["ID"] in admin_mc().lifecycle_rule_ids(BUCKET)
    assert (await connector.ensure_noncurrent_version_rule(alias, BUCKET, max_versions=3))["status"] == "unchanged"
    assert (await connector.ensure_noncurrent_version_rule(alias, BUCKET, max_versions=5))["status"] == "updated"

    assert (await connector.ensure_bucket_versioning(alias, BUCKET, False))["status"] == "suspended"
    assert (await connector.get_bucket_versioning_status(alias, BUCKET))["suspended"] is True


async def test_mirror_bucket_cross_alias_copies_every_object(connector: MinioConnector, alias: str) -> None:
    """De kloon van een bucket (``CLONE_BUCKET``) gaat over deze methode."""
    await connector.create_bucket(alias, BUCKET)
    await connector.create_bucket(alias, OTHER_BUCKET)
    readback = admin_mc()
    readback.ok("pipe", f"{readback.alias}/{BUCKET}/een.txt", stdin="een")
    readback.ok("pipe", f"{readback.alias}/{BUCKET}/map/twee.txt", stdin="twee")

    result = await connector.mirror_bucket_cross_alias(alias, BUCKET, alias, OTHER_BUCKET)
    assert result["status"] == "success", result
    assert readback.objects(OTHER_BUCKET) == readback.objects(BUCKET)
    assert readback.read(OTHER_BUCKET, "een.txt") == "een"


async def test_deleting_the_bucket_and_the_user_leaves_nothing_behind(connector: MinioConnector, alias: str) -> None:
    await connector.create_user(alias, USER, USER_SECRET)
    await connector.create_bucket(alias, BUCKET)
    readback = admin_mc()
    readback.ok("pipe", f"{readback.alias}/{BUCKET}/een.txt", stdin="een")

    # Zonder force weigert MinIO een bucket met objecten; dat is de grendel onder een
    # verwijdering die data zou weggooien.
    with pytest.raises(MinioExecutionError):
        await connector.delete_bucket(alias, BUCKET)

    assert (await connector.delete_bucket(alias, BUCKET, force=True))["status"] == "deleted"
    assert BUCKET not in {bucket["name"] for bucket in await connector.list_buckets(alias)}

    assert (await connector.delete_user(alias, USER))["status"] == "deleted"
    assert USER not in {user["username"] for user in await connector.list_users(alias)}
    assert (await connector.delete_user(alias, USER))["status"] == "not_found"


async def test_check_minio_availability_sees_the_live_server(connector: MinioConnector) -> None:
    """De opstartstap: zonder werkende ``mc`` komt OPI niet aan een bucket toe."""
    assert await check_minio_availability() is True


# ==================== Het scenario: twee deployments via de manager ====================


def project_data() -> dict[str, Any]:
    return {
        "name": PROJECT,
        "clusters": [CLUSTER],
        "services": ["minio-storage"],
        "components": [{"name": "app", "services": ["minio-storage"]}],
        "deployments": [
            {"name": naam, "cluster": CLUSTER, "namespace": PREFIX, "components": [{"reference": "app"}]}
            for naam in DEPLOYMENTS
        ],
    }


@pytest.fixture
def minio_manager(connector: MinioConnector, monkeypatch: pytest.MonkeyPatch) -> MinioManager:
    """De MinioManager op de wegwerpserver, met een ProjectManager die alleen antwoordt.

    Kubernetes heeft nog geen live groep, dus het secret-kanaal is een mock: hij meldt dat
    er nog geen secret staat, zoals bij een eerste verwerking.
    """
    monkeypatch.setattr(settings, "MINIO_HOST", host())
    monkeypatch.setattr(settings, "MINIO_ADMIN_ACCESS_KEY", admin_access_key())
    monkeypatch.setattr(settings, "MINIO_ADMIN_SECRET_KEY", admin_secret_key())
    monkeypatch.setattr(settings, "MINIO_USE_TLS", False)
    monkeypatch.setattr("opi.manager.minio_manager.other_project_deployments", lambda _name: {})

    stub = MagicMock()
    stub.get_name = AsyncMock(return_value=PROJECT)
    stub.get_progress_manager.return_value = None
    stub._project_file_handler = ProjectFileHandler()
    stub._kubectl_connector.get_secret = AsyncMock(return_value=None)
    stub.stored_secrets = {}
    stub._add_secret_to_create = lambda deployment, naam, secret: stub.stored_secrets.__setitem__(
        (deployment, naam), secret
    )
    return MinioManager(project_manager=stub)


async def assert_each_user_reaches_only_its_own_bucket(stored: dict[tuple[str, str], Any], *, step: str) -> None:
    """Na elke stap: elke gebruiker in zijn eigen bucket, en in geen andere."""
    buckets = {name: secret for (name, _kind), secret in stored.items()}
    for name, secret in buckets.items():
        mine = Mc(alias=f"zadlive-{name}", access_key=secret.access_key, secret_key=secret.secret_key)
        output, code = mine.put(secret.bucket_name, "eigen.txt", "inhoud")
        assert code == 0, f"{step}: {secret.access_key} schrijft niet in zijn eigen bucket: {output}"
        for other_name, other in buckets.items():
            if other_name == name:
                continue
            output, code = mine.put(other.bucket_name, "vreemd.txt", "inhoud")
            assert code != 0, f"{step}: {secret.access_key} schrijft in {other.bucket_name}: {output}"


async def test_two_deployments_each_reach_only_their_own_bucket(minio_manager: MinioManager) -> None:
    data = project_data()
    stub = minio_manager.project_manager

    for name in DEPLOYMENTS:
        await minio_manager.create_resources_for_deployment(
            data, next(entry for entry in data["deployments"] if entry["name"] == name)
        )
        await assert_each_user_reaches_only_its_own_bucket(stub.stored_secrets, step=f"na het aanmaken van {name}")

    for name in DEPLOYMENTS:
        secret = stub.stored_secrets[(name, "minio")]
        assert secret.bucket_name == generate_bucket_name(PROJECT, name, None)
        assert secret.access_key == generate_minio_username(PROJECT, name)

    # Opnieuw verwerken verandert niets, en de grenzen staan daarna nog.
    first = stub.stored_secrets[(DEPLOYMENTS[0], "minio")]
    await minio_manager.create_resources_for_deployment(
        data, next(entry for entry in data["deployments"] if entry["name"] == DEPLOYMENTS[0])
    )
    await assert_each_user_reaches_only_its_own_bucket(stub.stored_secrets, step="na het opnieuw verwerken")
    assert stub.stored_secrets[(DEPLOYMENTS[0], "minio")].bucket_name == first.bucket_name


async def test_deleting_a_deployment_leaves_no_bucket_or_user_behind(minio_manager: MinioManager) -> None:
    data = project_data()
    stub = minio_manager.project_manager
    for name in DEPLOYMENTS:
        await minio_manager.create_resources_for_deployment(
            data, next(entry for entry in data["deployments"] if entry["name"] == name)
        )

    gone = stub.stored_secrets[(DEPLOYMENTS[0], "minio")]
    result = await minio_manager.delete_resources_for_deployment(
        data, next(entry for entry in data["deployments"] if entry["name"] == DEPLOYMENTS[0])
    )
    assert result["success"] is True, result

    readback = admin_mc()
    assert gone.bucket_name not in {
        str(entry.get("key", "")).strip("/") for entry in readback.json_lines("ls", "--json", readback.alias)
    }
    assert gone.access_key not in {
        entry.get("accessKey") for entry in readback.json_lines("admin", "user", "list", readback.alias, "--json")
    }

    del stub.stored_secrets[(DEPLOYMENTS[0], "minio")]
    await assert_each_user_reaches_only_its_own_bucket(stub.stored_secrets, step="na het verwijderen van de eerste")
