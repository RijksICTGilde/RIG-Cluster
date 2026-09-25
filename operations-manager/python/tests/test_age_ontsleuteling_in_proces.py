"""Ontsleuteling loopt in het proces, niet via een fork van het age-binary."""

import logging
import shutil
import subprocess
import tempfile
from unittest.mock import patch

import pyrage
import pytest
from opi.core.metrics import _connector_subprocess_calls
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

#: De vorm van de waarden die hier echt door lopen (een GitHub-PAT), zonder er een te zijn.
GEHEIM = "ghp_" + "z" * 36


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
    async def test_blok_van_het_binary_opent_in_het_proces(self, age_keypair):
        """Wat het binary schreef moet de nieuwe weg lezen: alle bestaande bestanden zijn zo gemaakt."""
        prive, publiek = age_keypair
        blok = _binary_versleutelt(KLARE_TEKST, publiek)

        assert is_age_encrypted(blok)
        assert await decrypt_age_content(blok, prive) == KLARE_TEKST
        assert decrypt_age_content_sync(blok, prive) == KLARE_TEKST

    @pytest.mark.asyncio
    async def test_blok_van_opi_blijft_leesbaar_voor_het_binary(self, age_keypair):
        """OPI schrijft nog met het binary; de heenweg en de terugweg blijven op elkaar passen."""
        prive, publiek = age_keypair
        blok = await encrypt_age_content(KLARE_TEKST, publiek)

        assert is_age_encrypted(blok), "de opgeslagen vorm is armored; --armor hoort op de versleutelkant te blijven"
        gelezen = _binary_ontsleutelt(blok.encode(), prive)
        assert gelezen.returncode == 0, gelezen.stderr.decode()
        assert gelezen.stdout.decode() == KLARE_TEKST
        assert await decrypt_age_content(blok, prive) == KLARE_TEKST

    @pytest.mark.asyncio
    async def test_env_var_blok_met_niet_ascii_komt_teken_voor_teken_terug(self, age_keypair):
        """Niet-ascii loopt over ``decode("utf-8")``; het regeleinde aan het eind valt weg,
        want beide varianten strippen hun uitkomst, ook de oude.
        """
        prive, publiek = age_keypair
        blok = _binary_versleutelt("WELKOM=Groetjes uit Noord\nMUNT=\u20ac 12,50\nPAD=/tmp/caf\u00e9\n", publiek)

        assert (
            await decrypt_age_content(blok, prive) == "WELKOM=Groetjes uit Noord\nMUNT=\u20ac 12,50\nPAD=/tmp/caf\u00e9"
        )

    def test_blok_uit_de_bibliotheek_opent_in_het_binary(self, age_keypair):
        """De meting waar de versleutelkant later op kan rusten."""
        prive, publiek = age_keypair
        blok = pyrage.encrypt(KLARE_TEKST.encode(), [pyrage.x25519.Recipient.from_str(publiek)])

        gelezen = _binary_ontsleutelt(blok, prive)
        assert gelezen.returncode == 0, gelezen.stderr.decode()
        assert gelezen.stdout.decode() == KLARE_TEKST


class TestGeenSubprocessOpHetLeespad:
    @pytest.mark.asyncio
    async def test_ontsleutelen_start_geen_proces(self, age_keypair):
        prive, publiek = age_keypair
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
    async def test_prive_sleutel_gaat_niet_naar_schijf(self, age_keypair):
        prive, publiek = age_keypair
        blok = _binary_versleutelt(KLARE_TEKST, publiek)

        def ontploft(*args, **kwargs):
            raise AssertionError("de private sleutel mag niet naar een tempfile")

        with patch.object(tempfile, "NamedTemporaryFile", side_effect=ontploft):
            assert await decrypt_age_content(blok, prive) == KLARE_TEKST
            assert decrypt_age_content_sync(blok, prive) == KLARE_TEKST


class TestFoutgedrag:
    @pytest.mark.asyncio
    async def test_verkeerde_sleutel_faalt_luid(self, age_keypair, make_age_keypair):
        _prive, publiek = age_keypair
        andere_prive, _ = make_age_keypair()
        blok = _binary_versleutelt(KLARE_TEKST, publiek)

        with pytest.raises(Exception, match="Age decryption failed"):
            await decrypt_age_content(blok, andere_prive)

    @pytest.mark.asyncio
    async def test_onleesbare_invoer_faalt_luid(self, age_keypair):
        prive, _publiek = age_keypair

        with pytest.raises(Exception, match="Age decryption failed"):
            await decrypt_age_content("geen age-blok", prive)

    @pytest.mark.asyncio
    async def test_kapotte_sleutel_faalt_luid(self, age_keypair):
        _prive, publiek = age_keypair
        blok = _binary_versleutelt(KLARE_TEKST, publiek)

        with pytest.raises(Exception, match="Age decryption failed"):
            await decrypt_age_content(blok, "AGE-SECRET-KEY-GEENGELDIGESLEUTEL")

    @pytest.mark.asyncio
    async def test_ontbrekende_invoer_blijft_een_valuefout(self, age_keypair):
        prive, _publiek = age_keypair

        with pytest.raises(ValueError, match="Missing encrypted content or private key"):
            await decrypt_age_content("", prive)
        with pytest.raises(ValueError, match="Missing encrypted content or private key"):
            await decrypt_age_content("iets", "")

    def test_sync_geeft_none_bij_een_fout(self, age_keypair):
        _prive, publiek = age_keypair
        blok = _binary_versleutelt(KLARE_TEKST, publiek)

        assert decrypt_age_content_sync(blok, "AGE-SECRET-KEY-GEENGELDIGESLEUTEL") is None
        assert decrypt_age_content_sync("geen age-blok", "AGE-SECRET-KEY-GEENGELDIGESLEUTEL") is None
        assert decrypt_age_content_sync("", "iets") is None


class TestRandenVanDeInvoer:
    """De twee ``strip()``-aanroepen in ``_decrypt_in_process``: pyrage is strenger dan het binary."""

    @pytest.mark.asyncio
    async def test_sleutel_met_een_regeleinde_erachter_opent(self, age_keypair):
        """``age -d -i`` opende een sleutelbestand met een regeleinde erachter (gemeten: exit 0),
        en die vorm komt voor: de sleutel bereikt OPI via een omgevingsvariabele uit een
        k8s-secret. ``Identity.from_str`` weigert hem met ``IdentityError``.
        """
        prive, publiek = age_keypair
        blok = _binary_versleutelt(KLARE_TEKST, publiek)

        assert await decrypt_age_content(blok, prive + "\n") == KLARE_TEKST
        assert decrypt_age_content_sync(blok, prive + "\n") == KLARE_TEKST

    @pytest.mark.asyncio
    async def test_een_blok_zonder_inhoud_geeft_een_lege_tekst_terug(self, age_keypair):
        """Leeg blijft leeg in plaats van een fout te worden: ``decrypt_if_encrypted`` en
        ``decrypt_password_smart`` toetsen hierna alleen op ``None``.
        """
        prive, publiek = age_keypair
        blok = _binary_versleutelt("", publiek)

        assert await decrypt_age_content(blok, prive) == ""
        assert decrypt_age_content_sync(blok, prive) == ""

    @pytest.mark.asyncio
    async def test_blok_opent_in_elke_vorm_die_is_age_encrypted_accepteert(self, age_keypair):
        """``is_age_encrypted`` stript voor het de markers herkent, dus wat die poort doorlaat
        moet hierna ook opengaan; ``decrypt_tree`` zet die twee achter elkaar. Wijder dan het
        binary, dat op een blok met witruimte ervoor afketste met "unexpected intro".
        """
        prive, publiek = age_keypair
        omrand = "\n  " + _binary_versleutelt(KLARE_TEKST, publiek).strip() + "\n\n"

        assert is_age_encrypted(omrand)
        assert await decrypt_age_content(omrand, prive) == KLARE_TEKST
        assert decrypt_age_content_sync(omrand, prive) == KLARE_TEKST


class TestGeenGeheimInDeUitvoer:
    """Logregels en foutmeldingen staan in de CI-uitvoer van elke rode run, en een rode run
    is precies het moment waarop iemand anders meekijkt.
    """

    @pytest.mark.asyncio
    async def test_de_klare_tekst_komt_niet_in_de_logs(self, age_keypair, caplog):
        prive, publiek = age_keypair
        blok = _binary_versleutelt(GEHEIM, publiek)

        with caplog.at_level(logging.DEBUG, logger="opi.utils.age"):
            assert await decrypt_age_content(blok, prive) == GEHEIM
            assert decrypt_age_content_sync(blok, prive) == GEHEIM

        gelogd = "\n".join(r.getMessage() for r in caplog.records)
        assert GEHEIM not in gelogd
        assert prive not in gelogd

    @pytest.mark.asyncio
    async def test_een_mislukte_ontsleuteling_noemt_sleutel_noch_cijfertekst(
        self, age_keypair, make_age_keypair, caplog
    ):
        _prive, publiek = age_keypair
        andere, _ = make_age_keypair()
        blok = _binary_versleutelt(GEHEIM, publiek)

        with caplog.at_level(logging.DEBUG, logger="opi.utils.age"):
            with pytest.raises(Exception, match="Age decryption failed") as fout:
                await decrypt_age_content(blok, andere)
            assert decrypt_age_content_sync(blok, andere) is None

        uitvoer = str(fout.value) + "\n".join(r.getMessage() for r in caplog.records)
        assert andere not in uitvoer
        assert blok.strip() not in uitvoer


class TestDeProcesteller:
    """``opi_connector_subprocess_calls_total{connector="age"}`` blijft op het versleutelen staan
    en is tegelijk het bewijs voor deze wijziging: hij hoort niet meer op te lopen terwijl iemand
    detailpagina's opent. ``_connector_subprocess_calls`` is de dict waaruit die gauge wordt
    gevuld (``opi/core/metrics.py``).
    """

    @pytest.mark.asyncio
    async def test_versleutelen_telt_nog_mee_en_ontsleutelen_niet_meer(self, age_keypair):
        prive, publiek = age_keypair

        voor = _connector_subprocess_calls.get("age", 0)
        blok = await encrypt_age_content(KLARE_TEKST, publiek)
        na_versleutelen = _connector_subprocess_calls.get("age", 0)
        assert na_versleutelen == voor + 1, "versleutelen start een proces en hoort geteld te blijven"

        assert await decrypt_age_content(blok, prive) == KLARE_TEKST
        assert decrypt_age_content_sync(blok, prive) == KLARE_TEKST
        assert _connector_subprocess_calls.get("age", 0) == na_versleutelen, (
            "ontsleutelen start geen proces en hoort de procesteller niet te laten oplopen"
        )


class TestGeenCache:
    """Het plan sluit caching uit: een ontsleuteld geheim dat blijft liggen is een risico dat
    terugkomt voor tijdwinst die de omzetting zelf al geeft.
    """

    @pytest.mark.asyncio
    async def test_dezelfde_cijfertekst_wordt_elke_keer_opnieuw_ontsleuteld(self, age_keypair):
        prive, publiek = age_keypair
        blok = _binary_versleutelt(GEHEIM, publiek)

        with patch("pyrage.decrypt", wraps=pyrage.decrypt) as spion:
            assert await decrypt_age_content(blok, prive) == GEHEIM
            assert await decrypt_age_content(blok, prive) == GEHEIM
            assert decrypt_age_content_sync(blok, prive) == GEHEIM

        assert spion.call_count == 3

    @pytest.mark.asyncio
    async def test_een_geslaagde_ontsleuteling_opent_hetzelfde_blok_niet_voor_een_andere_sleutel(
        self, age_keypair, make_age_keypair
    ):
        """De vorm die een cache op de cijfertekst alleen zou verbergen: eerst met de goede
        sleutel, daarna met een sleutel die er geen recht op heeft.
        """
        prive, publiek = age_keypair
        andere, _ = make_age_keypair()
        blok = _binary_versleutelt(GEHEIM, publiek)

        assert await decrypt_age_content(blok, prive) == GEHEIM

        with pytest.raises(Exception, match="Age decryption failed"):
            await decrypt_age_content(blok, andere)
        assert decrypt_age_content_sync(blok, andere) is None
