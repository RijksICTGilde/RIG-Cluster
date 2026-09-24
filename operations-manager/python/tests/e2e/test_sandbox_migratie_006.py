"""Migratie 006 en zijn backfill, tegen een database met echte historie.

De migratie zelf is op een LEGE testdatabase getoetst. Wat daar niet uit blijkt is of de
backfill het op echte rijen ook doet: die leest `async_tasks`, een tabel met payloads van
uiteenlopende vorm (soms zonder `rollout`, soms zonder payload), en een SQL-statement dat
op een lege tabel vrolijk doorloopt kan op zulke rijen alsnog omvallen.

Deze toets draait de backfill van `006_add_project_reconciliation.py` LETTERLIJK, maar
tegen een eigen tabel in plaats van tegen `project_reconciliation`: de sandbox-database is
gedeeld met alles wat er verder op dit cluster draait, en een toets hoort de meting van een
ander niet te verstoren. De bron (`async_tasks`) is wel de echte.

Drie dingen worden gemeten, en het derde is de reden dat de rest er staat:

1. de backfill loopt zonder fout over de echte `async_tasks`;
2. hij is idempotent, en de unieke index doet zijn werk ook voor de NULL-scope
   (`COALESCE(deployment_name, '')`) - dat is de vondst die stil kan falen, want een gewone
   unieke index zou twee projectbrede rijen naast elkaar toelaten;
3. de uitkomst is gelijk aan de OUDE meting. Die stond in
   `ROLLOUT_CLEARING_TASK_TYPES = {"refresh_project", "delete_component"}` (verwijderd in
   9ec42982), en de belofte van de backfill is dat de teller op het omschakelmoment
   hetzelfde leest. Dat is een gelijkheid, niet een aantal: op een database zonder die twee
   taaktypes horen er nul rijen uit te komen, en dan is nul het goede antwoord.
"""

from __future__ import annotations

import base64
import json
import logging
import subprocess
import uuid
from pathlib import Path

import pytest

logger = logging.getLogger(__name__)

pytestmark = [pytest.mark.e2e, pytest.mark.sandbox]

_NAMESPACE = "rig-system"
_DB_HOST = "rig-db-rw"
_DB_NAAM = "operations_manager"
_ADMIN_SECRET = "postgres-admin-credentials"

#: De backfill uit de migratie, letterlijk. Alleen de doeltabel is een parameter, zodat de
#: toets niet in `project_reconciliation` schrijft. Loopt deze tekst uiteen met de migratie,
#: dan meet de toets iets anders dan er draait; ``test_de_backfill_is_die_uit_de_migratie``
#: hieronder bewaakt dat.
_BACKFILL = """
INSERT INTO {tabel} (project_name, deployment_name, reconciled_at)
SELECT project_name, NULL, max(coalesce(started_at, completed_at))
FROM async_tasks
WHERE status = 'completed'
  AND task_type IN ('refresh_project', 'delete_component')
  AND (payload ->> 'rollout') IS DISTINCT FROM 'false'
GROUP BY project_name
HAVING max(coalesce(started_at, completed_at)) IS NOT NULL
ON CONFLICT (project_name, COALESCE(deployment_name, '')) DO NOTHING;
"""

#: De oude meting, in dezelfde woorden als de constante die verdween.
_OUDE_METING = """
SELECT count(DISTINCT project_name) FROM async_tasks
WHERE status = 'completed'
  AND task_type IN ('refresh_project', 'delete_component')
  AND (payload ->> 'rollout') IS DISTINCT FROM 'false';
"""


def _kubectl(args: list[str], *, timeout: float = 60.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["kubectl", *args], capture_output=True, text=True, timeout=timeout, check=False)


@pytest.fixture(scope="module")
def db_wachtwoord() -> str:
    gelezen = _kubectl(["-n", _NAMESPACE, "get", "secret", _ADMIN_SECRET, "-o", "jsonpath={.data.password}"])
    if gelezen.returncode != 0 or not gelezen.stdout.strip():
        pytest.skip(f"geen toegang tot {_ADMIN_SECRET} in {_NAMESPACE}: {gelezen.stderr[:200]}")
    return base64.b64decode(gelezen.stdout).decode()


def _psql(wachtwoord: str, sql: str, *, timeout: float = 300.0) -> tuple[int, str]:
    resultaat = _kubectl(
        [
            "-n",
            _NAMESPACE,
            "run",
            f"psql-mig006-{uuid.uuid4().hex[:8]}",
            "--rm",
            "-i",
            "--restart=Never",
            "--image=postgres:16-alpine",
            "--env",
            f"PGPASSWORD={wachtwoord}",
            "--command",
            "--",
            "psql",
            "-h",
            _DB_HOST,
            "-U",
            "postgres",
            "-d",
            _DB_NAAM,
            "-v",
            "ON_ERROR_STOP=1",
            "-tAc",
            sql,
        ],
        timeout=timeout,
    )
    return resultaat.returncode, (resultaat.stdout + resultaat.stderr).strip()


def _getallen(uitvoer: str) -> list[int]:
    """De cijferregels uit de psql-uitvoer; `kubectl run --rm` plakt er zijn eigen regel bij."""
    return [int(regel.strip()) for regel in uitvoer.splitlines() if regel.strip().lstrip("-").isdigit()]


def test_de_backfill_is_die_uit_de_migratie() -> None:
    """De tekst hierboven moet die van de migratie zijn, anders meet de rest niets.

    Vergeleken op de kenmerkende regels en niet op het hele blok: de migratie schrijft in
    `project_reconciliation` en deze toets in een eigen tabel, dus die ene regel verschilt
    met opzet.
    """
    migratie = Path(__file__).resolve().parents[2] / "opi/migrations/versions/006_add_project_reconciliation.py"
    bron = migratie.read_text()

    for regel in (
        "SELECT project_name, NULL, max(coalesce(started_at, completed_at))",
        "AND task_type IN ('refresh_project', 'delete_component')",
        "AND (payload ->> 'rollout') IS DISTINCT FROM 'false'",
        "HAVING max(coalesce(started_at, completed_at)) IS NOT NULL",
        "ON CONFLICT (project_name, COALESCE(deployment_name, '')) DO NOTHING",
    ):
        assert regel in bron, f"de migratie kent deze regel niet (meer): {regel}"
        assert regel in _BACKFILL or regel in _BACKFILL.replace("\n", " "), f"deze toets kent hem niet: {regel}"


def test_de_database_draagt_echte_historie(db_wachtwoord: str) -> None:
    """Zonder rijen in `async_tasks` meet alles hieronder een lege tabel, net als de unittests."""
    code, uit = _psql(db_wachtwoord, "SELECT count(*) FROM async_tasks;")
    assert code == 0, f"async_tasks is niet te bevragen: {uit}"

    aantallen = _getallen(uit)
    assert aantallen, f"geen telling terug: {uit}"
    logger.info("async_tasks bevat %d rijen", aantallen[0])
    assert aantallen[0] > 0, "async_tasks is leeg; deze sandbox draagt geen historie om de backfill op te meten"


def test_de_backfill_loopt_over_de_echte_taken_en_is_idempotent(db_wachtwoord: str) -> None:
    """De kern: het statement uit de migratie, twee keer, op de echte `async_tasks`.

    De eerste run meet of hij over echte payloads heen komt (rijen zonder `rollout`, rijen
    zonder payload). De tweede meet de `ON CONFLICT`, en daarmee of de unieke index op
    `COALESCE(deployment_name, '')` de projectbrede rij echt uniek houdt: zonder die
    COALESCE laat Postgres twee rijen met `deployment_name IS NULL` gewoon naast elkaar
    staan en telt de tweede run dubbel.
    """
    tabel = f"rc227_backfill_{uuid.uuid4().hex[:8]}"
    opzet = f"""
        CREATE TABLE {tabel} (
            project_name VARCHAR(63) NOT NULL,
            deployment_name VARCHAR(63),
            reconciled_at TIMESTAMPTZ NOT NULL
        );
        CREATE UNIQUE INDEX idx_{tabel}_scope ON {tabel} (project_name, COALESCE(deployment_name, ''));
    """
    code, uit = _psql(db_wachtwoord, opzet)
    assert code == 0, f"de proeftabel kon niet worden aangemaakt: {uit}"

    try:
        code, uit = _psql(db_wachtwoord, _BACKFILL.format(tabel=tabel) + f"SELECT count(*) FROM {tabel};")
        assert code == 0, f"de backfill viel om op de echte async_tasks: {uit}"
        na_een = _getallen(uit)
        assert na_een, f"geen telling na de eerste backfill: {uit}"

        code, uit = _psql(db_wachtwoord, _BACKFILL.format(tabel=tabel) + f"SELECT count(*) FROM {tabel};")
        assert code == 0, f"de tweede backfill viel om: {uit}"
        na_twee = _getallen(uit)
        assert na_twee, f"geen telling na de tweede backfill: {uit}"

        logger.info("backfill leverde %d rijen, tweede run %d", na_een[0], na_twee[0])
        assert na_twee[0] == na_een[0], (
            f"de tweede backfill voegde rijen toe ({na_een[0]} -> {na_twee[0]}); dan houdt de unieke index "
            "de projectbrede rij niet uniek en telt elke migratieherhaling dubbel"
        )
    finally:
        _psql(db_wachtwoord, f"DROP TABLE IF EXISTS {tabel};")


def test_de_backfill_levert_precies_wat_de_oude_meting_zag(db_wachtwoord: str) -> None:
    """De belofte van de backfill: "zodat de teller op het omschakelmoment hetzelfde leest".

    Dat is een gelijkheid tussen twee metingen over dezelfde rijen, en niet een aantal. Op
    een database zonder `refresh_project`- of `delete_component`-taken komen er nul rijen
    uit, en dan is nul het juiste antwoord; de toets valt om zodra de twee uit elkaar gaan
    lopen, en dat is wat hem een toets maakt in plaats van een telling.
    """
    tabel = f"rc227_gelijk_{uuid.uuid4().hex[:8]}"
    code, uit = _psql(
        db_wachtwoord,
        f"""
        CREATE TABLE {tabel} (
            project_name VARCHAR(63) NOT NULL,
            deployment_name VARCHAR(63),
            reconciled_at TIMESTAMPTZ NOT NULL
        );
        CREATE UNIQUE INDEX idx_{tabel}_scope ON {tabel} (project_name, COALESCE(deployment_name, ''));
        """,
    )
    assert code == 0, f"de proeftabel kon niet worden aangemaakt: {uit}"

    try:
        code, uit = _psql(
            db_wachtwoord,
            _BACKFILL.format(tabel=tabel) + _OUDE_METING + f"SELECT count(*) FROM {tabel};",
        )
        assert code == 0, f"de vergelijking viel om: {uit}"

        getallen = _getallen(uit)
        assert len(getallen) >= 2, f"verwachtte twee tellingen, kreeg {getallen} uit: {uit}"
        oud, nieuw = getallen[0], getallen[1]
        logger.info("oude meting: %d projecten; backfill: %d rijen", oud, nieuw)

        assert nieuw == oud, (
            f"de backfill schreef {nieuw} rijen terwijl de oude meting {oud} projecten zag; "
            "dan leest de teller op het omschakelmoment iets anders dan ervoor"
        )

        # Een rij zonder tijdstip zou de leesregel meteen laten misgaan: -oneindig en NULL
        # gedragen zich verschillend in `projectbreed_at < T.completed_at`.
        code, uit = _psql(db_wachtwoord, f"SELECT count(*) FROM {tabel} WHERE reconciled_at IS NULL;")
        assert code == 0, f"de NULL-controle viel om: {uit}"
        leeg = _getallen(uit)
        assert leeg, f"geen telling voor de NULL-controle: {uit}"
        assert leeg[0] == 0, f"de backfill schreef rijen zonder reconciled_at: {uit}"
    finally:
        _psql(db_wachtwoord, f"DROP TABLE IF EXISTS {tabel};")


def test_de_echte_tabel_en_index_staan_er(db_wachtwoord: str) -> None:
    """De migratie is op dit cluster gedraaid: tabel, index en de revisie in alembic."""
    code, uit = _psql(
        db_wachtwoord,
        "SELECT to_regclass('public.project_reconciliation') IS NOT NULL;"
        "SELECT count(*) FROM pg_indexes WHERE indexname = 'idx_project_reconciliation_scope';"
        "SELECT version_num FROM alembic_version;",
    )
    assert code == 0, f"de controle viel om: {uit}"
    regels = [regel.strip() for regel in uit.splitlines() if regel.strip()]

    assert "t" in regels, f"project_reconciliation bestaat niet op dit cluster: {uit}"
    assert "1" in regels, f"idx_project_reconciliation_scope ontbreekt: {uit}"
    assert any(regel.startswith("006") or regel > "006" for regel in regels if regel[:1].isdigit()), (
        f"alembic staat niet op 006 of hoger: {json.dumps(regels)}"
    )
