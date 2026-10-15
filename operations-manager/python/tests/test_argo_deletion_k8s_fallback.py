"""Regression tests for ArgoConnector.wait_for_application_deletion.

The ArgoCD API returns an ambiguous 'permission denied' (PermissionError) to an
admin caller while it is merely stalled, for applications that still exist. The
old code treated that as "deleted", silently orphaning still-running deployments.

These tests pin the corrected behavior: ArgoCD is the fast path, but a
'permission denied' is never trusted as "deleted" - absence is confirmed against
the Kubernetes API (the honest source of truth) before reporting success.
"""

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.connectors.argo import ArgoConnector
from opi.connectors.kubectl import KubectlConnectionError, KubectlExecutionError


def _kubectl_returning(value: bool | None) -> MagicMock:
    """A stand-in kubectl connector whose k8s existence check returns ``value``."""
    kubectl = MagicMock()
    kubectl.argocd_application_exists = AsyncMock(return_value=value)
    return kubectl


@pytest.mark.asyncio
async def test_permission_denied_confirmed_absent_via_k8s_is_deleted() -> None:
    argo = ArgoConnector()
    kubectl = _kubectl_returning(False)  # k8s: NotFound -> truly gone
    with patch.object(argo, "application_exists", AsyncMock(side_effect=PermissionError("denied"))):
        result = await argo.wait_for_application_deletion("app", max_retries=1, kubectl_connector=kubectl)
    assert result is True
    kubectl.argocd_application_exists.assert_awaited()


@pytest.mark.asyncio
async def test_permission_denied_but_k8s_still_present_is_not_deleted() -> None:
    # The core regression: Argo says 'permission denied' but the CR still exists.
    # Old behavior returned True (false success); it must now return False.
    argo = ArgoConnector()
    kubectl = _kubectl_returning(True)  # k8s: still present
    with patch.object(argo, "application_exists", AsyncMock(side_effect=PermissionError("denied"))):
        result = await argo.wait_for_application_deletion("app", max_retries=1, kubectl_connector=kubectl)
    assert result is False


@pytest.mark.asyncio
async def test_permission_denied_k8s_unknown_is_not_deleted() -> None:
    argo = ArgoConnector()
    kubectl = _kubectl_returning(None)  # k8s: could not determine -> must not claim deleted
    with patch.object(argo, "application_exists", AsyncMock(side_effect=PermissionError("denied"))):
        result = await argo.wait_for_application_deletion("app", max_retries=1, kubectl_connector=kubectl)
    assert result is False


@pytest.mark.asyncio
async def test_permission_denied_without_kubectl_cannot_confirm() -> None:
    # Without a ground-truth source, an ambiguous answer cannot be confirmed.
    argo = ArgoConnector()
    with patch.object(argo, "application_exists", AsyncMock(side_effect=PermissionError("denied"))):
        result = await argo.wait_for_application_deletion("app", max_retries=1)
    assert result is False


@pytest.mark.asyncio
async def test_argo_reports_gone_confirmed_by_k8s_is_deleted() -> None:
    argo = ArgoConnector()
    kubectl = _kubectl_returning(False)
    with patch.object(argo, "application_exists", AsyncMock(return_value=False)):
        result = await argo.wait_for_application_deletion("app", max_retries=1, kubectl_connector=kubectl)
    assert result is True


# ---------------------------------------------------------------------------
# RC-238: traag is niet vastgelopen
# ---------------------------------------------------------------------------


def _kubectl_with_progress(progress: list[tuple[bool, int, bool] | None], gone_after: int) -> MagicMock:
    """Een kubectl-dubbel dat de cascade per poll laat zien.

    ``progress`` is wat ``argocd_application_deletion_progress`` per poll teruggeeft (de
    deletionTimestamp, het aantal ArgoCD-eigen resources dat in de doelnamespace over is, en
    of die inventaris compleet was), ``gone_after`` het aantal polls waarna de Application
    weg is.
    """
    kubectl = MagicMock()
    teller = {"n": 0}

    async def _exists(app_name: str, namespace: str | None = None) -> bool | None:
        return teller["n"] < gone_after

    async def _progress(
        app_name: str, namespace: str | None = None, resource_types: list[str] | None = None
    ) -> tuple[bool, int, bool] | None:
        n = teller["n"]
        teller["n"] += 1
        return progress[n] if n < len(progress) else progress[-1]

    kubectl.argocd_application_exists = AsyncMock(side_effect=_exists)
    kubectl.argocd_application_deletion_progress = AsyncMock(side_effect=_progress)
    kubectl.list_namespaced_resource_types = AsyncMock(return_value=["secrets", "deployments.apps"])
    return kubectl


@pytest.mark.asyncio
async def test_a_slow_cascade_with_shrinking_resources_ends_green() -> None:
    """Vijf minuten terminating met afnemende resources is een gezonde delete.

    Dit is het incident van 29 september: de cascade van drie Applications met elk 30+
    resources duurde langer dan het vaste venster van 40 polls x 5s = 200s, en werd
    daarom als vastgelopen behandeld. Met 60 polls x 5s = 300s hoort hij groen te zijn,
    en zonder het voortgangsvenster is hij dat niet.
    """
    polls = 60
    kubectl = _kubectl_with_progress([(True, 30 - (i // 2), True) for i in range(polls)], gone_after=polls)
    argo = ArgoConnector()

    async def _exists(app_name: str) -> bool:
        return bool(await kubectl.argocd_application_exists(app_name))

    with (
        patch.object(argo, "application_exists", AsyncMock(side_effect=_exists)),
        patch("asyncio.sleep", AsyncMock()),
    ):
        result = await argo.wait_for_application_deletion(
            "nd-j7s-pr-63",
            max_retries=40,
            retry_delay=5,
            kubectl_connector=kubectl,
            progress_window_seconds=900,
        )

    assert result is True, "een cascade die vordert mag niet op de klok afgekapt worden"


@pytest.mark.asyncio
async def test_the_same_slow_cascade_is_cut_off_without_the_progress_window() -> None:
    """Dezelfde cascade, zonder venster: dit is het gedrag dat rood maakte.

    Twee kanten van dezelfde meting, want anders pint de toets hierboven alleen dat de
    lus kan doorlopen en niet dat het venster er iets aan doet.
    """
    polls = 60
    kubectl = _kubectl_with_progress([(True, 30 - (i // 2), True) for i in range(polls)], gone_after=polls)
    argo = ArgoConnector()

    async def _exists(app_name: str) -> bool:
        return bool(await kubectl.argocd_application_exists(app_name))

    with (
        patch.object(argo, "application_exists", AsyncMock(side_effect=_exists)),
        patch("asyncio.sleep", AsyncMock()),
    ):
        result = await argo.wait_for_application_deletion(
            "nd-j7s-pr-63",
            max_retries=40,
            retry_delay=5,
            kubectl_connector=kubectl,
        )

    assert result is False
    assert kubectl.argocd_application_deletion_progress.await_count == 0, (
        "zonder venster hoort de voortgang niet eens opgevraagd te worden"
    )
    kubectl.list_namespaced_resource_types.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_deletion_that_shows_no_progress_still_reaches_the_force_route() -> None:
    """deletionTimestamp gezet, maar niets beweegt: dat is wel vastgelopen.

    En het mag niet het hele venster van 15 minuten opsouperen: de force-route hoort op
    hetzelfde moment te komen als voorheen.
    """
    kubectl = _kubectl_with_progress([(True, 30, True)], gone_after=10_000)
    argo = ArgoConnector()
    with (
        patch.object(argo, "application_exists", AsyncMock(return_value=True)),
        patch("asyncio.sleep", AsyncMock()),
    ):
        result = await argo.wait_for_application_deletion(
            "nd-j7s-pr-63",
            max_retries=40,
            retry_delay=5,
            kubectl_connector=kubectl,
            progress_window_seconds=900,
        )

    assert result is False
    assert kubectl.argocd_application_deletion_progress.await_count == 40, (
        "een stilstaande delete hoort op 40 polls te stoppen, niet op de 180 van het venster"
    )


async def _wacht_op_delete(argo: ArgoConnector, kubectl: MagicMock, stall_retries: int | None) -> bool:
    """De wacht met en zonder ``stall_retries``, zodat ``None`` echt de default aanroept.

    Twee aanroepen en geen ``**kwargs``: dan blijft de aanroep typecheckbaar.
    """
    if stall_retries is None:
        return await argo.wait_for_application_deletion(
            "nd-j7s-pr-63",
            max_retries=40,
            retry_delay=5,
            kubectl_connector=kubectl,
            progress_window_seconds=900,
        )
    return await argo.wait_for_application_deletion(
        "nd-j7s-pr-63",
        max_retries=40,
        retry_delay=5,
        kubectl_connector=kubectl,
        progress_window_seconds=900,
        stall_retries=stall_retries,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("meegegeven", "stilstand"),
    [(None, 12), (4, 4), (30, 30)],
    ids=["default", "korter", "langer"],
)
async def test_a_cascade_that_progresses_and_then_stalls_gives_up_on_the_stall(
    meegegeven: int | None, stilstand: int
) -> None:
    """Eerst vooruitgang, dan stilstand: stoppen op de stilstand, niet op de grens.

    Vordert tot poll 50, daarna niets meer. De grendel vraagt om BEIDE: het venster
    voorbij EN ``stall_retries`` polls zonder verandering. Dat is 50 polls vooruitgang,
    plus de eerste bevroren meting (nog een verandering), plus de stilstand zelf.

    De eerste rij laat ``stall_retries`` weg: zo staat de default vast. Zonder die rij is
    de default vrij te verlagen, en dan haalt een hapering in een gezonde cascade de
    force-route binnen.
    """
    vordert = [(True, 60 - i, True) for i in range(50)]
    kubectl = _kubectl_with_progress([*vordert, (True, 10, True)], gone_after=10_000)
    argo = ArgoConnector()
    with (
        patch.object(argo, "application_exists", AsyncMock(return_value=True)),
        patch("asyncio.sleep", AsyncMock()),
    ):
        result = await _wacht_op_delete(argo, kubectl, meegegeven)

    assert result is False
    assert kubectl.argocd_application_deletion_progress.await_count == 50 + 1 + stilstand


@pytest.mark.asyncio
async def test_a_window_shorter_than_the_base_wait_does_not_shorten_it() -> None:
    """Een venster KORTER dan ``max_retries`` mag de wacht niet inkorten.

    De wacht loopt tot de ruimste van de twee grenzen. Zou hij de kleinste nemen, dan
    maakt een krap gezet venster de delete sneller rood dan voorheen, en dat is de fout
    die deze wijziging juist weghaalt. 60s / 5s is 12 polls tegen een basis van 40.
    """
    kubectl = _kubectl_with_progress([(True, 30, True)], gone_after=10_000)
    argo = ArgoConnector()
    bestaat = AsyncMock(return_value=True)
    with patch.object(argo, "application_exists", bestaat), patch("asyncio.sleep", AsyncMock()):
        result = await argo.wait_for_application_deletion(
            "nd-j7s-pr-63",
            max_retries=40,
            retry_delay=5,
            kubectl_connector=kubectl,
            progress_window_seconds=60,
        )

    assert result is False
    assert bestaat.await_count == 40
    assert kubectl.argocd_application_deletion_progress.await_count == 0, (
        "een venster dat niets toevoegt hoort de voortgang niet op te vragen"
    )


@pytest.mark.asyncio
async def test_an_unreadable_application_earns_no_extra_window() -> None:
    """Een mislukte read is geen stilstand, maar ook geen voortgang.

    Alleen een GEMETEN daling verdient het ruimere venster. Zou de wacht ook doorlopen op de
    afwezigheid van informatie, dan wacht een onleesbare Application 900s waar hij er 200
    wachtte, zonder ooit iets gemeten te hebben.
    """
    kubectl = _kubectl_with_progress([None], gone_after=10_000)
    argo = ArgoConnector()
    with (
        patch.object(argo, "application_exists", AsyncMock(return_value=True)),
        patch("asyncio.sleep", AsyncMock()),
    ):
        result = await argo.wait_for_application_deletion(
            "nd-j7s-pr-63",
            max_retries=40,
            retry_delay=5,
            kubectl_connector=kubectl,
            progress_window_seconds=900,
        )

    assert result is False
    assert kubectl.argocd_application_deletion_progress.await_count == 40, (
        "zonder meting hoort de wacht op zijn gewone 40 polls te blijven, niet op de 180 van het venster"
    )


@pytest.mark.asyncio
async def test_a_cascade_that_becomes_unreadable_halfway_keeps_the_window_it_earned() -> None:
    """De andere kant: gemeten voortgang en daarna onleesbaar.

    Die reads vallen weg terwijl de cascade liep, en dan mag de wacht niet onmiddellijk
    stoppen. Hij stopt op ``stall_retries`` polls zonder meting na het gewone venster, dus op
    50 polls voortgang plus 12.
    """
    vordert = [(True, 60 - i, True) for i in range(50)]
    kubectl = _kubectl_with_progress([*vordert, None], gone_after=10_000)
    argo = ArgoConnector()
    with (
        patch.object(argo, "application_exists", AsyncMock(return_value=True)),
        patch("asyncio.sleep", AsyncMock()),
    ):
        result = await argo.wait_for_application_deletion(
            "nd-j7s-pr-63",
            max_retries=40,
            retry_delay=5,
            kubectl_connector=kubectl,
            progress_window_seconds=900,
        )

    assert result is False
    assert kubectl.argocd_application_deletion_progress.await_count == 50 + 12


@pytest.mark.asyncio
async def test_a_wait_without_a_window_does_not_claim_a_stall_it_never_measured(caplog) -> None:
    """De wacht zonder venster meet niets, dus hij mag ook niets over stilstand beweren.

    Zonder die voorwaarde loopt de teller van "geen meting" ook daar vol, en meldt een
    gewone time-out dat de delete is vastgelopen terwijl er nooit iets gemeten is.

    De tekst hieronder staat letterlijk in de logregel van de wacht: verhuist die melding,
    dan hoort deze assertie mee te verhuizen en niet stil waar te worden.
    """
    kubectl = _kubectl_with_progress([(True, 30, True)], gone_after=10_000)
    argo = ArgoConnector()
    with (
        patch.object(argo, "application_exists", AsyncMock(return_value=True)),
        patch("asyncio.sleep", AsyncMock()),
        caplog.at_level(logging.WARNING, logger="opi.connectors.argo"),
    ):
        result = await argo.wait_for_application_deletion(
            "nd-j7s-pr-63", max_retries=40, retry_delay=5, kubectl_connector=kubectl
        )

    assert result is False
    assert "NOT confirmed deleted after 40 retries" in caplog.text
    assert "no measured decrease" not in caplog.text


@pytest.mark.asyncio
async def test_the_resource_types_are_discovered_once_per_wait() -> None:
    """De inventaris loopt over elk namespaced type, dus de discovery hoort niet per poll.

    Zonder deze toets is de cache vrij weg te halen, en dan doet een wacht van 180 polls 180
    api-resources-aanroepen extra op een control plane die het al zwaar heeft.
    """
    kubectl = _kubectl_with_progress([(True, 30 - (i // 2), True) for i in range(60)], gone_after=60)
    argo = ArgoConnector()

    async def _exists(app_name: str) -> bool:
        return bool(await kubectl.argocd_application_exists(app_name))

    with (
        patch.object(argo, "application_exists", AsyncMock(side_effect=_exists)),
        patch("asyncio.sleep", AsyncMock()),
    ):
        await argo.wait_for_application_deletion(
            "nd-j7s-pr-63",
            max_retries=40,
            retry_delay=5,
            kubectl_connector=kubectl,
            progress_window_seconds=900,
        )

    assert kubectl.list_namespaced_resource_types.await_count == 1
    assert kubectl.argocd_application_deletion_progress.await_args.args[2] == ["secrets", "deployments.apps"], (
        "de gevonden types horen mee naar de inventaris, anders doet die zijn eigen discovery"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("verloop", "beweging"),
    [
        ([(True, 30 + i, True) for i in range(200)], "groeiende telling"),
        ([(True, 30 + (i % 2), True) for i in range(200)], "telling die 30/31 afwisselt"),
        ([(True, 30, i % 2 == 0) for i in range(200)], "fladderende complete-vlag"),
    ],
    ids=["groei", "wisselt", "vlag-fladdert"],
)
async def test_movement_that_is_not_emptying_earns_no_extra_window(
    verloop: list[tuple[bool, int, bool] | None], beweging: str
) -> None:
    """Beweging is geen voortgang: alleen een namespace die LEEGLOOPT verdient het venster.

    Drie vormen die alle drie op het cluster kunnen gebeuren. Groei is de samenloop van 29
    september: de ordening serialiseert op (project, deployment), dus een delete van pr-62 en
    een add van pr-64 lopen parallel in EEN namespace en de resources van de add laten de
    teller van de vastgelopen delete bewegen. Een wisselende telling is diezelfde samenloop
    in het klein, en de complete-vlag fladdert zodra de inventaris een type niet kan lezen,
    precies wat tijdens het incident exit 1 gaf.

    Vroeg de grendel of de meting VERANDERDE, dan kochten alle drie het volle venster van
    180 polls voor hetzelfde vastgelopen geval, en was het taakbudget daarna op.
    """
    kubectl = _kubectl_with_progress(verloop, gone_after=10_000)
    argo = ArgoConnector()
    with (
        patch.object(argo, "application_exists", AsyncMock(return_value=True)),
        patch("asyncio.sleep", AsyncMock()),
    ):
        result = await argo.wait_for_application_deletion(
            "nd-j7s-pr-62",
            max_retries=40,
            retry_delay=5,
            kubectl_connector=kubectl,
            progress_window_seconds=900,
        )

    assert result is False
    assert kubectl.argocd_application_deletion_progress.await_count == 40, (
        f"een {beweging} is beweging zonder voortgang en hoort op 40 polls te stoppen"
    )


@pytest.mark.asyncio
async def test_a_cascade_that_keeps_emptying_through_the_noise_ends_green() -> None:
    """De andere kant: groei naast een cascade die wel leegloopt blijft groen.

    Dezelfde namespace waarin een add resources bijzet, maar de delete vordert nu echt. De
    telling gaat 30 -> 34 -> 0, en omdat de vergelijking tegen de LAAGSTE stand loopt tellen
    de dalingen boven die stand niet mee: een gezonde cascade zet elke keer een nieuwe
    laagste stand en haalt de force-route niet.

    ``max_retries`` staat hier op 10 zodat de stilstandgrendel AL leeft terwijl de ruis loopt.
    Op 40 zou de grendel pas na de ruis aan de beurt zijn en belooft de toets niets over hoe
    zwaar die ruis weegt: de teller komt op 9 van de 12, en wie de ruis dubbel laat tellen
    maakt deze delete rood.
    """
    groeit = [(True, 30 + i, True) for i in range(5)]
    krimpt = [(True, 34 - i, True) for i in range(35)]
    kubectl = _kubectl_with_progress([*groeit, *krimpt], gone_after=40)
    argo = ArgoConnector()

    async def _exists(app_name: str) -> bool:
        return bool(await kubectl.argocd_application_exists(app_name))

    with (
        patch.object(argo, "application_exists", AsyncMock(side_effect=_exists)),
        patch("asyncio.sleep", AsyncMock()),
    ):
        result = await argo.wait_for_application_deletion(
            "nd-j7s-pr-62",
            max_retries=10,
            retry_delay=5,
            kubectl_connector=kubectl,
            progress_window_seconds=900,
        )

    assert result is True


@pytest.mark.asyncio
async def test_a_failed_type_discovery_is_asked_once_and_measures_nothing() -> None:
    """Een mislukte discovery is niets te meten, en hoort niet per poll opnieuw.

    Zonder deze grendel blijft ``progress_types`` op ``None``, probeert elke poll de
    discovery opnieuw EN doet ``list_tracked_resources`` hem daarna nog een keer: gemeten 80
    api-resources-aanroepen en 121 ERROR-regels in een wacht van 40 polls, op een control
    plane die het al zwaar heeft. Zo'n poll verdient ook geen venster, dus de wacht houdt
    zijn kale 40 polls.
    """
    kubectl = _kubectl_with_progress([(True, 30, True)], gone_after=10_000)
    kubectl.list_namespaced_resource_types = AsyncMock(return_value=None)
    argo = ArgoConnector()
    bestaat = AsyncMock(return_value=True)
    with patch.object(argo, "application_exists", bestaat), patch("asyncio.sleep", AsyncMock()):
        result = await argo.wait_for_application_deletion(
            "nd-j7s-pr-62",
            max_retries=40,
            retry_delay=5,
            kubectl_connector=kubectl,
            progress_window_seconds=900,
        )

    assert result is False
    assert bestaat.await_count == 40, "zonder meting hoort de wacht op zijn gewone 40 polls te blijven"
    assert kubectl.list_namespaced_resource_types.await_count == 1
    kubectl.argocd_application_deletion_progress.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("inventaris_compleet", "meldt_halve_inventaris"),
    [(False, True), (True, False)],
    ids=["halve-inventaris", "hele-inventaris"],
)
async def test_the_measurement_the_comparison_ignores_still_reaches_the_log(
    inventaris_compleet: bool, meldt_halve_inventaris: bool, caplog
) -> None:
    """Wat buiten de vergelijking valt, hoort in de logregel te staan.

    De ``complete``-vlag en de deletionTimestamp wegen niet mee in de beslissing: een
    fladderende inventaris is geen voortgang. Daarmee is de log hun enige bestemming, en
    juist die inventaris gaf tijdens het incident exit 1. Vallen ze stil uit de melding weg,
    dan is bij de volgende vastgelopen delete niet terug te lezen dat de meting waarop de
    wacht doorliep maar de helft van de namespace zag.
    """
    kubectl = _kubectl_with_progress([(True, 30, inventaris_compleet)], gone_after=10_000)
    argo = ArgoConnector()
    with (
        patch.object(argo, "application_exists", AsyncMock(return_value=True)),
        patch("asyncio.sleep", AsyncMock()),
        caplog.at_level(logging.INFO, logger="opi.connectors.argo"),
    ):
        result = await argo.wait_for_application_deletion(
            "nd-j7s-pr-62",
            max_retries=2,
            retry_delay=5,
            kubectl_connector=kubectl,
            progress_window_seconds=900,
        )

    assert result is False
    assert "deletion underway: True" in caplog.text
    assert "30 ArgoCD-owned resources left in its namespace" in caplog.text
    assert ("inventory incomplete" in caplog.text) is meldt_halve_inventaris


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("gooit_op", "fout"),
    [
        ("discovery", KubectlConnectionError),
        ("inventaris", KubectlConnectionError),
        ("inventaris", KubectlExecutionError),
    ],
    ids=["discovery-verbinding", "inventaris-verbinding", "inventaris-uitvoering"],
)
async def test_a_kubectl_that_raises_does_not_escalate_out_of_the_wait(gooit_op: str, fout: type[Exception]) -> None:
    """Een kubectl die gooit is geen meting, en mag de wacht niet opblazen.

    De meting is er om een trage delete te sparen, niet om een delete te laten escaleren.
    Escaleert hij wel, dan slaat de aanroeper de force-route over en zet hij in force mode
    `argocd_app_deleted = True`, waarmee de project file wordt bijgewerkt zonder bevestiging.

    Beide foutsoorten die de wacht afvangt staan erin: `_run_kubectl_command` noemt ze
    allebei in zijn Raises, en met alleen de verbindingsfout is de andere helft van die
    tuple vrij weg te halen.
    """
    kubectl = _kubectl_with_progress([(True, 30, True)], gone_after=10_000)
    stuk = AsyncMock(side_effect=fout("kubectl weg"))
    if gooit_op == "discovery":
        kubectl.list_namespaced_resource_types = stuk
    else:
        kubectl.argocd_application_deletion_progress = stuk
    argo = ArgoConnector()
    bestaat = AsyncMock(return_value=True)
    with patch.object(argo, "application_exists", bestaat), patch("asyncio.sleep", AsyncMock()):
        result = await argo.wait_for_application_deletion(
            "nd-j7s-pr-62",
            max_retries=40,
            retry_delay=5,
            kubectl_connector=kubectl,
            progress_window_seconds=900,
        )

    assert result is False
    assert bestaat.await_count == 40, "zonder meting hoort de wacht zijn gewone 40 polls te lopen"
    if gooit_op == "discovery":
        # Een discovery die gooit wordt net zo onthouden als een die None gaf: zonder dat
        # herhaalt elke poll de api-resources-aanroep en haar foutregel.
        assert kubectl.list_namespaced_resource_types.await_count == 1


@pytest.mark.asyncio
async def test_an_inventory_that_throws_once_keeps_being_measured_after_it() -> None:
    """Een inventaris die EEN keer gooit zet de meting van de rest van de wacht niet uit.

    De discovery wordt onthouden omdat de types van een cluster niet veranderen terwijl een
    cascade loopt, maar de inventaris is een lezing per poll: een kubectl die even wegvalt
    zegt niets over de volgende poll. Zou die ene fout ook onthouden worden, dan meet de
    wacht daarna niets meer, verdient een gezonde cascade zijn ruimere venster niet, en komt
    de delete van 29 september alsnog bij de force-route uit op de haperingen van het control
    plane in plaats van op zijn eigen voortgang.
    """
    kubectl = _kubectl_with_progress([(True, 200 - i, True) for i in range(200)], gone_after=10_000)
    echte_meting = kubectl.argocd_application_deletion_progress.side_effect
    gegooid = {"ja": False}

    async def _eerste_keer_stuk(
        app_name: str, namespace: str | None = None, resource_types: list[str] | None = None
    ) -> tuple[bool, int, bool] | None:
        if not gegooid["ja"]:
            gegooid["ja"] = True
            raise KubectlConnectionError("kubectl even weg")
        return await echte_meting(app_name, namespace, resource_types)

    kubectl.argocd_application_deletion_progress = AsyncMock(side_effect=_eerste_keer_stuk)
    argo = ArgoConnector()
    with (
        patch.object(argo, "application_exists", AsyncMock(return_value=True)),
        patch("asyncio.sleep", AsyncMock()),
    ):
        result = await argo.wait_for_application_deletion(
            "nd-j7s-pr-63",
            max_retries=40,
            retry_delay=5,
            kubectl_connector=kubectl,
            progress_window_seconds=900,
        )

    assert result is False
    assert kubectl.argocd_application_deletion_progress.await_count == 180, (
        "na de ene mislukte lezing hoort elke volgende poll weer te meten, en die dalingen "
        "kopen het volle venster van 180 polls"
    )
    assert kubectl.list_namespaced_resource_types.await_count == 1, (
        "de discovery blijft wel onthouden: het venster komt van de metingen, niet van een "
        "discovery die per poll opnieuw gedaan wordt"
    )
