"""Twee taken op een project stappen niet meer op elkaar (RC-166, deel B).

Op 31 augustus 2026 mislukte een uitrol van ``mpfb-8wh`` twee keer binnen een half uur,
en beide keren was de melding ``timed out after 300s waiting for sync`` terwijl er niets
mis was met de uitrol. Er liepen twee taken tegelijk op hetzelfde project: een
``configure_service`` zonder deploymentnaam (projectbreed) en een ``delete_deployment``
voor ``pr-244``. De claim-grendel vergeleek de kolom ``deployment_name`` letterlijk, dus
``NULL IS NOT DISTINCT FROM 'pr-244'`` was onwaar en allebei mochten draaien - waarna de
delete de Application weghaalde waar de andere taak net op ging wachten.

De scope van een taak staat nu in ``affects_deployments``, geschreven door ``scope_of()``
bij het aanmaken. Claimen vraagt of twee scopes OVERLAPPEN (kunnen ze elkaar in de weg
zitten), superseden vraagt of de een een SUPERSET van de ander is (doet die nieuwere taak
alles over): een kolom, twee predicaten.
"""

from datetime import UTC, datetime, timedelta

import pytest
from opi.core.async_task_service import AsyncTaskService
from opi.core.db import session_scope
from opi.services.persistence.async_tasks import AsyncTask

NU = datetime(2026, 8, 31, 9, 21, 10, tzinfo=UTC)


def _svc(cluster: str = "c1") -> AsyncTaskService:
    return AsyncTaskService(cluster=cluster)


async def _rij(
    *,
    project: str = "mpfb-8wh",
    scope: list[str] | None,
    status: str = "pending",
    task_type: str = "upsert_deployment",
    deployment: str | None = None,
    cluster: str = "c1",
    seconden: int = 0,
) -> str:
    """Een taakrij rechtstreeks, zodat de test de scope en de tijd zelf zet.

    Niet via ``create_task``: de matrix hieronder meet het overlappredicaat, niet de
    afleiding die de kolom vult. Die afleiding heeft zijn eigen test.
    """
    async with session_scope() as session:
        row = AsyncTask(
            task_type=task_type,
            project_name=project,
            deployment_name=deployment,
            cluster=cluster,
            payload={},
            status=status,
            affects_deployments=scope,
            created_at=NU + timedelta(seconds=seconden),
        )
        session.add(row)
        await session.flush()
        return str(row.id)


# ---------------------------------------------------------------------------
# 1. De overlapmatrix, in beide richtingen
# ---------------------------------------------------------------------------

OVERLAP_MATRIX = [
    (None, None, True),
    (None, ["a"], True),
    (["a"], ["a"], True),
    (["a"], ["b"], False),
    (["a", "b"], ["b", "c"], True),
]


@pytest.mark.parametrize(("draaiend", "wachtend", "overlapt"), OVERLAP_MATRIX)
async def test_overlap_blokkeert_het_claimen(orm_db, draaiend, wachtend, overlapt) -> None:
    await _rij(scope=draaiend, status="running", seconden=0)
    await _rij(scope=wachtend, status="pending", seconden=10)

    geclaimd = await _svc().claim_next_task(cluster="c1")

    assert (geclaimd is None) is overlapt, f"{draaiend} tegen {wachtend}"


@pytest.mark.parametrize(("draaiend", "wachtend", "overlapt"), OVERLAP_MATRIX)
async def test_overlap_is_symmetrisch(orm_db, draaiend, wachtend, overlapt) -> None:
    """Dezelfde matrix met de rollen omgedraaid.

    Asymmetrie is precies de fout die de oude grendel maakte: NULL naast 'pr-244' viel
    aan beide kanten weg, dus geen van de twee zag de ander.
    """
    await _rij(scope=wachtend, status="running", seconden=0)
    await _rij(scope=draaiend, status="pending", seconden=10)

    geclaimd = await _svc().claim_next_task(cluster="c1")

    assert (geclaimd is None) is overlapt, f"{wachtend} tegen {draaiend}"


# ---------------------------------------------------------------------------
# 2 en 3. Het incident, en de parallelliteit die moet blijven
# ---------------------------------------------------------------------------


async def test_mpfb_projectbrede_taak_houdt_een_delete_tegen(orm_db) -> None:
    """De situatie van 31 augustus: allebei geclaimd, en dat mag niet meer."""
    svc = _svc()
    configure = await svc.create_task(
        task_type="configure_service",
        project_name="mpfb-8wh",
        deployment_name=None,
        cluster="c1",
        payload={"service": "keycloak"},
    )
    verwijder = await svc.create_task(
        task_type="delete_deployment",
        project_name="mpfb-8wh",
        deployment_name="pr-244",
        cluster="c1",
        payload={"deployment_name": "pr-244"},
    )

    eerste = await svc.claim_next_task(cluster="c1")
    assert eerste is not None
    assert eerste["task_id"] == configure["task_id"], "de oudste taak gaat voor"

    assert await svc.claim_next_task(cluster="c1") is None, (
        "de delete overlapt met de draaiende projectbrede taak en moet wachten"
    )
    assert (await svc.get_task(verwijder["task_id"]))["status"] == "pending"


async def test_twee_deployments_van_een_project_lopen_nog_steeds_naast_elkaar(orm_db) -> None:
    """Geen bijvangst maar een eis: {pr-244} en {pr-250} overlappen niet."""
    svc = _svc()
    await svc.create_task(
        task_type="upsert_deployment",
        project_name="mpfb-8wh",
        deployment_name="pr-244",
        cluster="c1",
        payload={"image": "web:1"},
    )
    await svc.create_task(
        task_type="upsert_deployment",
        project_name="mpfb-8wh",
        deployment_name="pr-250",
        cluster="c1",
        payload={"image": "web:1"},
    )

    eerste = await svc.claim_next_task(cluster="c1")
    tweede = await svc.claim_next_task(cluster="c1")

    assert eerste is not None
    assert tweede is not None
    assert {eerste["deployment_name"], tweede["deployment_name"]} == {"pr-244", "pr-250"}


# ---------------------------------------------------------------------------
# 4 en 5. Eerst binnen, eerst gedraaid - met een uitzondering
# ---------------------------------------------------------------------------


async def test_een_oudere_wachtende_taak_gaat_voor(orm_db) -> None:
    """Zonder deze regel kan een stroom smalle taken een bredere taak blijven voorbijgaan.

    Hier draait iets op ``a``, wacht een oudere taak op ``{a, b}`` daarachter, en komt er
    een nieuwere taak voor ``{b}`` binnen. Die nieuwere overlapt niet met wat er draait,
    dus alleen de volgorde binnen het project houdt hem tegen.
    """
    await _rij(scope=["a"], status="running", seconden=0)
    await _rij(scope=["a", "b"], status="pending", seconden=10)
    await _rij(scope=["b"], status="pending", seconden=20)

    assert await _svc().claim_next_task(cluster="c1") is None


async def test_een_wachtende_backup_houdt_de_wachtrij_niet_op(orm_db) -> None:
    """Backups zijn wereldwijd afgeknepen; wachten zij, dan wacht het project niet mee.

    De nachtelijke sweep zet tientallen backups tegelijk in de wachtrij. Zou zo'n
    wachtende backup als blokkeerder meetellen, dan legt een limiet die niets met dit
    project te maken heeft de hele wachtrij van dat project stil.
    """
    await _rij(project="ander-project", scope=None, status="running", task_type="backup", seconden=0)
    await _rij(scope=None, status="pending", task_type="backup", seconden=10)
    await _rij(scope=["main"], status="pending", task_type="upsert_deployment", seconden=20)

    geclaimd = await _svc().claim_next_task(cluster="c1", type_concurrency_limits={"backup": 1})

    assert geclaimd is not None, "de backup is door zijn eigen limiet geblokkeerd, niet door dit project"
    assert geclaimd["task_type"] == "upsert_deployment"


async def test_een_draaiende_backup_blokkeert_wel(orm_db) -> None:
    """Je verwijdert geen deployment terwijl zijn backup draait."""
    await _rij(scope=["main"], status="running", task_type="backup", seconden=0)
    await _rij(scope=["main"], status="pending", task_type="delete_deployment", seconden=10)

    assert await _svc().claim_next_task(cluster="c1") is None


# ---------------------------------------------------------------------------
# 6. create_task is de enige schrijver van de kolom
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("task_type", "deployment", "payload", "verwacht"),
    [
        ("upsert_deployment", "pr-244", {}, ["pr-244"]),
        ("delete_deployment", "pr-244", {}, ["pr-244"]),
        ("add_component", None, {"deployment_names": ["productie", "acceptatie"]}, ["acceptatie", "productie"]),
        ("add_component", None, {}, None),
        ("configure_service", None, {}, None),
        # De drie projectbrede typen krijgen NULL, ook als er een deploymentnaam meekomt.
        ("refresh_project", "pr-244", {}, None),
        ("update_component", "pr-244", {}, None),
        ("add_service", "pr-244", {}, None),
        ("een_toekomstig_type", None, {}, None),
    ],
)
async def test_create_task_zet_de_scope(orm_db, task_type, deployment, payload, verwacht) -> None:
    rij = await _svc().create_task(
        task_type=task_type,
        project_name="mpfb-8wh",
        deployment_name=deployment,
        cluster="c1",
        payload=payload,
    )

    assert rij["affects_deployments"] == verwacht


# ---------------------------------------------------------------------------
# 7. Rijen van voor de migratie
# ---------------------------------------------------------------------------


async def test_een_rij_van_voor_de_migratie_is_projectbreed(orm_db) -> None:
    """NULL blokkeert maximaal, aan beide kanten van het predicaat.

    De migratie laat bestaande rijen op NULL staan, en dat is de veilige kant voor de
    handvol taken die tijdens een upgrade openstaan.
    """
    await _rij(scope=None, deployment="pr-244", status="running", seconden=0)
    await _rij(scope=["pr-250"], status="pending", seconden=10)
    assert await _svc().claim_next_task(cluster="c1") is None


async def test_een_rij_van_voor_de_migratie_wordt_ook_geblokkeerd(orm_db) -> None:
    await _rij(scope=["pr-250"], status="running", seconden=0)
    await _rij(scope=None, deployment="pr-244", status="pending", seconden=10)
    assert await _svc().claim_next_task(cluster="c1") is None


# ---------------------------------------------------------------------------
# 8. Een wachtende taak zegt waarop hij wacht
# ---------------------------------------------------------------------------


async def test_find_blocking_task_noemt_de_draaiende_taak(orm_db) -> None:
    draait = await _rij(scope=None, task_type="configure_service", status="running", seconden=0)
    await _rij(scope=["pr-244"], task_type="upsert_deployment", status="pending", seconden=5)
    wacht = await _rij(scope=["pr-244"], task_type="delete_deployment", status="pending", seconden=10)

    blokkeerder = await _svc().find_blocking_task(wacht)

    assert blokkeerder is not None
    assert blokkeerder["task_id"] == draait, "een draaiende taak gaat voor een wachtende als reden"
    assert blokkeerder["status"] == "running"


async def test_find_blocking_task_noemt_anders_de_oudste_wachtende(orm_db) -> None:
    oudste = await _rij(scope=None, task_type="configure_service", status="pending", seconden=0)
    await _rij(scope=["pr-244"], task_type="upsert_deployment", status="pending", seconden=5)
    wacht = await _rij(scope=["pr-244"], task_type="delete_deployment", status="pending", seconden=10)

    blokkeerder = await _svc().find_blocking_task(wacht)

    assert blokkeerder is not None
    assert blokkeerder["task_id"] == oudste
    assert blokkeerder["status"] == "pending"


async def test_find_blocking_task_is_none_als_de_taak_vrij_is(orm_db) -> None:
    await _rij(scope=["pr-250"], status="running", seconden=0)
    vrij = await _rij(scope=["pr-244"], status="pending", seconden=10)

    assert await _svc().find_blocking_task(vrij) is None


async def test_find_blocking_task_is_none_voor_een_taak_die_al_draait(orm_db) -> None:
    await _rij(scope=None, status="running", seconden=0)
    draait = await _rij(scope=["pr-244"], status="running", seconden=10)

    assert await _svc().find_blocking_task(draait) is None
