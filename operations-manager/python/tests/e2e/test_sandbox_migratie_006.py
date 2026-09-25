"""Migratie 006 en zijn backfill, tegen een database met echte historie.

De migratie zelf is op een LEGE testdatabase getoetst. Wat daar niet uit blijkt is of de
backfill het op echte rijen ook doet: die leest `async_tasks`, een tabel met payloads van
uiteenlopende vorm (soms zonder `rollout`, soms zonder payload), en een SQL-statement dat
op een lege tabel vrolijk doorloopt kan op zulke rijen alsnog omvallen.

Deze toets draait de backfill van `006_add_project_reconciliation.py` LETTERLIJK, maar
tegen een eigen tabel in plaats van tegen `project_reconciliation`: de sandbox-database is
gedeeld met alles wat er verder op dit cluster draait, en een toets hoort de meting van een
ander niet te verstoren. De bron is wel de echte: een momentopname van `async_tasks` van
dit cluster, en waarom die opname er tussen zit staat bij de fixture ``taken``.

Drie dingen worden gemeten, en het derde is de reden dat de rest er staat:

1. de backfill loopt zonder fout over de echte rijen uit `async_tasks`;
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

import json
import logging
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from tests.e2e.helpers import cluster

if TYPE_CHECKING:
    from collections.abc import Generator

logger = logging.getLogger(__name__)

# Eigen tijdsbudget, en waarom dat moet staat in `features/e2e-sandbox-tests.md`.
# Elk statement hieronder gaat via `run_psql`, dat bij een pod die zijn uitvoer verliest
# drie pogingen van 300s doet; de zwaarste toets stuurt er vijf achter elkaar.
pytestmark = [pytest.mark.e2e, pytest.mark.sandbox, pytest.mark.timeout(1800)]

_NAMESPACE = "rig-system"
_DB_HOST = "rig-db-rw"
_DB_NAAM = "operations_manager"
_ADMIN_SECRET = "postgres-admin-credentials"

#: De backfill uit de migratie, letterlijk. Doeltabel en brontabel zijn een parameter: de
#: toets schrijft niet in `project_reconciliation`, en hij leest uit een MOMENTOPNAME van
#: `async_tasks` (zie de fixture ``taken``). Loopt deze tekst verder uiteen met de migratie,
#: dan meet de toets iets anders dan er draait; ``test_de_backfill_is_die_uit_de_migratie``
#: hieronder bewaakt dat.
_BACKFILL = """
INSERT INTO {tabel} (project_name, deployment_name, reconciled_at)
SELECT project_name, NULL, max(coalesce(started_at, completed_at))
FROM {bron}
WHERE status = 'completed'
  AND task_type IN ('refresh_project', 'delete_component')
  AND (payload ->> 'rollout') IS DISTINCT FROM 'false'
GROUP BY project_name
HAVING max(coalesce(started_at, completed_at)) IS NOT NULL
ON CONFLICT (project_name, COALESCE(deployment_name, '')) DO NOTHING;
"""

#: De oude meting, in dezelfde woorden als de constante die verdween.
_OUDE_METING = """
SELECT count(DISTINCT project_name) FROM {bron}
WHERE status = 'completed'
  AND task_type IN ('refresh_project', 'delete_component')
  AND (payload ->> 'rollout') IS DISTINCT FROM 'false';
"""


#: Elke tabel die deze module aanmaakt begint hiermee. Een gang die halverwege stopt (een
#: ctrl-c, een pytest-timeout) slaat de ``finally`` over, en dan blijft zo'n tabel in de
#: GEDEELDE database van dit cluster staan. Daarom ruimt de fixture ``taken`` eerst op wat een
#: vorige gang liet liggen: er draait per keer EEN pr op deze sandbox, dus er is geen tweede
#: gang die deze tabellen op dat moment nodig heeft.
_EIGEN_PREFIX = "rc227_"

#: Het blok draagt een BENOEMDE dollar-quote en niet `$$`: de args van een pod gaan langs de
#: variabele-expansie van Kubernetes, en die leest `$$` als een ontsnapte `$`. Postgres krijgt
#: dan `DO $` en antwoordt met `syntax error at or near "$"`. Nagemeten via ``cluster.run_psql``.
_OPRUIMEN = f"""
DO $leegmaken$
DECLARE tabel text;
BEGIN
  FOR tabel IN
    SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename LIKE '{_EIGEN_PREFIX}%'
  LOOP
    EXECUTE format('DROP TABLE IF EXISTS public.%I', tabel);
  END LOOP;
END $leegmaken$;
"""


@pytest.fixture(scope="module")
def db_wachtwoord(sandbox_url: str) -> str:
    """Het admin-wachtwoord van de sandbox-database.

    ``sandbox_url`` wordt hier niet gebruikt maar wel gevraagd: die fixture is de poort die
    de hele suite dicht houdt zonder ``E2E_BASE_URL``. Zonder hem draaien deze toetsen op
    welk cluster ``kubectl`` ook maar aanwijst, en dat is precies wat de afspraak voorkomt.

    Het lezen gaat via ``cluster.secret_values``, dat het kubectl-commando, de jsonpath en de
    base64 al doet. Hier stond een eigen wrapper naast, en dat is precies de dubbeling die
    deze repo niet wil.
    """
    # OSError staat erbij voor de machine zonder kubectl: dan is er geen cluster om op te
    # meten, en dat is een skip en geen fout.
    try:
        return cluster.secret_values(_NAMESPACE, _ADMIN_SECRET)["password"]
    except (RuntimeError, KeyError, OSError) as fout:
        pytest.skip(f"geen toegang tot het wachtwoord in {_ADMIN_SECRET} ({_NAMESPACE}): {fout}")


def _psql(wachtwoord: str, sql: str, *, timeout: float = 300.0) -> tuple[int, str]:
    """SQL tegen de sandbox-database, met de vaste gegevens van deze server.

    De afdichting tegen een pod die zijn uitvoer verliest zit in ``cluster.run_psql``, en
    daar staat ook de eis die dat aan een statement stelt: het moet een tweede keer kunnen.
    Daarom staat er hieronder `DROP TABLE IF EXISTS` voor elke `CREATE TABLE`, en op elke
    INSERT de `ON CONFLICT` die de migratie zelf ook draagt.
    """
    return cluster.run_psql(
        sql,
        host=_DB_HOST,
        user="postgres",
        database=_DB_NAAM,
        password=wachtwoord,
        namespace=_NAMESPACE,
        timeout=timeout,
    )


@pytest.fixture(scope="module")
def taken(db_wachtwoord: str) -> Generator[str]:
    """Een momentopname van `async_tasks`, en dat is de reparatie van een wankele toets.

    De rijen moeten echt zijn, maar ze mogen niet BEWEGEN: dit is de database van een draaiend
    cluster. Gemeten over drie gangen op dezelfde dag leverde de backfill 1 rij, daarna 0, daarna
    0, want een projectverwijdering haalt taken weg en een nieuwe taak zet er een bij. Twee
    metingen die niet tegelijk gebeuren kunnen daardoor uit elkaar lopen zonder dat er iets mis
    is: de idempotentietoets zou dat lezen als "de tweede backfill voegde rijen toe", en de
    vergelijking met de oude meting leest twee verschillende momenten.
    """
    opgeruimd, uit = _psql(db_wachtwoord, _OPRUIMEN)
    assert opgeruimd == 0, f"de tabellen van een vorige gang konden niet worden opgeruimd: {uit}"

    naam = f"{_EIGEN_PREFIX}taken_{uuid.uuid4().hex[:8]}"
    code, uit = _psql(db_wachtwoord, f"DROP TABLE IF EXISTS {naam}; CREATE TABLE {naam} AS SELECT * FROM async_tasks;")
    assert code == 0, f"de momentopname van async_tasks kon niet worden gemaakt: {uit}"
    try:
        yield naam
    finally:
        _psql(db_wachtwoord, f"DROP TABLE IF EXISTS {naam};")


def _getallen(uitvoer: str) -> list[int]:
    """De cijferregels uit de psql-uitvoer; `kubectl run --rm` plakt er zijn eigen regel bij."""
    return [int(regel.strip()) for regel in uitvoer.splitlines() if regel.strip().lstrip("-").isdigit()]


def test_de_backfill_is_die_uit_de_migratie(sandbox_url: str) -> None:
    """De tekst hierboven moet die van de migratie zijn, anders meet de rest niets.

    Leest alleen van schijf en heeft geen cluster nodig, maar vraagt toch ``sandbox_url``:
    zo heeft deze module EEN regel over wanneer hij draait, in plaats van een toets die
    stilletjes meedoet in een gewone unittestronde.

    Vergeleken op het HELE blok en niet op een lijst kenmerkende regels. Die lijst stond er
    eerst en keek of vijf regels in beide teksten voorkwamen, en daar glipt een regel die in
    de migratie BIJKOMT gewoon langs: met `AND project_name NOT LIKE ...` erbij bleef deze
    toets groen terwijl de drie toetsen hieronder een ander statement meten dan er draait.
    De vergelijking gaat over genormaliseerde witruimte, want de migratie laat het statement
    inspringen; de twee parameters staan op de waarden die de migratie zelf gebruikt, dus
    juist het verschil dat met opzet bestaat (een eigen doeltabel en brontabel) telt niet mee.
    """
    migratie = Path(__file__).resolve().parents[2] / "opi/migrations/versions/006_add_project_reconciliation.py"
    bron = " ".join(migratie.read_text().split())
    verwacht = " ".join(_BACKFILL.format(tabel="project_reconciliation", bron="async_tasks").split())

    # Alleen het stuk rond de INSERT in de melding: het hele genormaliseerde bestand maakt de
    # regel onleesbaar, en het verschil zit per definitie in dit statement.
    kern = bron[max(bron.find("INSERT INTO project_reconciliation"), 0) :][:700]
    assert verwacht in bron, (
        f"het statement in {migratie.name} is niet (meer) het statement dat deze toets draait.\n"
        f"deze toets: {verwacht}\nde migratie: {kern}"
    )


def test_de_database_draagt_echte_historie(db_wachtwoord: str, taken: str) -> None:
    """Zonder rijen in `async_tasks` meet alles hieronder een lege tabel, net als de unittests."""
    code, uit = _psql(db_wachtwoord, f"SELECT count(*) FROM {taken};")
    assert code == 0, f"de momentopname van async_tasks is niet te bevragen: {uit}"

    aantallen = _getallen(uit)
    assert aantallen, f"geen telling terug: {uit}"
    logger.info("async_tasks bevat %d rijen", aantallen[0])
    assert aantallen[0] > 0, "async_tasks is leeg; deze sandbox draagt geen historie om de backfill op te meten"


def test_de_backfill_loopt_over_de_echte_taken_en_is_idempotent(db_wachtwoord: str, taken: str) -> None:
    """De kern: het statement uit de migratie, twee keer, op de echte rijen.

    De eerste run meet of hij over echte payloads heen komt (rijen zonder `rollout`, rijen
    zonder payload). De tweede meet de `ON CONFLICT`, en daarmee of de unieke index op
    `COALESCE(deployment_name, '')` de projectbrede rij echt uniek houdt: zonder die
    COALESCE laat Postgres twee rijen met `deployment_name IS NULL` gewoon naast elkaar
    staan en telt de tweede run dubbel.
    """
    tabel = f"{_EIGEN_PREFIX}backfill_{uuid.uuid4().hex[:8]}"
    opzet = f"""
        DROP TABLE IF EXISTS {tabel};
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
        code, uit = _psql(db_wachtwoord, _BACKFILL.format(tabel=tabel, bron=taken) + f"SELECT count(*) FROM {tabel};")
        assert code == 0, f"de backfill viel om op de echte rijen uit async_tasks: {uit}"
        na_een = _getallen(uit)
        assert na_een, f"geen telling na de eerste backfill: {uit}"

        code, uit = _psql(db_wachtwoord, _BACKFILL.format(tabel=tabel, bron=taken) + f"SELECT count(*) FROM {tabel};")
        assert code == 0, f"de tweede backfill viel om: {uit}"
        na_twee = _getallen(uit)
        assert na_twee, f"geen telling na de tweede backfill: {uit}"

        logger.info("backfill leverde %d rijen, tweede run %d", na_een[0], na_twee[0])
        assert na_twee[0] == na_een[0], (
            f"de tweede backfill voegde rijen toe ({na_een[0]} -> {na_twee[0]}); dan houdt de unieke index "
            "de projectbrede rij niet uniek en telt elke migratieherhaling dubbel"
        )

        # De twee runs hierboven raken de ON CONFLICT alleen als de echte `async_tasks`
        # toevallig een `refresh_project` of `delete_component` draagt; op deze database was
        # dat eerder nul, en dan vergelijkt de gelijkheid hierboven 0 met 0. Daarom de
        # NULL-scope er ook met een eigen rij naast, zodat die belofte niet van de data hangt.
        botsing = (
            f"INSERT INTO {tabel} (project_name, deployment_name, reconciled_at) "
            "VALUES ('rc227-proef', NULL, now()) "
            "ON CONFLICT (project_name, COALESCE(deployment_name, '')) DO NOTHING;"
        )
        code, uit = _psql(db_wachtwoord, botsing + botsing + f"SELECT count(*) FROM {tabel};")
        assert code == 0, f"de NULL-scope liet zich niet tweemaal aanbieden: {uit}"
        na_botsing = _getallen(uit)
        assert na_botsing, f"geen telling na de botsingsproef: {uit}"
        assert na_botsing[0] == na_twee[0] + 1, (
            f"dezelfde projectbrede rij landde niet precies een keer ({na_twee[0]} -> {na_botsing[0]}); "
            "zonder COALESCE in de index staan twee rijen met deployment_name IS NULL naast elkaar"
        )
    finally:
        _psql(db_wachtwoord, f"DROP TABLE IF EXISTS {tabel};")


def test_de_backfill_levert_precies_wat_de_oude_meting_zag(db_wachtwoord: str, taken: str) -> None:
    """De belofte van de backfill: "zodat de teller op het omschakelmoment hetzelfde leest".

    Dat is een gelijkheid tussen twee metingen over dezelfde rijen, en niet een aantal. Op
    een database zonder `refresh_project`- of `delete_component`-taken komen er nul rijen
    uit, en dan is nul het juiste antwoord; de toets valt om zodra de twee uit elkaar gaan
    lopen, en dat is wat hem een toets maakt in plaats van een telling.
    """
    tabel = f"{_EIGEN_PREFIX}gelijk_{uuid.uuid4().hex[:8]}"
    code, uit = _psql(
        db_wachtwoord,
        f"""
        DROP TABLE IF EXISTS {tabel};
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
            _BACKFILL.format(tabel=tabel, bron=taken)
            + _OUDE_METING.format(bron=taken)
            + f"SELECT count(*) FROM {tabel};",
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
    # Elk antwoord met zijn eigen label. Zonder die labels staan drie losse waarden in een
    # ongesorteerde bak en kan een assertie de waarde van een ANDERE oppakken: de controle op
    # de alembic-revisie ("006 of hoger") werd afgedekt door de "1" van de indextelling, en
    # bleef daardoor ook groen op revisie 003.
    code, uit = _psql(
        db_wachtwoord,
        "SELECT 'tabel=' || (to_regclass('public.project_reconciliation') IS NOT NULL);"
        "SELECT 'index=' || count(*) FROM pg_indexes WHERE indexname = 'idx_project_reconciliation_scope';"
        "SELECT 'indexdef=' || indexdef FROM pg_indexes WHERE indexname = 'idx_project_reconciliation_scope';"
        "SELECT 'revisie=' || version_num FROM alembic_version;",
    )
    assert code == 0, f"de controle viel om: {uit}"
    antwoord = dict(
        regel.strip().split("=", 1) for regel in uit.splitlines() if "=" in regel and not regel.startswith("pod ")
    )
    logger.info("stand op het cluster: %s", json.dumps(antwoord))

    # "true" en niet "t": een boolean die met `||` aan tekst geplakt wordt komt uitgeschreven mee.
    assert antwoord.get("tabel") == "true", f"project_reconciliation bestaat niet op dit cluster: {uit}"
    assert antwoord.get("index") == "1", f"idx_project_reconciliation_scope ontbreekt: {uit}"

    # De scherpste regel van de migratie, gemeten op de ECHTE index en niet op de kopie in
    # deze toets: zonder de COALESCE laat Postgres twee projectbrede rijen naast elkaar staan.
    assert "COALESCE" in antwoord.get("indexdef", "").upper(), (
        f"de index houdt de projectbrede rij niet uniek via COALESCE: {antwoord.get('indexdef')}"
    )

    # De revisies zijn met nullen opgevuld en even lang (001..006), dus dit vergelijkt goed.
    revisie = antwoord.get("revisie", "")
    assert revisie >= "006", f"alembic staat niet op 006 of hoger maar op {revisie!r}"
