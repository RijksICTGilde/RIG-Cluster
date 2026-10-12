"""Een OOM zonder meetpunten mag niet op het eigen vorige antwoord verder rekenen.

Wat er in productie gebeurde
----------------------------
toets-hn7/pr-103, component ``frontend``, gedeclareerd op 25Mi met limit gelijk aan
request. De app viel om bij het opstarten (``exit 137``, container ``app``), en omdat een
crashloopende pod nooit gescrapet wordt gaf Grafana 19 keer "no data" voor precies deze
pods. In 42 seconden::

    10:35:48  OOM: pr-103-frontend-6694c99686-dj56w container app, exit 137
    10:35:51  tuner: baseline 25Mi   -> schrijft 139Mi   (task upsert_deployment)
    10:36:29  OOM: zelfde pod
    10:36:30  tuner: baseline 139Mi  -> 278Mi, afgetopt op 200Mi (task refresh_deployment)

Elke ronde las de limit die de vorige ronde zelf had geschreven, vermenigvuldigde die
(x3 onder 64Mi, x2 onder 256Mi) en legde de headroom-marge van 64Mi er bovenop. Dat is
geen tuner meer maar een feedbackloop, en de enige rem die hem stopte was het
groeiplafond van 8x het gedeclareerde. Daarom staat production-frontend nu op 136/200Mi
voor een app die 2Mi gebruikt: ``200 = 8 x 25`` en ``136 = 200 - 64``.

Beide OOM-regels komen van DEZELFDE pod, en dat is waar de reparatie aangrijpt: de
vermenigvuldiging met de huidige limit is juist zolang het bewijs van de huidige
pod-generatie komt, en alleen dan. Het bewijs van een pod die bij 25Mi omviel zegt niets
over een template van 139Mi dat nog niet eens is uitgerold. De rem daarvoor bestond al
(``oom_is_fresh_evidence``) maar stond alleen op de paden van de OOM-watcher; hij hoort
op de gedeelde deur waar alle paden langs tunen, de AFTER_SYNC-haak van de tuner.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.handlers.project_file_handler import ProjectFileHandler
from opi.services import oom_watcher
from opi.services.catalog.base import ComponentHealth
from opi.services.deployment_observation import AfterSyncObservation, run_after_sync_observation
from opi.services.resource_analyzer import _k8s_memory_to_mb

_PROJECT = "toets-hn7"
_CLUSTER = "odcn-production"
_NAMESPACE = "rig-prd-toets-hn7"
_DEPLOYMENT = "pr-103"
_COMPONENT = "frontend"

#: De pod die beide keren omviel, nog op het 25Mi-template.
_GENERATIE = "6694c99686"

#: Wat toets-hn7 declareert: limit gelijk aan request, nul headroom.
_DECLARED = "25Mi"

#: Het plafond waar de escalatie op uitkwam, 8x het gedeclareerde.
_PLAFOND_MB = 200


@pytest.fixture(autouse=True)
def _schone_generatiestaat():
    """De rem is moduleniveau-staat, dus elke test begint en eindigt leeg."""
    oom_watcher._last_tuned_pod_template_hash.clear()
    yield
    oom_watcher._last_tuned_pod_template_hash.clear()


def _project() -> dict[str, Any]:
    return {
        "name": _PROJECT,
        "components": [
            {
                "name": _COMPONENT,
                "type": "single",
                "resources": {
                    "limits": {"memory": _DECLARED, "cpu": "1"},
                    "requests": {"memory": _DECLARED, "cpu": "50m"},
                },
            }
        ],
        "deployments": [
            {
                "name": _DEPLOYMENT,
                "cluster": _CLUSTER,
                "namespace": _PROJECT,
                "components": [{"reference": _COMPONENT}],
            }
        ],
    }


def _connector(gemeten_mi: float | None, oom: bool = True) -> Any:
    """Prometheus die niets teruggeeft (``None``) of een meting, plus het OOM-signaal."""

    async def custom_query(query: str) -> list[dict[str, Any]]:
        if "kube_pod_container_status_last_terminated_reason" in query:
            return [{"value": [0, "1"]}] if oom else []
        if gemeten_mi is None:
            return []
        return [{"value": [0, str(gemeten_mi * 1024 * 1024)]}]

    return AsyncMock(custom_query=AsyncMock(side_effect=custom_query))


def _heeft_getuned(observatie: AfterSyncObservation) -> bool:
    return observatie.committed


def _huidige_limit(project: dict[str, Any]) -> str:
    comp = project["deployments"][0]["components"][0]
    override = (comp.get("resources") or {}).get("limits", {}).get("memory")
    return override or project["components"][0]["resources"]["limits"]["memory"]


def _huidige_request(project: dict[str, Any]) -> str:
    comp = project["deployments"][0]["components"][0]
    override = (comp.get("resources") or {}).get("requests", {}).get("memory")
    return override or project["components"][0]["resources"]["requests"]["memory"]


async def _ronde(
    project: dict[str, Any],
    *,
    generatie: str | None = _GENERATIE,
    gemeten_mi: float | None = None,
    commit_faalt: bool = False,
) -> AfterSyncObservation:
    """Een AFTER_SYNC-ronde voor de OOM'ende frontend, langs de runner die committeert.

    Langs de runner en niet rechtstreeks langs de haak, want de generatiegrendel wordt
    daar vastgelegd: pas nadat de commit is geslaagd.
    """
    save = AsyncMock(side_effect=RuntimeError("Forgejo onbereikbaar") if commit_faalt else None)
    kubectl = MagicMock(
        get_deployment_conditions=AsyncMock(return_value=None),
        get_vpa_recommendation=AsyncMock(return_value=None),
    )
    health = {_COMPONENT: ComponentHealth(oom_detected=True, oom_pod_template_hash=generatie)}
    with (
        patch("opi.manager.project_manager.ProjectManager", return_value=MagicMock(save_and_commit_project=save)),
        patch(
            "opi.services.resource_tuning_service.get_project_data_from_git",
            new=AsyncMock(return_value=(project, f"{_PROJECT}.yaml")),
        ),
        patch("opi.services.deployment_observation.get_prefixed_namespace", return_value=_NAMESPACE),
        patch("opi.services.resource_tuning_service.KubectlConnector", return_value=kubectl),
        patch("opi.services.resource_tuning_service.supports_vpa", return_value=False),
        patch("opi.services.resource_tuning_service.get_prefixed_namespace", return_value=_NAMESPACE),
        patch(
            "opi.services.resource_tuning_service.get_metrics_connector",
            new=AsyncMock(return_value=_connector(gemeten_mi)),
        ),
    ):
        return await run_after_sync_observation(_PROJECT, _DEPLOYMENT, health)


@pytest.mark.asyncio
async def test_zonder_meting_blijft_de_tweede_ronde_onder_het_plafond() -> None:
    """Dezelfde pod twee keer: de tweede ronde rekent niet verder op wat de eerste schreef."""
    project = _project()

    eerste = await _ronde(project)
    assert _heeft_getuned(eerste), "een OOM zonder meting moet nog wel ruimte geven"
    na_ronde_1 = _huidige_limit(project)

    tweede = await _ronde(project)

    assert not _heeft_getuned(tweede), (
        f"ronde 2 tunet op het bewijs van pod-generatie {_GENERATIE}, dat ronde 1 al beantwoordde"
    )
    assert _huidige_limit(project) == na_ronde_1
    assert _k8s_memory_to_mb(_huidige_limit(project)) < _PLAFOND_MB, (
        f"de tuner escaleert nog: ronde 1 gaf {na_ronde_1}, ronde 2 gaf {_huidige_limit(project)}, "
        f"en het plafond is {_PLAFOND_MB}Mi"
    )


@pytest.mark.asyncio
async def test_de_eerste_ronde_geeft_wel_headroom() -> None:
    """De OOM is echt, dus er moet ruimte bij: limit gelijk aan request is de oorzaak."""
    project = _project()

    await _ronde(project)

    assert _k8s_memory_to_mb(_huidige_limit(project)) > _k8s_memory_to_mb(_huidige_request(project)), (
        f"limit {_huidige_limit(project)} is niet boven request {_huidige_request(project)}, "
        "en dat is precies de toestand die de OOM veroorzaakte"
    )


@pytest.mark.asyncio
async def test_een_nieuwe_generatie_tunet_wel_weer() -> None:
    """De verhoging rolde uit en de pod valt nog om: dan is er echt meer nodig."""
    project = _project()

    await _ronde(project)
    na_ronde_1 = _huidige_limit(project)
    tweede = await _ronde(project, generatie="7d9c1a2b4")

    assert _heeft_getuned(tweede), "vers bewijs van een nieuwe pod moet wel tunen"
    assert _k8s_memory_to_mb(_huidige_limit(project)) > _k8s_memory_to_mb(na_ronde_1)


@pytest.mark.asyncio
async def test_zonder_generatie_tunet_hij_zoals_voorheen() -> None:
    """Een onbekende generatie mag geen stille blokkade worden.

    Dezelfde keuze als in ``oom_is_fresh_evidence``: valt de hash niet vast te stellen
    (een kubectl-hik, onleesbare uitvoer), dan is een tune te veel beter dan een
    auto-tune die uitvalt juist wanneer het cluster het al moeilijk heeft.
    """
    project = _project()

    eerste = await _ronde(project, generatie=None)
    tweede = await _ronde(project, generatie=None)

    assert _heeft_getuned(eerste)
    assert _heeft_getuned(tweede)


@pytest.mark.asyncio
async def test_een_tweede_ronde_schrijft_niets_naar_de_projects_repo() -> None:
    """De meetbare uitkomst: geen tweede commit, dus geen tweede pod-rollout.

    De fout kostte vier rollouts en vier commits op het bewijs van een pod, en elke
    rollout trok het image opnieuw. "Geen wijziging" is hier dus de uitkomst, niet een
    bijzaak.
    """
    project = _project()
    health = {_COMPONENT: ComponentHealth(oom_detected=True, oom_pod_template_hash=_GENERATIE)}
    project_manager = MagicMock(save_and_commit_project=AsyncMock())
    kubectl = MagicMock(
        get_deployment_conditions=AsyncMock(return_value=None),
        get_vpa_recommendation=AsyncMock(return_value=None),
    )

    with (
        patch("opi.manager.project_manager.ProjectManager", return_value=project_manager),
        patch(
            "opi.services.resource_tuning_service.get_project_data_from_git",
            new=AsyncMock(return_value=(project, f"{_PROJECT}.yaml")),
        ),
        patch("opi.services.deployment_observation.get_prefixed_namespace", return_value=_NAMESPACE),
        patch("opi.services.resource_tuning_service.KubectlConnector", return_value=kubectl),
        patch("opi.services.resource_tuning_service.supports_vpa", return_value=False),
        patch("opi.services.resource_tuning_service.get_prefixed_namespace", return_value=_NAMESPACE),
        patch(
            "opi.services.resource_tuning_service.get_metrics_connector",
            new=AsyncMock(return_value=_connector(None)),
        ),
    ):
        eerste = await run_after_sync_observation(_PROJECT, _DEPLOYMENT, health)
        tweede = await run_after_sync_observation(_PROJECT, _DEPLOYMENT, health)

    assert eerste.committed is True
    assert tweede.committed is False, "een tweede ronde op onveranderd bewijs mag niets committen"
    assert tweede.requeue_refresh is False, "en dus ook geen refresh in de wachtrij zetten"
    project_manager.save_and_commit_project.assert_awaited_once()


@pytest.mark.asyncio
async def test_een_mislukte_commit_schrijft_geen_generatie_af() -> None:
    """Gooit de commit, dan is er niets geschreven en is de generatie nog onbeantwoord.

    Anders weigert de volgende geplande check met "de vorige tune heeft deze generatie al
    beantwoord" terwijl die tune nooit landde. Op het watcher-pad valt dat niet op:
    ``apply_oom_tune`` slikt de fout en belast het budget niet, dus geen enkele rem meldt
    er nog iets over.
    """
    with pytest.raises(RuntimeError):
        await _ronde(_project(), commit_faalt=True)

    assert not oom_watcher._last_tuned_pod_template_hash, (
        f"de generatie staat als beantwoord terwijl de commit mislukte: {oom_watcher._last_tuned_pod_template_hash}"
    )

    # Verse lezing uit git, zoals de volgende ronde die ook doet: de verloren mutatie
    # staat er niet in.
    herkansing = await _ronde(_project())
    assert _heeft_getuned(herkansing), "na een mislukte commit moet de tune opnieuw kunnen"


@pytest.mark.asyncio
async def test_een_meting_onder_het_clusterminimum_is_een_meting() -> None:
    """2Mi gemeten tegen een clusterminimum van 25Mi is een meting, geen 'no data'.

    ``min_memory_limit_mi`` is 25 op elk cluster en ``min_observed_mi`` is 5.0, en die twee
    stonden in de verkeerde verhouding: onder 25Mi kun je niets zetten, dus elke meting
    daaronder kan maar een antwoord opleveren. Onder de 5 viel hij juist in de tak die bij
    een OOM gaat vermenigvuldigen, en dat is de slechtste van de twee. toets-hn7/frontend
    is er een van: 2Mi gemeten tegen een minimum van 25Mi.

    Zonder OOM, want dat is de tak waar "ik weet niets" naar overslaan leidde. Daarom langs
    de analyse zelf en niet langs de haak, die altijd een OOM meldt.
    """
    from opi.services.resource_tuning_service import _analyze_component_resources

    kubectl = MagicMock(
        get_deployment_conditions=AsyncMock(return_value=None),
        get_vpa_recommendation=AsyncMock(return_value=None),
    )
    with (
        patch("opi.services.resource_tuning_service.supports_vpa", return_value=False),
        patch("opi.services.resource_tuning_service.get_prefixed_namespace", return_value=_NAMESPACE),
    ):
        analyse = await _analyze_component_resources(
            connector=_connector(2.0, oom=False),
            file_handler=ProjectFileHandler(),
            project_data=_project(),
            dep_name=_DEPLOYMENT,
            component_ref=_COMPONENT,
            namespace=_NAMESPACE,
            cluster=_CLUSTER,
            kubectl=kubectl,
        )

    assert analyse is not None, "2Mi is gemeten, dus er valt wel iets te zeggen"
    assert analyse.max_observed_mb == pytest.approx(25.0), (
        f"de meting is niet op het clusterminimum gezet: {analyse.max_observed_mb}Mi"
    )
    assert _k8s_memory_to_mb(analyse.new_limit) < _PLAFOND_MB, f"escalatie op een meting van 2Mi: {analyse.new_limit}"
