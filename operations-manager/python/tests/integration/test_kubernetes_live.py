"""OPI's kubectl-connector tegen een echte Kubernetes-API op het wegwerpcluster.

Elke methode van ``KubectlConnector`` die ``opi/`` aanroept, draait hier ten minste een
keer, en het terugleeskanaal is een eigen ``kubectl`` (zie ``kubernetes_live.Kubectl``):
de returnwaarde van de connector zegt niets over wat het cluster deed.

De toetsen die hier eerder als ``test_kubectl_logs_real.py``,
``test_kubectl_error_handling.py`` en ``test_kubectl_write_ops.py`` stonden, zijn hierin
opgegaan: ze maakten hun eigen kind-cluster en toetsten vaak de aanroep in plaats van de
uitkomst ("the test passes if no exception is raised").

Twee methoden zijn hier maar half te meten, omdat hun operator alleen op ODCN staat en de
versie daarvan niet in deze repo gepind is:
``wait_for_capsule_tenant_label`` (het label wordt hier met de hand gezet, wat de
jsonpath-ontsnapping wel meet, maar niet de webhook van Capsule) en
``get_vpa_recommendation`` (alleen de tak zonder VPA). Zie de regels op
``tests/regression/actions.yaml``.

Draai hem met ``task test-cluster-up`` en dan ``task test-kubernetes``; zonder
``KUBERNETES_LIVE_CONTEXT`` slaat de groep zichzelf over.
``tests/test_live_group_coverage.py`` bewaakt dat er geen methode ontbreekt.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from opi.connectors.kubectl import KubectlConnector, KubectlExecutionError, create_kubectl_connector
from opi.utils.argocd_tracking import INSTANCE_LABEL, TrackedResource
from tests.integration.kubernetes_live import (
    DEPLOYMENT,
    NAMESPACE,
    OTHER_NAMESPACE,
    PREFIX,
    Kubectl,
    assert_test_cluster,
    drop_by_prefix,
    logging_deployment_manifest,
    namespace_manifest,
    wait_for_deployment,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

pytestmark = [pytest.mark.requires_infra, pytest.mark.kubernetes_live]

#: De namespace waar de Application-CR's van deze groep staan, zoals ArgoCD ze op dit
#: cluster houdt.
ARGO_NAMESPACE = "rig-system"

APP = f"{PREFIX}-app"
WORKLOAD = f"{PREFIX}-logger"

#: Een eigen finalizer om een Application vast te houden. Niet die van ArgoCD: dan zou de
#: controller de cascade doen en meet deze toets diens tempo in plaats van de connector.
HOLD_FINALIZER = "zadlive.example/hold"


@pytest.fixture(scope="module")
def kubectl() -> Kubectl:
    assert_test_cluster()
    return Kubectl()


@pytest.fixture(scope="module")
def connector(kubectl: Kubectl) -> Iterator[KubectlConnector]:
    """De connector zoals OPI hem bouwt, tegen de KUBECONFIG van het wegwerpcluster."""
    KubectlConnector._instance = None
    built = create_kubectl_connector()
    if not built.isConnected:
        pytest.fail("kubectl kan niet bij het cluster; draai task test-cluster-up")
    drop_by_prefix()
    yield built
    drop_by_prefix()
    KubectlConnector._instance = None


@pytest.fixture
def namespaces(connector: KubectlConnector, kubectl: Kubectl) -> Iterator[tuple[str, str]]:
    """Twee verse wegwerp-namespaces, en weg na de toets."""
    for namespace in (NAMESPACE, OTHER_NAMESPACE):
        kubectl.apply(namespace_manifest(namespace))
    yield (NAMESPACE, OTHER_NAMESPACE)
    drop_by_prefix()


@pytest.fixture
def workload(namespaces: tuple[str, str], kubectl: Kubectl) -> str:
    """Een draaiende deployment die logregels produceert, in de eerste namespace."""
    kubectl.apply(logging_deployment_manifest(WORKLOAD, NAMESPACE))
    wait_for_deployment(WORKLOAD, NAMESPACE)
    return WORKLOAD


def application_manifest(
    name: str,
    destination: str,
    *,
    finalizers: tuple[str, ...] = (),
    operation: bool = False,
) -> str:
    """Een Application-CR zoals OPI er een in git zet, maar met een onbereikbare bron.

    Onbereikbaar met opzet: deze groep meet de Kubernetes-API, niet de synchronisatie. Wat
    ArgoCD met een echte bron doet, meet de groep argocd_live.
    """
    lines = [
        "apiVersion: argoproj.io/v1alpha1",
        "kind: Application",
        "metadata:",
        f"  name: {name}",
        f"  namespace: {ARGO_NAMESPACE}",
    ]
    if finalizers:
        lines.append("  finalizers:")
        lines += [f"    - {finalizer}" for finalizer in finalizers]
    lines += [
        "spec:",
        "  project: default",
        "  source:",
        "    repoURL: http://forgejo.rig-system.svc.cluster.local:3000/rig-admin/zadlive-absent.git",
        "    targetRevision: main",
        "    path: .",
        "  destination:",
        "    server: https://kubernetes.default.svc",
        f"    namespace: {destination}",
    ]
    if operation:
        lines += ["operation:", "  sync:", "    revision: main"]
    return "\n".join(lines) + "\n"


@pytest.fixture
def application(namespaces: tuple[str, str], kubectl: Kubectl) -> Iterator[str]:
    kubectl.apply(application_manifest(APP, NAMESPACE))
    yield APP
    kubectl.run(
        "patch", "application", APP, "-n", ARGO_NAMESPACE, "--type", "merge", "-p", '{"metadata":{"finalizers":[]}}'
    )
    kubectl.run("delete", "application", APP, "-n", ARGO_NAMESPACE, "--ignore-not-found=true")


# ==================== de opstelling zelf ====================


async def test_the_connector_talks_to_the_throwaway_cluster(connector: KubectlConnector, kubectl: Kubectl) -> None:
    """De bodem onder de rest: zonder verbinding is elke andere uitkomst zinloos."""
    stdout, stderr, code = await connector.run_command(["get", "nodes", "-o", "name"])
    assert code == 0, stderr
    assert stdout.strip() in kubectl.ok("get", "nodes", "-o", "name")


async def test_run_command_passes_the_exit_code_of_kubectl_through(connector: KubectlConnector) -> None:
    stdout, stderr, code = await connector.run_command(["get", "there-is-no-such-type"])
    assert code != 0
    assert stderr


async def test_run_command_sends_stdin_to_kubectl(connector: KubectlConnector, namespaces: tuple[str, str]) -> None:
    namespace, _ = namespaces
    manifest = (
        f"apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: {PREFIX}-stdin\n  namespace: {namespace}\ndata:\n  a: b\n"
    )
    stdout, stderr, code = await connector.run_command(["apply", "-f", "-"], stdin_input=manifest)
    assert code == 0, stderr
    assert Kubectl().get("configmap", f"{PREFIX}-stdin", namespace) is not None


# ==================== templating en apply ====================


def test_template_manifest_fills_the_variables_in(connector: KubectlConnector) -> None:
    rendered = connector.template_manifest("name: {{ naam }}", {"naam": "zadlive"})
    assert rendered == "name: zadlive\n"


async def test_apply_manifest_puts_the_resource_in_the_cluster(
    connector: KubectlConnector, kubectl: Kubectl, namespaces: tuple[str, str], tmp_path: Path
) -> None:
    namespace, _ = namespaces
    manifest = tmp_path / "configmap.yaml"
    manifest.write_text(
        "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: {{ naam }}\ndata:\n  sleutel: '{{ waarde }}'\n"
    )

    await connector.apply_manifest(str(manifest), {"naam": f"{PREFIX}-cm", "waarde": "gemeten"}, namespace=namespace)

    stored = kubectl.get("configmap", f"{PREFIX}-cm", namespace)
    assert stored is not None
    assert stored["data"] == {"sleutel": "gemeten"}


async def test_apply_manifest_raises_on_a_namespace_that_does_not_exist(
    connector: KubectlConnector, tmp_path: Path
) -> None:
    manifest = tmp_path / "configmap.yaml"
    manifest.write_text(f"apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: {PREFIX}-nowhere\ndata:\n  a: b\n")

    with pytest.raises(KubectlExecutionError) as fout:
        await connector.apply_manifest(str(manifest), namespace=f"{PREFIX}-absent")
    assert f"{PREFIX}-absent" in str(fout.value)


async def test_apply_manifest_raises_on_a_manifest_the_api_server_rejects(
    connector: KubectlConnector, namespaces: tuple[str, str], tmp_path: Path
) -> None:
    namespace, _ = namespaces
    manifest = tmp_path / "broken.yaml"
    manifest.write_text("apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: Dit Mag Niet\ndata:\n  a: b\n")

    with pytest.raises(KubectlExecutionError):
        await connector.apply_manifest(str(manifest), namespace=namespace)


# ==================== namespaces ====================


async def test_namespace_exists_and_namespace_absent_answer_the_same_question(
    connector: KubectlConnector, namespaces: tuple[str, str]
) -> None:
    namespace, _ = namespaces
    assert await connector.namespace_exists(namespace) is True
    assert await connector.namespace_absent(namespace) is False
    assert await connector.namespace_exists(f"{PREFIX}-nergens") is False
    assert await connector.namespace_absent(f"{PREFIX}-nergens") is True


async def test_get_namespace_labels_map_reads_the_labels_of_every_namespace(
    connector: KubectlConnector, kubectl: Kubectl, namespaces: tuple[str, str]
) -> None:
    namespace, other = namespaces
    kubectl.ok("label", "namespace", namespace, "zadlive/meting=een", "--overwrite")

    gelezen = await connector.get_namespace_labels_map()

    assert gelezen[namespace]["zadlive/meting"] == "een"
    assert "zadlive/meting" not in gelezen[other]
    assert "kube-system" in gelezen, "de kaart hoort over het hele cluster te gaan"


async def test_apply_labels_to_resource_writes_every_label_at_once(
    connector: KubectlConnector, kubectl: Kubectl, namespaces: tuple[str, str]
) -> None:
    namespace, _ = namespaces

    assert await connector.apply_labels_to_resource("namespace", namespace, {"a": "1", "b": "2"}) is True

    labels = (kubectl.get("namespace", namespace) or {})["metadata"]["labels"]
    assert labels["a"] == "1"
    assert labels["b"] == "2"


async def test_apply_labels_to_resource_answers_false_for_a_resource_that_is_not_there(
    connector: KubectlConnector,
) -> None:
    assert await connector.apply_labels_to_resource("namespace", f"{PREFIX}-nergens", {"a": "1"}) is False


async def test_delete_namespace_and_wait_for_namespace_deletion_leave_nothing_behind(
    connector: KubectlConnector, kubectl: Kubectl, namespaces: tuple[str, str]
) -> None:
    namespace, other = namespaces

    assert await connector.delete_namespace(namespace) is True
    assert await connector.wait_for_namespace_deletion(namespace, max_retries=60, retry_delay=2) is True

    assert kubectl.get("namespace", namespace) is None
    assert kubectl.get("namespace", other) is not None, "de buur-namespace blijft staan"


async def test_delete_namespace_refuses_a_namespace_of_the_platform(connector: KubectlConnector) -> None:
    """De tweede laag onder een projectverwijdering: rig-system gaat nooit mee."""
    with pytest.raises(KubectlExecutionError):
        await connector.delete_namespace(ARGO_NAMESPACE)
    assert Kubectl().get("namespace", ARGO_NAMESPACE) is not None


async def test_wait_for_capsule_tenant_label_sees_the_label_once_it_is_there(
    connector: KubectlConnector, kubectl: Kubectl, namespaces: tuple[str, str]
) -> None:
    """Capsule staat alleen op ODCN; wat hier gemeten wordt is het lezen van dat label.

    Het label heeft een punt in zijn sleutel, en een jsonpath daarop geeft ongequot stil een
    lege waarde. Dat deel is hier echt gemeten, de webhook van Capsule niet.
    """
    namespace, _ = namespaces
    assert await connector.wait_for_capsule_tenant_label(namespace, timeout=1) is False

    kubectl.ok("label", "namespace", namespace, "capsule.clastix.io/tenant=zadlive-tenant", "--overwrite")

    assert await connector.wait_for_capsule_tenant_label(namespace, timeout=10) is True


# ==================== secrets en resources ====================


async def test_get_secret_reads_back_what_was_stored(
    connector: KubectlConnector, kubectl: Kubectl, namespaces: tuple[str, str]
) -> None:
    namespace, _ = namespaces
    kubectl.ok("create", "secret", "generic", f"{PREFIX}-secret", "-n", namespace, "--from-literal=wachtwoord=geheim")

    assert await connector.get_secret(f"{PREFIX}-secret", namespace) == {"wachtwoord": "geheim"}
    assert await connector.secret_exists(f"{PREFIX}-secret", namespace) is True


async def test_get_secret_answers_none_for_a_secret_that_is_not_there(
    connector: KubectlConnector, namespaces: tuple[str, str]
) -> None:
    namespace, _ = namespaces
    assert await connector.get_secret(f"{PREFIX}-absent", namespace) is None
    assert await connector.secret_exists(f"{PREFIX}-absent", namespace) is False


async def test_get_sops_secret_from_namespace_decodes_the_age_key(
    connector: KubectlConnector, kubectl: Kubectl, namespaces: tuple[str, str]
) -> None:
    namespace, other = namespaces
    sleutel = "AGE-SECRET-KEY-1EXAMPLEEXAMPLEEXAMPLEEXAMPLEEXAMPLE"
    kubectl.ok("create", "secret", "generic", "sops-age-key", "-n", namespace, f"--from-literal=key={sleutel}")

    assert await connector.get_sops_secret_from_namespace(namespace) == sleutel
    assert await connector.get_sops_secret_from_namespace(other) is None, "elke namespace heeft zijn eigen sleutel"


async def test_resource_exists_tells_present_from_absent(
    connector: KubectlConnector, kubectl: Kubectl, namespaces: tuple[str, str]
) -> None:
    namespace, _ = namespaces
    kubectl.ok("create", "configmap", f"{PREFIX}-cm", "-n", namespace, "--from-literal=a=b")

    assert await connector.resource_exists("configmap", f"{PREFIX}-cm", namespace) is True
    assert await connector.resource_exists("configmap", f"{PREFIX}-absent", namespace) is False


async def test_get_namespaced_resource_returns_the_object_the_api_has(
    connector: KubectlConnector, kubectl: Kubectl, namespaces: tuple[str, str]
) -> None:
    namespace, _ = namespaces
    kubectl.ok("create", "configmap", f"{PREFIX}-cm", "-n", namespace, "--from-literal=a=b")

    gelezen = await connector.get_namespaced_resource("configmap", f"{PREFIX}-cm", namespace)

    assert gelezen is not None
    assert gelezen["data"] == {"a": "b"}
    assert await connector.get_namespaced_resource("configmap", f"{PREFIX}-absent", namespace) is None


async def test_get_resources_by_label_selects_only_what_matches(
    connector: KubectlConnector, kubectl: Kubectl, namespaces: tuple[str, str]
) -> None:
    namespace, _ = namespaces
    kubectl.ok("create", "configmap", f"{PREFIX}-wel", "-n", namespace, "--from-literal=a=b")
    kubectl.ok("create", "configmap", f"{PREFIX}-niet", "-n", namespace, "--from-literal=a=b")
    kubectl.ok("label", "configmap", f"{PREFIX}-wel", "-n", namespace, "zadlive/kies=ja")

    gevonden = await connector.get_resources_by_label("configmap", namespace, "zadlive/kies=ja")

    assert [item["metadata"]["name"] for item in gevonden] == [f"{PREFIX}-wel"]
    assert await connector.get_resources_by_label("configmap", namespace, "zadlive/kies=nee") == []


async def test_delete_resource_and_wait_for_resource_absence_confirm_the_resource_is_gone(
    connector: KubectlConnector, kubectl: Kubectl, namespaces: tuple[str, str]
) -> None:
    namespace, _ = namespaces
    kubectl.ok("create", "configmap", f"{PREFIX}-cm", "-n", namespace, "--from-literal=a=b")

    assert await connector.delete_resource("configmap", f"{PREFIX}-cm", namespace) is True
    assert await connector.wait_for_resource_absence("configmap", f"{PREFIX}-cm", namespace) is True
    assert kubectl.get("configmap", f"{PREFIX}-cm", namespace) is None


async def test_delete_resource_counts_a_resource_that_was_already_gone_as_deleted(
    connector: KubectlConnector, namespaces: tuple[str, str]
) -> None:
    """Een NotFound is de gevraagde eindtoestand, dus die telt als geslaagd.

    Een opruiming die twee keer langskomt mag de tweede keer niet falen.
    """
    namespace, _ = namespaces
    assert await connector.delete_resource("configmap", f"{PREFIX}-absent", namespace) is True


async def test_delete_resource_answers_false_when_the_cluster_does_not_serve_the_type(
    connector: KubectlConnector, namespaces: tuple[str, str]
) -> None:
    namespace, _ = namespaces
    assert await connector.delete_resource("zadlivenietbestaand", f"{PREFIX}-absent", namespace) is False


async def test_get_vpa_recommendation_answers_none_without_a_vpa(
    connector: KubectlConnector, namespaces: tuple[str, str]
) -> None:
    """De VPA-operator staat alleen op ODCN, dus alleen deze tak is hier te meten."""
    namespace, _ = namespaces
    assert await connector.get_vpa_recommendation(namespace, f"{PREFIX}-logger") is None


# ==================== werklasten, logs en gebeurtenissen ====================


async def test_get_deployment_status_counts_the_replicas_the_cluster_reports(
    connector: KubectlConnector, kubectl: Kubectl, workload: str
) -> None:
    statussen = await connector.get_deployment_status(NAMESPACE, workload)

    assert [status["name"] for status in statussen] == [workload]
    assert statussen[0]["ready"] == "1/1"
    assert (kubectl.get("deployment", workload, NAMESPACE) or {})["status"]["readyReplicas"] == 1


async def test_get_deployment_status_without_a_name_lists_the_whole_namespace(
    connector: KubectlConnector, workload: str
) -> None:
    assert {status["name"] for status in await connector.get_deployment_status(NAMESPACE)} == {workload}


async def test_get_deployment_conditions_reads_the_conditions_of_the_deployment(
    connector: KubectlConnector, workload: str
) -> None:
    condities = await connector.get_deployment_conditions(NAMESPACE, workload)

    assert condities is not None
    assert "Available" in {conditie["type"] for conditie in condities}
    assert await connector.get_deployment_conditions(NAMESPACE, f"{PREFIX}-absent") is None


async def test_get_deployment_logs_returns_the_lines_the_container_wrote(
    connector: KubectlConnector, workload: str
) -> None:
    regels = await connector.get_deployment_logs(workload, NAMESPACE, lines=5)

    assert any(regel.startswith("regel ") for regel in regels), regels
    assert len(regels) <= 5


async def test_get_deployment_logs_is_empty_for_a_deployment_that_is_not_there(
    connector: KubectlConnector, namespaces: tuple[str, str]
) -> None:
    namespace, _ = namespaces
    assert await connector.get_deployment_logs(f"{PREFIX}-absent", namespace) == []


async def test_stream_deployment_logs_keeps_handing_over_new_lines(connector: KubectlConnector, workload: str) -> None:
    proces = await connector.stream_deployment_logs(workload, NAMESPACE, lines=1)

    assert proces is not None
    assert proces.stdout is not None
    try:
        regel = await asyncio.wait_for(proces.stdout.readline(), timeout=60)
        assert regel.decode().startswith("regel ")
    finally:
        proces.terminate()
        await proces.wait()


async def test_get_application_pods_reports_what_each_pod_is_doing(
    connector: KubectlConnector, kubectl: Kubectl, workload: str
) -> None:
    pods = await connector.get_application_pods(NAMESPACE, DEPLOYMENT)

    assert len(pods) == 1, pods
    pod = pods[0]
    assert pod["ready"] is True
    assert pod["restart_count"] == 0
    assert pod["name"] in kubectl.names("pod", NAMESPACE)
    assert pod["image"].endswith(":1.37")


async def test_get_application_pods_is_empty_for_a_deployment_without_pods(
    connector: KubectlConnector, namespaces: tuple[str, str]
) -> None:
    namespace, _ = namespaces
    assert await connector.get_application_pods(namespace, f"{PREFIX}-absent") == []


async def test_get_pod_container_image_names_the_image_the_pod_runs(
    connector: KubectlConnector, kubectl: Kubectl, workload: str
) -> None:
    pod = sorted(kubectl.names("pod", NAMESPACE, selector=f"app={workload}"))[0]

    assert await connector.get_pod_container_image(NAMESPACE, pod, "app") == "busybox:1.37"
    assert await connector.get_pod_container_image(NAMESPACE, pod, "er-is-geen-container") is None


async def test_watch_pods_streams_the_current_state_first(connector: KubectlConnector, workload: str) -> None:
    """De eerste regels zijn ADDED-events voor wat er al staat; daar leeft de cache van."""
    proces = await connector.watch_pods("component=application")

    assert proces is not None
    assert proces.stdout is not None
    try:
        regel = await asyncio.wait_for(proces.stdout.readline(), timeout=60)
        assert regel.decode().strip(), "de watch hoort met de huidige stand te beginnen"
    finally:
        proces.terminate()
        await proces.wait()


async def test_get_namespace_events_reads_the_events_of_the_namespace(
    connector: KubectlConnector, kubectl: Kubectl, namespaces: tuple[str, str]
) -> None:
    """Een pod met een image die niet bestaat levert een Warning op; die hoort hier terug."""
    namespace, _ = namespaces
    kubectl.apply(
        f"apiVersion: v1\nkind: Pod\nmetadata:\n  name: {PREFIX}-geen-image\n  namespace: {namespace}\n"
        f"spec:\n  containers:\n    - name: app\n      image: localhost:5000/zadlive-bestaat-niet:0\n"
    )

    async def _een_waarschuwing() -> list[dict[str, str]]:
        for _ in range(60):
            events = await connector.get_namespace_events(namespace, event_type="Warning")
            if events:
                return events
            await asyncio.sleep(2)
        return []

    events = await _een_waarschuwing()
    assert events, "geen Warning-event gezien voor een pod die zijn image niet kan halen"
    assert all(event["type"] == "Warning" for event in events)
    assert await connector.get_namespace_events(namespace, event_type=None), "zonder filter hoort alles terug te komen"


# ==================== Application-CR's en de opruiming eromheen ====================


async def test_argocd_application_exists_and_get_read_the_cr_through_the_api(
    connector: KubectlConnector, application: str
) -> None:
    assert await connector.argocd_application_exists(application, ARGO_NAMESPACE) is True

    gelezen = await connector.get_argocd_application(application, ARGO_NAMESPACE)

    assert gelezen is not None
    assert gelezen["spec"]["destination"]["namespace"] == NAMESPACE
    assert await connector.argocd_application_exists(f"{PREFIX}-absent", ARGO_NAMESPACE) is False
    assert await connector.get_argocd_application(f"{PREFIX}-absent", ARGO_NAMESPACE) is None


async def test_get_argocd_application_destination_namespace_reads_the_destination(
    connector: KubectlConnector, application: str
) -> None:
    assert await connector.get_argocd_application_destination_namespace(application, ARGO_NAMESPACE) == NAMESPACE
    assert await connector.get_argocd_application_destination_namespace(f"{PREFIX}-absent", ARGO_NAMESPACE) is None


async def test_list_argocd_applications_includes_the_application_that_was_just_made(
    connector: KubectlConnector, application: str
) -> None:
    namen = {app["metadata"]["name"] for app in await connector.list_argocd_applications(ARGO_NAMESPACE)}

    assert application in namen


async def test_argocd_application_resource_types_falls_back_when_the_status_names_nothing(
    connector: KubectlConnector, application: str
) -> None:
    """Een Application die nog niets heeft uitgerold, geeft None: dan volgt volle discovery.

    Dat None is het antwoord waar de aanroeper op rekent; de tak met een gevulde
    ``status.resources`` meet de groep argocd_live, want daarvoor moet er echt gesynct zijn.
    """
    assert await connector.argocd_application_resource_types(application, ARGO_NAMESPACE) is None


async def test_terminate_argocd_application_operation_leaves_no_operation_behind(
    connector: KubectlConnector, kubectl: Kubectl, namespaces: tuple[str, str]
) -> None:
    namespace, _ = namespaces
    naam = f"{PREFIX}-op"
    kubectl.apply(application_manifest(naam, namespace, operation=True))
    try:
        assert await connector.terminate_argocd_application_operation(naam, ARGO_NAMESPACE) is True
        assert "operation" not in (kubectl.get("application", naam, ARGO_NAMESPACE) or {})
        # Nog een keer: zonder operation is er niets te wissen, en dat is geen fout.
        assert await connector.terminate_argocd_application_operation(naam, ARGO_NAMESPACE) is True
    finally:
        kubectl.run("delete", "application", naam, "-n", ARGO_NAMESPACE, "--ignore-not-found=true")


async def test_terminate_argocd_application_operation_is_true_for_an_application_that_is_gone(
    connector: KubectlConnector,
) -> None:
    assert await connector.terminate_argocd_application_operation(f"{PREFIX}-absent", ARGO_NAMESPACE) is True


async def test_remove_argocd_application_finalizers_lets_the_held_application_go(
    connector: KubectlConnector, kubectl: Kubectl, namespaces: tuple[str, str]
) -> None:
    """De valkuil: een verwijdering die blijft hangen omdat een finalizer de CR vasthoudt."""
    namespace, _ = namespaces
    naam = f"{PREFIX}-vast"
    kubectl.apply(application_manifest(naam, namespace, finalizers=(HOLD_FINALIZER,)))
    kubectl.run("delete", "application", naam, "-n", ARGO_NAMESPACE, "--wait=false")

    vastgehouden = kubectl.get("application", naam, ARGO_NAMESPACE)
    assert vastgehouden is not None, "de finalizer hoort de CR vast te houden"
    assert vastgehouden["metadata"]["deletionTimestamp"]

    assert await connector.remove_argocd_application_finalizers(naam, ARGO_NAMESPACE) is True

    assert await connector.wait_for_resource_absence("application", naam, ARGO_NAMESPACE) is True
    assert kubectl.get("application", naam, ARGO_NAMESPACE) is None


async def test_remove_argocd_application_finalizers_is_true_for_an_application_that_is_gone(
    connector: KubectlConnector,
) -> None:
    assert await connector.remove_argocd_application_finalizers(f"{PREFIX}-absent", ARGO_NAMESPACE) is True


async def test_delete_argocd_application_removes_the_cr(
    connector: KubectlConnector, kubectl: Kubectl, namespaces: tuple[str, str]
) -> None:
    namespace, _ = namespaces
    naam = f"{PREFIX}-weg"
    kubectl.apply(application_manifest(naam, namespace))

    assert await connector.delete_argocd_application(naam, ARGO_NAMESPACE) is True

    assert kubectl.get("application", naam, ARGO_NAMESPACE) is None
    assert await connector.delete_argocd_application(naam, ARGO_NAMESPACE) is True, "weg blijft weg, zonder fout"


async def test_argocd_application_deletion_progress_counts_what_is_left_in_the_namespace(
    connector: KubectlConnector, kubectl: Kubectl, namespaces: tuple[str, str]
) -> None:
    """Een cascade die vordert is te onderscheiden van een die stilstaat.

    Dat is wat de aanroeper nodig heeft: de status van de Application bevriest zodra er een
    deletionTimestamp staat, dus het getal moet uit de namespace komen.
    """
    namespace, _ = namespaces
    naam = f"{PREFIX}-cascade"
    kubectl.apply(application_manifest(naam, namespace, finalizers=(HOLD_FINALIZER,)))
    kubectl.ok("create", "configmap", f"{PREFIX}-gemarkeerd", "-n", namespace, "--from-literal=a=b")
    kubectl.ok("label", "configmap", f"{PREFIX}-gemarkeerd", "-n", namespace, f"{INSTANCE_LABEL}={naam}")

    try:
        voortgang = await connector.argocd_application_deletion_progress(naam, ARGO_NAMESPACE)
        assert voortgang is not None
        onderweg, over, volledig = voortgang
        assert onderweg is False, "zonder deletionTimestamp is er geen cascade"
        assert over == 1
        assert volledig is True

        kubectl.run("delete", "application", naam, "-n", ARGO_NAMESPACE, "--wait=false")
        kubectl.ok("delete", "configmap", f"{PREFIX}-gemarkeerd", "-n", namespace)

        voortgang = await connector.argocd_application_deletion_progress(naam, ARGO_NAMESPACE)
        assert voortgang is not None
        onderweg, over, volledig = voortgang
        assert onderweg is True
        assert over == 0, "de namespace is leeggelopen, dus de cascade vorderde"
    finally:
        kubectl.run(
            "patch",
            "application",
            naam,
            "-n",
            ARGO_NAMESPACE,
            "--type",
            "merge",
            "-p",
            '{"metadata":{"finalizers":[]}}',
        )
        kubectl.run("delete", "application", naam, "-n", ARGO_NAMESPACE, "--ignore-not-found=true")


async def test_argocd_application_deletion_progress_is_none_for_an_application_that_is_gone(
    connector: KubectlConnector,
) -> None:
    assert await connector.argocd_application_deletion_progress(f"{PREFIX}-absent", ARGO_NAMESPACE) is None


# ==================== de opruimveger ====================


async def test_list_namespaced_resource_types_discovers_the_types_of_this_cluster(
    connector: KubectlConnector,
) -> None:
    """Een vaste lijst zou een blinde vlek zijn voor een veger die juist het vergetene zoekt."""
    types = await connector.list_namespaced_resource_types()

    assert types is not None
    assert "secrets" in types
    assert "deployments.apps" in types
    assert "applications.argoproj.io" in types, "de CRD's van dit cluster horen erbij"


async def test_list_namespaced_kinds_maps_a_kind_to_its_kubectl_type(connector: KubectlConnector) -> None:
    kinds = await connector.list_namespaced_kinds()

    assert kinds is not None
    assert kinds["Secret"] == "secrets", "een kind uit de core-groep staat zonder groep in de sleutel"
    assert kinds["Deployment.apps"] == "deployments.apps"


async def test_list_tracked_resources_finds_only_what_argocd_owns(
    connector: KubectlConnector, kubectl: Kubectl, namespaces: tuple[str, str]
) -> None:
    """Het label is het merk op dit cluster; een resource met een eigenaar telt niet mee.

    Die uitzondering voor ownerReferences zit er omdat een operator het instance-label
    kopieert naar wat hij zelf maakt, en zulke resources horen bij hun eigenaar.
    """
    namespace, _ = namespaces
    kubectl.ok("create", "configmap", f"{PREFIX}-van-argo", "-n", namespace, "--from-literal=a=b")
    kubectl.ok("label", "configmap", f"{PREFIX}-van-argo", "-n", namespace, f"{INSTANCE_LABEL}={PREFIX}-app")
    kubectl.ok("create", "configmap", f"{PREFIX}-van-niemand", "-n", namespace, "--from-literal=a=b")

    inventaris = await connector.list_tracked_resources(namespace, ["configmaps"])

    assert inventaris is not None
    gevonden, volledig = inventaris
    assert volledig is True
    assert [resource.name for resource in gevonden] == [f"{PREFIX}-van-argo"]
    assert gevonden[0].app_name == f"{PREFIX}-app"
    assert gevonden[0].from_label is True


async def test_list_tracked_resources_is_empty_for_a_namespace_without_argocd_resources(
    connector: KubectlConnector, namespaces: tuple[str, str]
) -> None:
    _, other = namespaces
    assert await connector.list_tracked_resources(other, ["configmaps"]) == ([], True)


async def test_delete_tracked_resources_removes_them_and_names_what_it_could_not(
    connector: KubectlConnector, kubectl: Kubectl, namespaces: tuple[str, str]
) -> None:
    namespace, _ = namespaces
    kubectl.ok("create", "configmap", f"{PREFIX}-opruimen", "-n", namespace, "--from-literal=a=b")
    bestaat = TrackedResource(
        kind="ConfigMap",
        api_version="v1",
        name=f"{PREFIX}-opruimen",
        namespace=namespace,
        app_name=f"{PREFIX}-app",
        from_label=True,
    )
    # Een resource die er niet meer is, telt als opgeruimd; onverwijderbaar is een type dat
    # dit cluster niet kent, en dat is wat de aanroeper terug hoort te krijgen.
    niet_te_verwijderen = TrackedResource(
        kind="ZadliveNietBestaand",
        api_version="zadlive.example/v1",
        name=f"{PREFIX}-absent",
        namespace=namespace,
        app_name=f"{PREFIX}-app",
        from_label=True,
    )

    mislukt = await connector.delete_tracked_resources([bestaat, niet_te_verwijderen])

    assert mislukt == [niet_te_verwijderen]
    assert kubectl.get("configmap", f"{PREFIX}-opruimen", namespace) is None


# ==================== gelijktijdigheid ====================


async def test_the_connector_handles_commands_in_parallel(
    connector: KubectlConnector, namespaces: tuple[str, str]
) -> None:
    """Elke taak van OPI doet meerdere kubectl-aanroepen naast elkaar."""
    namespace, other = namespaces

    uitkomsten = await asyncio.gather(
        connector.namespace_exists(namespace),
        connector.namespace_exists(other),
        connector.namespace_exists(f"{PREFIX}-absent"),
        connector.get_namespace_labels_map(),
    )

    assert uitkomsten[0] is True
    assert uitkomsten[1] is True
    assert uitkomsten[2] is False
    assert namespace in uitkomsten[3]
