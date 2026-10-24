"""Real-Postgres tests for the ORM-backed UserAdminService (RC-5 persistence)."""

import pytest
from opi.services.user_admin_service import UserAdminService, _als_uuid
from sqlalchemy.exc import IntegrityError


def test_als_uuid_geeft_none_in_plaats_van_een_valueerror_bij_een_vreemde_tekst():
    """Faalconditie: ``uuid.UUID("niet-een-uuid")`` gooit een ValueError op elke pad-parameter
    die geen UUID is, en die bubbelt zonder deze guard door tot een 500 op de route."""
    assert _als_uuid("niet-een-uuid") is None
    assert str(_als_uuid("550e8400-e29b-41d4-a716-446655440000")) == "550e8400-e29b-41d4-a716-446655440000"


async def test_create_get_and_get_by_email(orm_db):
    svc = UserAdminService()
    created = await svc.create_user("a@example.nl", "Alice")
    assert created["email"] == "a@example.nl"
    assert created["full_name"] == "Alice"
    assert created["id"]
    assert created["created_at"]
    assert created["updated_at"]

    assert (await svc.get_user(created["id"]))["email"] == "a@example.nl"
    assert (await svc.get_user_by_email("a@example.nl"))["full_name"] == "Alice"
    assert await svc.get_user_by_email("missing@example.nl") is None


async def test_list_users_ordered_by_full_name(orm_db):
    svc = UserAdminService()
    await svc.create_user("b@example.nl", "Bob")
    await svc.create_user("a@example.nl", "Alice")
    assert [u["full_name"] for u in await svc.list_users()] == ["Alice", "Bob"]


async def test_update_user(orm_db):
    svc = UserAdminService()
    created = await svc.create_user("a@example.nl", "Alice")
    updated = await svc.update_user(created["id"], "alice2@example.nl", "Alice Two")
    assert updated["email"] == "alice2@example.nl"
    assert updated["full_name"] == "Alice Two"
    assert await svc.update_user("00000000-0000-0000-0000-000000000000", "x@x.nl", "X") is None


async def test_delete_user(orm_db):
    svc = UserAdminService()
    created = await svc.create_user("a@example.nl", "Alice")
    assert await svc.delete_user(created["id"]) is True
    assert await svc.delete_user(created["id"]) is False
    assert await svc.get_user(created["id"]) is None


async def test_duplicate_email_raises_integrity_error(orm_db):
    svc = UserAdminService()
    await svc.create_user("dup@example.nl", "First")
    with pytest.raises(IntegrityError):
        await svc.create_user("dup@example.nl", "Second")


async def test_een_id_die_geen_uuid_is_geeft_niet_gevonden_in_plaats_van_een_fout(orm_db):
    """Faalconditie: een pad-parameter die geen UUID is laat de route op een 500 vallen.

    ``uuid.UUID(user_id)`` gooit een ``ValueError`` op elke tekst die geen UUID is. Een
    gebruiker-id komt rechtstreeks uit het pad van de API, dus een client kan hier zomaar iets
    neerzetten; dat hoort een 404 te worden, geen onafgehandelde crash.
    """
    svc = UserAdminService()
    assert await svc.get_user("niet-een-uuid") is None
    assert await svc.update_user("niet-een-uuid", "x@x.nl", "X") is None
    assert await svc.delete_user("niet-een-uuid") is False
