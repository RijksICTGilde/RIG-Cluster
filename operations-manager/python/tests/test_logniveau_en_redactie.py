"""Het logniveau van de opi-logger en het redactiefilter, via een echte setup_logging.

De twee horen bij elkaar: LOG_LEVEL bepaalt wat er uit de applicatie komt, en het filter
op de handler is het vangnet voor een geheim dat toch in een bericht belandt. Beide zijn
pas bewezen als ze door de echte opbouw van setup_logging lopen, niet alleen als losse
functie, want de bedrading (niveau op de logger, filter op elke handler) is het gedrag.
"""

from __future__ import annotations

import contextlib
import io
import logging
import time
from typing import TYPE_CHECKING

import pytest
from opi.connectors.postgres import _summarize_sql_statement
from opi.utils.logging_config import setup_logging
from opi.utils.logging_redact import redact_text

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

AGE_SLEUTEL = "AGE-SECRET-KEY-1QQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQ"
# Middelste deel kort houden: secret-scan herkent een geldige JWT-header gevolgd door twee
# delen van 10+ tekens als een kubeconfig-token (zie tests/test_logging_redact.py).
JWT = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.kort.handtekening"


@pytest.fixture
def logstroom() -> Iterator[Callable[..., io.StringIO]]:
    """Draai setup_logging en lees mee op de stdout-handler; zet de logstaat daarna terug."""
    root = logging.getLogger()
    opi_logger = logging.getLogger("opi")
    uvicorn_access = logging.getLogger("uvicorn.access")
    bewaard = (root.handlers[:], root.level, opi_logger.level, uvicorn_access.filters[:])

    def start(level: str = "DEBUG") -> io.StringIO:
        setup_logging(level=level)
        buffer = io.StringIO()
        for handler in root.handlers:
            if isinstance(handler, logging.StreamHandler):
                handler.setStream(buffer)
        return buffer

    yield start

    root.handlers[:] = bewaard[0]
    root.setLevel(bewaard[1])
    opi_logger.setLevel(bewaard[2])
    uvicorn_access.filters[:] = bewaard[3]


class TestLogniveau:
    """LOG_LEVEL zet het niveau van de opi-logger; de handlers staan op DEBUG."""

    def test_info_onderdrukt_debug_van_opi(self, logstroom) -> None:
        buffer = logstroom("INFO")
        logging.getLogger("opi.x").debug("een debugregel")
        logging.getLogger("opi.x").info("een inforegel")
        uitvoer = buffer.getvalue()
        assert "een debugregel" not in uitvoer
        assert "een inforegel" in uitvoer

    def test_debug_laat_debug_door(self, logstroom) -> None:
        buffer = logstroom("DEBUG")
        logging.getLogger("opi.x").debug("een debugregel")
        assert "een debugregel" in buffer.getvalue()

    def test_een_onbekende_naam_valt_terug_op_info_met_een_waarschuwing(self, logstroom) -> None:
        # De waarschuwing komt uit setup_logging zelf, dus voordat de buffer eraan hangt.
        meldingen = io.StringIO()
        with contextlib.redirect_stderr(meldingen):
            buffer = logstroom("PRAATJES")
        logging.getLogger("opi.x").debug("een debugregel")
        logging.getLogger("opi.x").info("een inforegel")
        assert logging.getLogger("opi").level == logging.INFO
        assert "Unknown LOG_LEVEL" in meldingen.getvalue()
        assert "een debugregel" not in buffer.getvalue()
        assert "een inforegel" in buffer.getvalue()

    def test_een_niveaunaam_mag_spaties_en_kleine_letters_hebben(self, logstroom) -> None:
        logstroom(" info ")
        assert logging.getLogger("opi").level == logging.INFO


class TestHetRedactiefilter:
    """Een test per patroon uit _SECRET_PATTERNS, elk via de handler."""

    @pytest.mark.parametrize(
        ("regel", "geheim"),
        [
            ("Request headers: {'X-API-Key': 'supersleutel123'}", "supersleutel123"),
            (f"Request headers: {{'Authorization': 'Bearer {JWT}'}}", JWT),
            (f"Verzoek met token Bearer {JWT} afgewezen", JWT),
            ("DSN: postgresql://u@host/db?password=geheimwachtwoord", "geheimwachtwoord"),
            ('Payload: {"password": "geheimwachtwoord"}', "geheimwachtwoord"),
            (f"Sleutelbestand bevat {AGE_SLEUTEL} op regel 3", AGE_SLEUTEL),
            # Basic hoort bij het schema: anders bleef de base64-credential naast het
            # merkteken staan en meldde de regel een redactie die niet gebeurd was.
            ("Request headers: {'Authorization': 'Basic dXNlcjpnZWhlaW0='}", "dXNlcjpnZWhlaW0="),
            # SQL uit het projectbestand: postInitSQL mag een rol met wachtwoord aanmaken.
            ("CREATE ROLE app_user WITH LOGIN PASSWORD 'markerwachtwoord'", "markerwachtwoord"),
            ("ALTER USER app_user ENCRYPTED PASSWORD 'markerwachtwoord'", "markerwachtwoord"),
        ],
    )
    def test_maskeert_het_patroon(self, logstroom, regel: str, geheim: str) -> None:
        buffer = logstroom("DEBUG")
        logging.getLogger("opi.x").info(regel)
        uitvoer = buffer.getvalue()
        assert geheim not in uitvoer
        assert "***REDACTED***" in uitvoer

    @pytest.mark.parametrize(
        "regel",
        [
            "Password rotation finished",
            "Bearer authentication failed for route /projects",
            "Failed to decrypt registry password: invalid key",
            "Failed to decode session cookie: bad signature",
        ],
    )
    def test_laat_een_gewone_regel_heel(self, logstroom, regel: str) -> None:
        buffer = logstroom("DEBUG")
        logging.getLogger("opi.x").info(regel)
        uitvoer = buffer.getvalue()
        assert regel in uitvoer
        assert "***REDACTED***" not in uitvoer

    def test_maskeert_ook_een_geheim_dat_in_de_args_zit(self, logstroom) -> None:
        # %s-argumenten worden pas door de formatter samengevoegd, dus het filter moet op
        # het samengestelde bericht werken en de args daarna loslaten.
        buffer = logstroom("DEBUG")
        logging.getLogger("opi.x").info("Token: %s", f"Bearer {JWT}")
        uitvoer = buffer.getvalue()
        assert JWT not in uitvoer
        assert "***REDACTED***" in uitvoer

    def test_maskeert_ook_een_regel_van_een_bibliotheek(self, logstroom) -> None:
        # Het filter hangt op de handler, niet op de opi-logger, juist voor dit geval.
        buffer = logstroom("DEBUG")
        logging.getLogger("httpx").info(f"Verzoek met token Bearer {JWT}")
        uitvoer = buffer.getvalue()
        assert JWT not in uitvoer
        assert "***REDACTED***" in uitvoer


class TestDeTracebackEnDeSqlSamenvatting:
    """Een geheim in een traceback of in een SQL-statement komt niet in de uitvoer."""

    def test_een_geheim_in_een_exception_bericht_wordt_gemaskeerd(self, logstroom) -> None:
        buffer = logstroom("DEBUG")
        try:
            raise RuntimeError("verbinden mislukt: password='markerwachtwoord'")
        except RuntimeError:
            logging.getLogger("opi.x").exception("Init SQL mislukt")
        uitvoer = buffer.getvalue()
        assert "markerwachtwoord" not in uitvoer
        assert "Traceback" in uitvoer
        assert "***REDACTED***" in uitvoer

    def test_de_drie_geheimvormen_in_een_traceback_worden_alle_drie_gemaskeerd(self, logstroom) -> None:
        """Alle patronen werken op de geformatteerde traceback, niet alleen op het bericht."""
        buffer = logstroom("DEBUG")
        try:
            raise RuntimeError(f"faalde met {AGE_SLEUTEL}, Authorization: Bearer {JWT} en PASSWORD 'markerwachtwoord'")
        except RuntimeError:
            logging.getLogger("opi.x").exception("Init mislukt")

        uitvoer = buffer.getvalue()
        tracebackregel = next(regel for regel in uitvoer.splitlines() if regel.startswith("RuntimeError:"))
        assert AGE_SLEUTEL not in uitvoer
        assert JWT not in uitvoer
        assert "markerwachtwoord" not in uitvoer
        assert tracebackregel.count("***REDACTED***") == 3, tracebackregel

    def test_de_samenvatting_van_een_create_role_houdt_het_wachtwoord_buiten_de_log(self) -> None:
        # Het filter is het vangnet; de logregel zelf mag de SQL al niet meer bevatten.
        samenvatting = _summarize_sql_statement("CREATE ROLE app_user WITH LOGIN PASSWORD 'markerwachtwoord'")
        assert samenvatting == "CREATE ROLE app_user"

    @pytest.mark.parametrize(
        ("sql", "verwacht"),
        [
            ("ALTER USER app_user ENCRYPTED PASSWORD 'marker'", "ALTER USER app_user"),
            ("CREATE EXTENSION IF NOT EXISTS vector", "CREATE EXTENSION IF"),
            ("SET password='marker'", "SET"),
            ("'marker'", "unrecognised statement"),
        ],
    )
    def test_de_samenvatting_stopt_bij_het_eerste_gequoteerde_token(self, sql: str, verwacht: str) -> None:
        assert _summarize_sql_statement(sql) == verwacht

    def test_het_sql_patroon_krabbelt_niet_terug_op_een_witruimtereeks(self) -> None:
        """Het woord password plus een lange witruimtereeks zonder ``'`` erna blijft lineair.

        Met gewone ``*`` in plaats van possessieve quantifiers is dit patroon kwadratisch:
        32.000 tekens kostten bijna 10 seconden, en redact_text draait synchroon op de
        event loop en op ongekapte tekst (``subtasks[].error``, en ``error_message`` wordt
        pas NA de redactie afgekapt). Een halve seconde is ruim: lineair is dit onder 1 ms.
        """
        tekst = "password" + " " * 32000
        begin = time.perf_counter()
        resultaat = redact_text(tekst)
        duur = time.perf_counter() - begin
        assert resultaat == tekst
        assert duur < 0.5, f"redact_text deed {duur:.2f}s over {len(tekst)} tekens"
