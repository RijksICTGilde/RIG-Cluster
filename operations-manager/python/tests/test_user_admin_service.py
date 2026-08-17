"""Real-Postgres tests for the ORM-backed UserAdminService (RC-5 persistence)."""

import pytest
from opi.services.user_admin_service import UserAdminService
from sqlalchemy.exc import IntegrityError


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
