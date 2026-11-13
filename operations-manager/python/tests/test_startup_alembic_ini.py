"""The boot finds the Alembic configuration it migrates with."""

from opi.runtime.startup.startup import ALEMBIC_INI


def test_the_alembic_ini_the_boot_migrates_with_exists() -> None:
    assert ALEMBIC_INI.is_file(), ALEMBIC_INI
