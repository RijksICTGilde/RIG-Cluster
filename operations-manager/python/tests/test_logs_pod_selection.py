"""De podkeuze in de logstroom: de kiezer, en de grendel eronder (RC-162).

WAAROM DIT DE BELANGRIJKSTE TOETS VAN DEZE TAAK IS

``kubectl logs <pod>`` kijkt niet naar welk component een pod hoort. Een projectnamespace
draagt de deployments van het hele team, dus een lid dat een podnaam raadt zou zonder deze
grendel de logs van een collega kunnen meelezen. De naam komt van de client en mag daarom
nooit rechtstreeks in een commando belanden - hij wordt eerst teruggezocht tussen de pods
die bij DIT project, DEZE deployment en DIT component horen.

En dat op twee plekken: bij het openen en bij ``switch``. Een verbinding die met een geldige
pod opende en daarna een andere naam meestuurt, is precies de weg die je overhoudt als je
alleen het openen toetst.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opi.api.logs_router import get_component_pods, resolve_component_pods
from opi.api.logs_websocket_router import logs_websocket_router
from opi.connectors.kubectl import KubectlConnector
from opi.core.config import settings

PROJECT = "psd-law"
DEPLOYMENT = "pr-114"
COMPONENT = "profielservice"
#: Het cluster dat deze instantie beheert. Uit de instellingen en niet verzonnen: de
#: handler weigert een deployment van een ander cluster VOOR hij naar de pod kijkt, en een
#: verzonnen naam zou de podtoets daarmee stil overslaan - de test was dan groen op de
#: verkeerde weigering.
CLUSTER = settings.CLUSTER_MANAGER

EIGEN_POD = "pr-114-profielservice-58cb9567c5-9t87d"
POD_VAN_EEN_COLLEGA = "pr-114-betaalservice-77d9f8b4c-mmmmm"


def _pod(name: str, app: str, *, ready: bool = True, restarts: int = 0) -> dict[str, Any]:
    return {
        "name": name,
        "app": app,
        "pod_template_hash": "58cb9567c5",
        "deleting": False,
        "ready": ready,
        "image": "rcr.rijksapps.nl/ghcr-rig/minbzk/moza-profiel-service@sha256:2c0728ed",
        "restart_count": restarts,
        "started_at": "2026-08-18T11:59:12Z" if ready else None,
        "has_previous_attempt": restarts > 0,
    }


def _project_store(components: list[str] | None = None, cluster: str = CLUSTER) -> MagicMock:
    info = MagicMock()
    info.data = {
        "name": PROJECT,
        "deployments": [
            {
                "name": DEPLOYMENT,
                "cluster": cluster,
                "namespace": PROJECT,
                "components": [{"reference": ref} for ref in (components or [COMPONENT])],
            }
        ],
    }
    store = MagicMock()
    store.get = MagicMock(return_value=info)
    return store


# ---------------------------------------------------------------------------
# resolve_component_pods: het ENE antwoord op "welke pods mag dit component lezen"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_returns_only_the_pods_of_this_component():
    kubectl = MagicMock()
    kubectl.get_application_pods = AsyncMock(
        return_value=[
            _pod(EIGEN_POD, "pr-114-profielservice"),
            _pod(POD_VAN_EEN_COLLEGA, "pr-114-betaalservice"),
        ]
    )

    with patch("opi.api.logs_router.get_project_store", return_value=_project_store()):
        pods = await resolve_component_pods(
            kubectl, project_name=PROJECT, deployment_name=DEPLOYMENT, component=COMPONENT
        )

    assert [p["name"] for p in pods or []] == [EIGEN_POD]


@pytest.mark.asyncio
async def test_resolve_returns_none_for_an_unknown_component():
    """Geen lege lijst maar None: 'dit component bestaat niet' is iets anders dan 'geen pods'."""
    kubectl = MagicMock()
    kubectl.get_application_pods = AsyncMock(return_value=[])

    with patch("opi.api.logs_router.get_project_store", return_value=_project_store()):
        assert (
            await resolve_component_pods(
                kubectl, project_name=PROJECT, deployment_name=DEPLOYMENT, component="bestaat-niet"
            )
            is None
        )


@pytest.mark.asyncio
async def test_resolve_returns_none_for_a_deployment_on_another_cluster():
    kubectl = MagicMock()
    kubectl.get_application_pods = AsyncMock(return_value=[])

    with patch("opi.api.logs_router.get_project_store", return_value=_project_store(cluster="ergens-anders")):
        assert (
            await resolve_component_pods(kubectl, project_name=PROJECT, deployment_name=DEPLOYMENT, component=COMPONENT)
            is None
        )


# ---------------------------------------------------------------------------
# Het endpoint dat de kiezer vult
# ---------------------------------------------------------------------------


def _request(email: str | None) -> MagicMock:
    request = MagicMock()
    request.session = {"user": {"email": email}} if email else {}
    return request


@pytest.mark.asyncio
async def test_endpoint_refuses_a_user_without_rights_on_the_project():
    from fastapi import HTTPException

    with (
        patch("opi.api.logs_router.get_user_service") as user_service,
        patch("opi.api.logs_router.is_user_authorized_for_project", return_value=False),
    ):
        user_service.return_value.is_email_allowed.return_value = True
        with pytest.raises(HTTPException) as excinfo:
            await get_component_pods(
                _request("buitenstaander@rijksoverheid.nl"),
                PROJECT,
                deployment=DEPLOYMENT,
                component=COMPONENT,
            )

    assert excinfo.value.status_code == 403


@pytest.mark.asyncio
async def test_endpoint_refuses_without_a_session():
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        await get_component_pods(_request(None), PROJECT, deployment=DEPLOYMENT, component=COMPONENT)

    assert excinfo.value.status_code == 401


@pytest.mark.asyncio
async def test_endpoint_gives_a_404_for_an_unknown_component_and_not_another_ones_pods():
    from fastapi import HTTPException

    with (
        patch("opi.api.logs_router.get_user_service") as user_service,
        patch("opi.api.logs_router.is_user_authorized_for_project", return_value=True),
        patch("opi.api.logs_router.resolve_component_pods", AsyncMock(return_value=None)),
    ):
        user_service.return_value.is_email_allowed.return_value = True
        with pytest.raises(HTTPException) as excinfo:
            await get_component_pods(
                _request("lid@rijksoverheid.nl"), PROJECT, deployment=DEPLOYMENT, component="bestaat-niet"
            )

    assert excinfo.value.status_code == 404


@pytest.mark.asyncio
async def test_endpoint_lists_the_pods_with_the_source_registry_image():
    import json

    with (
        patch("opi.api.logs_router.get_user_service") as user_service,
        patch("opi.api.logs_router.is_user_authorized_for_project", return_value=True),
        patch(
            "opi.api.logs_router.resolve_component_pods",
            AsyncMock(return_value=[_pod(EIGEN_POD, "pr-114-profielservice", ready=False, restarts=5)]),
        ),
        patch(
            "opi.api.logs_router.get_registry_rewrite_mappings",
            return_value=[{"from": "ghcr.io", "to": "rcr.rijksapps.nl/ghcr-rig"}],
        ),
    ):
        user_service.return_value.is_email_allowed.return_value = True
        response = await get_component_pods(
            _request("lid@rijksoverheid.nl"), PROJECT, deployment=DEPLOYMENT, component=COMPONENT
        )

    payload = json.loads(response.body)
    assert payload["pods"] == [
        {
            "name": EIGEN_POD,
            "ready": False,
            "image": "ghcr.io/minbzk/moza-profiel-service@sha256:2c0728ed",
            "running_since": None,
            "restart_count": 5,
            "has_previous_attempt": True,
        }
    ]


# ---------------------------------------------------------------------------
# De WebSocket: een podnaam van de client komt er niet zomaar in
# ---------------------------------------------------------------------------


def _stdout_met(regels: list[bytes]) -> Any:
    """Een stdout die deze regels levert en daarna EOF blijft geven, zoals kubectl doet."""
    resterend = list(regels)

    async def readline() -> bytes:
        if resterend:
            return resterend.pop(0)
        await asyncio.sleep(0.05)
        return b""

    stdout = MagicMock()
    stdout.readline = readline
    return stdout


@pytest.fixture
def stream_start() -> Any:
    """Een nagebootste ``stream_deployment_logs``, zodat er nooit een kubectl start."""
    process = MagicMock()
    process.stdout = MagicMock()
    process.stderr = MagicMock()
    process.pid = 4242
    process.returncode = None
    # wait() wordt afgewacht bij een omschakeling en bij het opruimen; een kale MagicMock
    # is daar niet awaitable en laat de omschakeling stil stranden.
    process.wait = AsyncMock(return_value=0)
    return AsyncMock(return_value=process)


def _client(stream_start: Any, toegestane_pods: list[dict[str, Any]]) -> Any:
    """Een app met alleen de logrouter erin, met de authenticatie afgevangen.

    De sessiecontrole zelf is hier niet aan de beurt en heeft zijn eigen toetsen; wat hier
    gemeten wordt is wat er NA het inloggen met een podnaam gebeurt.
    """
    app = FastAPI()
    app.include_router(logs_websocket_router)

    return (
        TestClient(app),
        patch.multiple(
            "opi.api.logs_websocket_router",
            _get_session_from_cookie=MagicMock(return_value={"user": {"email": "lid@rijksoverheid.nl"}}),
            get_user_service=MagicMock(return_value=MagicMock(is_email_allowed=MagicMock(return_value=True))),
            is_user_authorized_for_project=MagicMock(return_value=True),
            get_project_store=MagicMock(return_value=_project_store()),
            resolve_component_pods=AsyncMock(return_value=toegestane_pods),
        ),
        patch.object(KubectlConnector, "stream_deployment_logs", stream_start),
    )


URL = f"/api/logs/stream/{PROJECT}?deployment={DEPLOYMENT}&component={COMPONENT}"


def test_a_pod_that_is_not_this_components_starts_no_process(stream_start: Any):
    """De naam van een pod van een collega wordt geweigerd voordat kubectl iets ziet."""
    client, auth, kubectl = _client(stream_start, [_pod(EIGEN_POD, "pr-114-profielservice")])

    with auth, kubectl, client.websocket_connect(f"{URL}&pod={POD_VAN_EEN_COLLEGA}") as ws:
        bericht = ws.receive_json()

    assert bericht["type"] == "error"
    assert bericht["message"] == "Resource not found"
    stream_start.assert_not_called()


def test_a_valid_pod_is_followed_by_name(stream_start: Any):
    client, auth, kubectl = _client(stream_start, [_pod(EIGEN_POD, "pr-114-profielservice")])

    with auth, kubectl, client.websocket_connect(f"{URL}&pod={EIGEN_POD}") as ws:
        assert ws.receive_json()["status"] == "connected"
        assert ws.receive_json()["status"] == "streaming"

    stream_start.assert_called_once()
    assert stream_start.call_args.kwargs["pod_name"] == EIGEN_POD
    assert stream_start.call_args.kwargs["previous"] is False


def test_the_previous_attempt_is_requested_when_asked_for(stream_start: Any):
    client, auth, kubectl = _client(stream_start, [_pod(EIGEN_POD, "pr-114-profielservice")])

    with auth, kubectl, client.websocket_connect(f"{URL}&pod={EIGEN_POD}&previous=true") as ws:
        assert ws.receive_json()["status"] == "connected"
        assert ws.receive_json()["status"] == "streaming"

    assert stream_start.call_args.kwargs["previous"] is True


def test_without_a_pod_the_label_selector_stays(stream_start: Any):
    """Het gedrag van vandaag blijft het gedrag van vandaag."""
    client, auth, kubectl = _client(stream_start, [_pod(EIGEN_POD, "pr-114-profielservice")])

    with auth, kubectl, client.websocket_connect(URL) as ws:
        assert ws.receive_json()["status"] == "connected"
        assert ws.receive_json()["status"] == "streaming"

    assert stream_start.call_args.kwargs["pod_name"] is None


def test_a_switch_to_a_foreign_pod_is_refused_and_starts_no_process(stream_start: Any):
    """Dezelfde grendel op switch: anders is een geopende verbinding de omweg eromheen."""
    client, auth, kubectl = _client(stream_start, [_pod(EIGEN_POD, "pr-114-profielservice")])

    with auth, kubectl, client.websocket_connect(URL) as ws:
        ws.receive_json()
        ws.receive_json()
        stream_start.reset_mock()

        ws.send_json({"action": "switch", "component": COMPONENT, "pod": POD_VAN_EEN_COLLEGA})
        bericht = ws.receive_json()

    assert bericht["type"] == "error"
    stream_start.assert_not_called()


def test_a_switch_to_an_own_pod_restarts_the_stream_on_that_pod(stream_start: Any):
    client, auth, kubectl = _client(stream_start, [_pod(EIGEN_POD, "pr-114-profielservice")])

    with auth, kubectl, client.websocket_connect(URL) as ws:
        ws.receive_json()
        ws.receive_json()
        stream_start.reset_mock()

        ws.send_json({"action": "switch", "component": COMPONENT, "pod": EIGEN_POD, "previous": True})
        assert ws.receive_json()["status"] == "switching"
        gestroomd = ws.receive_json()

    assert gestroomd["status"] == "streaming"
    assert gestroomd["pod"] == EIGEN_POD
    assert gestroomd["previous"] is True
    assert stream_start.call_args.kwargs["pod_name"] == EIGEN_POD
    assert stream_start.call_args.kwargs["previous"] is True


def test_the_previous_attempt_actually_delivers_its_lines(stream_start: Any):
    """Een afgesloten logboek moet WEL binnenkomen; alleen het opnieuw aanhaken vervalt.

    Deze toets bestaat omdat het mis ging. De voorwaarde die het aanhaken uitzet stond
    boven aan de leeslus in plaats van bij het aanhaakbesluit, en sloeg daarmee de
    ``readline`` zelf over: op de sandbox gaf de vorige poging nul regels terwijl
    ``kubectl logs --previous`` er twee had (gemeten 28 augustus 2026). Het commando was
    goed opgebouwd, dus de commandotoets bleef groen - dit is de toets die het wel ziet.
    """
    process = stream_start.return_value
    process.stdout = _stdout_met(
        [b"ERROR Migration checksum mismatch for migration 4\n", b"FATAL Flyway validation failed, shutting down\n"]
    )
    client, auth, kubectl = _client(stream_start, [_pod(EIGEN_POD, "pr-114-profielservice", ready=False, restarts=5)])

    regels = []
    with auth, kubectl, client.websocket_connect(f"{URL}&pod={EIGEN_POD}&previous=true") as ws:
        while len(regels) < 2:
            bericht = ws.receive_json()
            if bericht["type"] == "log":
                regels.append(bericht["line"])

    assert regels == [
        "ERROR Migration checksum mismatch for migration 4",
        "FATAL Flyway validation failed, shutting down",
    ]
