"""The ACME account storage in ``store.py``: the one piece no other test calls for real.

The lego run itself is tested against a faked ``store`` (``test_fundament_lego_aanroep.py``),
so the actual database round trip of :func:`store.write_acme_account` and
:func:`store.read_acme_account` is otherwise never executed against a real table.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from opi.core.config import settings
from opi.core.db import session_scope
from opi.fundament_registration import store
from opi.fundament_registration.store import FundamentAcmeAccount
from opi.utils.age import generate_age_key_pair

if TYPE_CHECKING:
    from collections.abc import Iterator

DIRECTORY = "https://acme-staging-v02.api.letsencrypt.org/directory"
FILES = {"account.json": '{"registration": {"uri": "https://acme/acct/1"}}', "account.key": "-----BEGIN KEY-----\n"}


@pytest.fixture
def platform_key() -> Iterator[None]:
    private_key, public_key = generate_age_key_pair()
    with (
        patch.object(settings, "SOPS_AGE_PUBLIC_KEY", public_key),
        patch.object(settings, "SOPS_AGE_PRIVATE_KEY", private_key),
    ):
        yield


async def test_an_unknown_directory_reads_as_empty(orm_db: Any) -> None:
    assert await store.read_acme_account(DIRECTORY) == {}


async def test_a_written_account_round_trips(orm_db: Any, platform_key: None) -> None:
    await store.write_acme_account(DIRECTORY, FILES)

    assert await store.read_acme_account(DIRECTORY) == FILES


async def test_the_files_sit_encrypted_in_the_table(orm_db: Any, platform_key: None) -> None:
    await store.write_acme_account(DIRECTORY, FILES)

    async with session_scope() as session:
        row = await session.get(FundamentAcmeAccount, DIRECTORY)

    assert row is not None
    assert row.files.startswith("base64+age:")
    assert "BEGIN KEY" not in row.files


async def test_a_second_write_replaces_the_first(orm_db: Any, platform_key: None) -> None:
    await store.write_acme_account(DIRECTORY, FILES)

    replacement = {"account.json": '{"registration": {"uri": "https://acme/acct/2"}}'}
    await store.write_acme_account(DIRECTORY, replacement)

    assert await store.read_acme_account(DIRECTORY) == replacement


async def test_two_directories_do_not_share_an_account(orm_db: Any, platform_key: None) -> None:
    other = "https://acme-v02.api.letsencrypt.org/directory"
    await store.write_acme_account(DIRECTORY, FILES)

    assert await store.read_acme_account(other) == {}
