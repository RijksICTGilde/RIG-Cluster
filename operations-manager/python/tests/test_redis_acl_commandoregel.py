"""Een projectuser op de gedeelde Redis mag data, geen beheer (RC-409).

Het sleutelpatroon van RC-338 beperkt WELKE sleutels een datacommando raakt, en niet
welke commando's er zijn: met ``+@all`` erbij kon een projectuser ACL SETUSER, FLUSHALL,
CONFIG en KEYS op de gedeelde instantie. Deze toetsen houden de commandoregel vast en de
eigenschap dat elke verwerking hem opnieuw zet.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.manager.redis_manager import ACL_COMMAND_RULES, RedisManager
from opi.utils.secrets import RedisSecret

ADMIN_PASSWORD = "admin1234"


async def _setuser_argumenten(key_prefix: str) -> list[str]:
    """De argumenten waarmee ``_create_acl_user`` ACL SETUSER stuurt."""
    verzonden: list[tuple[str, ...]] = []

    async def _command(_reader: Any, _writer: Any, *args: str) -> str:
        verzonden.append(args)
        return "+OK"

    writer = MagicMock()
    writer.wait_closed = AsyncMock()
    with (
        patch("asyncio.open_connection", AsyncMock(return_value=(MagicMock(), writer))),
        patch.object(RedisManager, "_send_redis_command", AsyncMock(side_effect=_command)),
    ):
        await RedisManager._create_acl_user("redis", 6379, ADMIN_PASSWORD, "p1-productie", "geheim", key_prefix)

    setuser = [args for args in verzonden if args[:2] == ("ACL", "SETUSER")]
    assert len(setuser) == 1, f"een aanmaak stuurt een SETUSER, niet {len(setuser)}"
    return list(setuser[0])


@pytest.mark.parametrize("key_prefix", ["productie-p1", ""])
async def test_de_user_houdt_alle_beheercommandos_buiten(key_prefix: str) -> None:
    """Ook de goedgekeurde opt-out zonder prefix (RC-338) krijgt geen beheer."""
    argumenten = await _setuser_argumenten(key_prefix)

    assert "+@all" in argumenten
    assert argumenten.index("-@admin") > argumenten.index("+@all")
    assert argumenten.index("-@dangerous") > argumenten.index("+@all")


async def test_wat_een_client_bij_het_verbinden_stuurt_blijft_mogen() -> None:
    """INFO en CLIENT SETNAME zitten in @dangerous, en veel clients sturen ze."""
    argumenten = await _setuser_argumenten("productie-p1")

    for uitzondering in ("+info", "+client|setname"):
        assert argumenten.index(uitzondering) > argumenten.index("-@dangerous")


async def test_de_regel_staat_een_keer_in_de_argumenten() -> None:
    argumenten = await _setuser_argumenten("productie-p1")

    assert argumenten[-len(ACL_COMMAND_RULES) :] == list(ACL_COMMAND_RULES)


async def test_een_bestaande_user_krijgt_de_regel_bij_elke_verwerking() -> None:
    """Geen vergelijking op de commandoset, en dat is bewust.

    Redis geeft de regel van een user alleen terug via ACL GETUSER, en SETUSER is
    idempotent. Elke verwerking zet hem daarom opnieuw, zodat een user die onder een
    oudere regel is aangemaakt meekomt zonder dat iemand hem hoeft te vinden.
    """
    from opi.manager.redis_manager import RedisManager as Manager

    bestaand = RedisSecret(
        host="redis",
        port=6379,
        username="p1-productie",
        password="bestaand",
        key_prefix="productie-p1",
    )

    project_manager = MagicMock()
    project_manager.get_name = AsyncMock(return_value="p1")
    project_manager.get_progress_manager = MagicMock(return_value=None)
    project_manager._add_secret_to_create = MagicMock()

    manager = Manager(project_manager)
    manager._deployment_uses_redis = AsyncMock(return_value=True)
    manager._project_uses_namespace_redis = MagicMock(return_value=False)
    manager._test_redis_connection = AsyncMock(return_value=True)
    manager._get_existing_redis_credentials_from_k8s = AsyncMock(return_value=bestaand)
    manager._create_acl_user = AsyncMock()

    project_data = {
        "name": "p1",
        "services": ["redis"],
        "deployments": [{"name": "productie", "cluster": "sandboxed-local"}],
    }
    await manager.create_resources_for_deployment(project_data, {"name": "productie", "cluster": "sandboxed-local"})

    manager._create_acl_user.assert_awaited_once()
    # Hetzelfde wachtwoord: de regel vernieuwen is geen rotatie.
    assert manager._create_acl_user.await_args.args[4] == bestaand.password
