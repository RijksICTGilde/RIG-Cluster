"""Ontsleuteling loopt in het proces, niet via een fork van het age-binary."""

import shutil
import subprocess
import tempfile
from unittest.mock import patch

import pyrage
import pytest
from opi.utils.age import (
    decrypt_age_content,
    decrypt_age_content_sync,
    encrypt_age_content,
    is_age_encrypted,
)

pytestmark = pytest.mark.skipif(
    shutil.which("age") is None or shutil.which("age-keygen") is None,
    reason="meet tegen het echte age-binary",
)

KLARE_TEKST = "een geheim\nmet twee regels"


@pytest.fixture
def sleutelpaar() -> tuple[str, str]:
    """Een sleutelpaar van age-keygen, dus precies de vorm die in projectbestanden staat."""
    uitvoer = subprocess.run(["age-keygen"], capture_output=True, text=True, check=True).stdout
    prive = next(r for r in uitvoer.splitlines() if r.startswith("AGE-SECRET-KEY-"))
    publiek = next(r for r in uitvoer.splitlines() if "public key:" in r).split(": ", 1)[1].strip()
    return publiek, prive


def _binary_versleutelt(klare_tekst: str, publieke_sleutel: str) -> str:
    return subprocess.run(
        ["age", "--armor", "-r", publieke_sleutel],
        input=klare_tekst,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _binary_ontsleutelt(blok: bytes, prive_sleutel: str) -> subprocess.CompletedProcess[bytes]:
    with tempfile.NamedTemporaryFile("w", suffix=".key") as sleutelbestand:
        sleutelbestand.write(prive_sleutel)
        sleutelbestand.flush()
        return subprocess.run(
            ["age", "-d", "-i", sleutelbestand.name],
            input=blok,
            capture_output=True,
            check=False,
        )


class TestUitwisselbaarMetHetBinary:
    @pytest.mark.asyncio
    async def test_blok_van_het_binary_opent_in_het_proces(self, sleutelpaar):
        """Wat het binary schreef moet de nieuwe weg lezen: alle bestaande bestanden zijn zo gemaakt."""
        publiek, prive = sleutelpaar
        blok = _binary_versleutelt(KLARE_TEKST, publiek)

        assert is_age_encrypted(blok)
        assert await decrypt_age_content(blok, prive) == KLARE_TEKST
        assert decrypt_age_content_sync(blok, prive) == KLARE_TEKST

    @pytest.mark.asyncio
    async def test_blok_van_opi_blijft_leesbaar_voor_het_binary(self, sleutelpaar):
        """OPI schrijft nog met het binary; de heenweg en de terugweg blijven op elkaar passen."""
        publiek, prive = sleutelpaar
        blok = await encrypt_age_content(KLARE_TEKST, publiek)

        gelezen = _binary_ontsleutelt(blok.encode(), prive)
        assert gelezen.returncode == 0, gelezen.stderr.decode()
        assert gelezen.stdout.decode() == KLARE_TEKST
        assert await decrypt_age_content(blok, prive) == KLARE_TEKST

    def test_blok_uit_de_bibliotheek_opent_in_het_binary(self, sleutelpaar):
        """De meting waar de versleutelkant later op kan rusten.

        Die kant is hier niet omgezet: pyrage 1.4.0 kent geen armor-uitvoer en de
        opgeslagen vorm is armored.
        """
        publiek, prive = sleutelpaar
        blok = pyrage.encrypt(KLARE_TEKST.encode(), [pyrage.x25519.Recipient.from_str(publiek)])

        gelezen = _binary_ontsleutelt(blok, prive)
        assert gelezen.returncode == 0, gelezen.stderr.decode()
        assert gelezen.stdout.decode() == KLARE_TEKST


class TestGeenSubprocessOpHetLeespad:
    @pytest.mark.asyncio
    async def test_ontsleutelen_start_geen_proces(self, sleutelpaar):
        publiek, prive = sleutelpaar
        blok = _binary_versleutelt(KLARE_TEKST, publiek)

        def ontploft(*args, **kwargs):
            raise AssertionError("ontsleutelen mag geen subprocess starten")

        with (
            patch("asyncio.create_subprocess_exec", side_effect=ontploft),
            patch("subprocess.run", side_effect=ontploft),
        ):
            assert await decrypt_age_content(blok, prive) == KLARE_TEKST
            assert decrypt_age_content_sync(blok, prive) == KLARE_TEKST

    @pytest.mark.asyncio
    async def test_prive_sleutel_gaat_niet_naar_schijf(self, sleutelpaar):
        publiek, prive = sleutelpaar
        blok = _binary_versleutelt(KLARE_TEKST, publiek)

        def ontploft(*args, **kwargs):
            raise AssertionError("de private sleutel mag niet naar een tempfile")

        with patch.object(tempfile, "NamedTemporaryFile", side_effect=ontploft):
            assert await decrypt_age_content(blok, prive) == KLARE_TEKST
            assert decrypt_age_content_sync(blok, prive) == KLARE_TEKST


class TestFoutgedrag:
    @pytest.mark.asyncio
    async def test_verkeerde_sleutel_faalt_luid(self, sleutelpaar):
        publiek, _ = sleutelpaar
        andere_prive = next(
            r
            for r in subprocess.run(["age-keygen"], capture_output=True, text=True, check=True).stdout.splitlines()
            if r.startswith("AGE-SECRET-KEY-")
        )
        blok = _binary_versleutelt(KLARE_TEKST, publiek)

        with pytest.raises(Exception, match="Age decryption failed"):
            await decrypt_age_content(blok, andere_prive)

    @pytest.mark.asyncio
    async def test_onleesbare_invoer_faalt_luid(self, sleutelpaar):
        _, prive = sleutelpaar

        with pytest.raises(Exception, match="Age decryption failed"):
            await decrypt_age_content("geen age-blok", prive)

    @pytest.mark.asyncio
    async def test_kapotte_sleutel_faalt_luid(self, sleutelpaar):
        publiek, _ = sleutelpaar
        blok = _binary_versleutelt(KLARE_TEKST, publiek)

        with pytest.raises(Exception, match="Age decryption failed"):
            await decrypt_age_content(blok, "AGE-SECRET-KEY-GEENGELDIGESLEUTEL")

    @pytest.mark.asyncio
    async def test_ontbrekende_invoer_blijft_een_valuefout(self, sleutelpaar):
        _, prive = sleutelpaar

        with pytest.raises(ValueError, match="Missing encrypted content or private key"):
            await decrypt_age_content("", prive)
        with pytest.raises(ValueError, match="Missing encrypted content or private key"):
            await decrypt_age_content("iets", "")

    def test_sync_geeft_none_bij_een_fout(self, sleutelpaar):
        publiek, _ = sleutelpaar
        blok = _binary_versleutelt(KLARE_TEKST, publiek)

        assert decrypt_age_content_sync(blok, "AGE-SECRET-KEY-GEENGELDIGESLEUTEL") is None
        assert decrypt_age_content_sync("geen age-blok", "AGE-SECRET-KEY-GEENGELDIGESLEUTEL") is None
        assert decrypt_age_content_sync("", "iets") is None
