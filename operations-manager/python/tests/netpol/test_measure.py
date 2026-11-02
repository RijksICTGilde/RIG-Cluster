"""Toetsen voor measure.py zonder een echt cluster.

`task netpol:meet` maakt een wegwerp kind-cluster en meet daar echt op; dat kan niet
vanuit een sessie (docker-wachter blokkeert het aanmaken van een eigen cluster). Deze
tests dekken daarom de logica die geen cluster nodig heeft: de naamgeving, de
listener-commando's, en de kubectl-aanroepen met `kubectl` zelf vervangen door een
nepversie die de argumenten vastlegt in plaats van ze echt uit te voeren.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from typing import ClassVar

import pytest
from tests.netpol import measure
from tests.netpol.evaluator import External, Pod
from tests.netpol.matrix import Flow


@dataclass
class FakeKubectl:
    """Legt elke aanroep vast en geeft de volgende voorbereide uitkomst terug."""

    uitkomsten: list[subprocess.CompletedProcess] = field(default_factory=list)
    aanroepen: list[tuple[tuple[str, ...], str | None]] = field(default_factory=list)

    def __call__(self, *args: str, input_text: str | None = None, check: bool = True):
        self.aanroepen.append((args, input_text))
        if self.uitkomsten:
            return self.uitkomsten.pop(0)
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")


def _endpoints(*namen: str) -> dict[str, Pod | External]:
    """load_matrix weigert een flow met een onbekend endpoint, dus een stub ervan moet
    elk endpoint uit zijn flows noemen."""
    return {naam: Pod(namespace="rig-prd-demo") for naam in namen}


def _voltooid(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], returncode=returncode, stdout=stdout, stderr="")


class TestKubectlWrapper:
    def test_kubectl_geeft_de_argumenten_door_aan_subprocess(self, monkeypatch):
        vastgelegd = {}

        def fake_run(cmd, input=None, capture_output=None, text=None, check=None):  # noqa: A002
            vastgelegd["cmd"] = cmd
            vastgelegd["input"] = input
            vastgelegd["capture_output"] = capture_output
            vastgelegd["text"] = text
            vastgelegd["check"] = check
            return _voltooid(stdout="ok")

        monkeypatch.setattr(measure.subprocess, "run", fake_run)
        result = measure.kubectl("get", "pods", input_text="payload", check=False)

        assert vastgelegd["cmd"] == ["kubectl", "get", "pods"]
        assert vastgelegd["input"] == "payload"
        assert vastgelegd["capture_output"] is True
        assert vastgelegd["text"] is True
        assert vastgelegd["check"] is False
        assert result.stdout == "ok"


class TestPodNaam:
    def test_underscores_en_hoofdletters_worden_platgeslagen(self):
        assert measure._pod_name("Tenant_App") == "probe-tenant-app"

    def test_een_naam_zonder_bijzonderheden_blijft_gelijk(self):
        assert measure._pod_name("rig-db") == "probe-rig-db"


class TestListenPoorten:
    FLOWS: ClassVar[list[Flow]] = [
        Flow(van="a", naar="doel", poort=443, verwacht="open", waarom=""),
        Flow(van="a", naar="doel", poort=80, verwacht="open", waarom=""),
        Flow(van="a", naar="doel", poort=443, verwacht="dicht", waarom="dubbel"),
        Flow(van="a", naar="ander-doel", poort=22, verwacht="open", waarom=""),
    ]

    def test_poorten_zijn_gesorteerd_en_zonder_duplicaten(self):
        assert measure._listen_ports("doel", self.FLOWS) == [80, 443]

    def test_een_doel_zonder_binnenkomende_flows_heeft_geen_poorten(self):
        assert measure._listen_ports("niemand-stuurt-hierheen", self.FLOWS) == []


class TestListenerCommando:
    def test_zonder_poorten_alleen_wachten(self):
        assert measure._listener_command([]) == "sleep infinity"

    def test_elke_poort_krijgt_een_eigen_lus(self):
        commando = measure._listener_command([80, 443])
        assert "nc -l -p 80" in commando
        assert "nc -l -p 443" in commando
        assert commando.endswith("sleep infinity")
        assert "-e" not in commando
        assert "-lk" not in commando


class TestApplyNamespaces:
    def test_alleen_pod_endpoints_leveren_een_namespace(self, monkeypatch):
        fake = FakeKubectl()
        monkeypatch.setattr(measure, "kubectl", fake)
        endpoints = {
            "tenant": Pod(namespace="rig-prd-demo", namespace_labels={"created-by": "operations-manager"}),
            "internet": External(ip="93.184.216.34"),
        }
        namespaces = measure.apply_namespaces(endpoints)

        assert namespaces == {"rig-prd-demo"}

    def test_de_namespace_manifest_draagt_de_namespace_labels(self, monkeypatch):
        fake = FakeKubectl()
        monkeypatch.setattr(measure, "kubectl", fake)
        endpoints = {"tenant": Pod(namespace="rig-prd-demo", namespace_labels={"created-by": "operations-manager"})}

        measure.apply_namespaces(endpoints)

        assert len(fake.aanroepen) == 1
        args, input_text = fake.aanroepen[0]
        assert args == ("apply", "-f", "-")
        manifest = json.loads(input_text)
        assert manifest["kind"] == "Namespace"
        assert manifest["metadata"]["name"] == "rig-prd-demo"
        assert manifest["metadata"]["labels"] == {"created-by": "operations-manager"}

    def test_een_systeem_pod_levert_geen_namespace(self, monkeypatch):
        """Anders zet de meting een eigen Namespace-manifest over kube-system heen."""
        fake = FakeKubectl()
        monkeypatch.setattr(measure, "kubectl", fake)
        endpoints = {
            "kube-dns": Pod(namespace="kube-system", labels={"k8s-app": "kube-dns"}),
            "tenant": Pod(namespace="rig-prd-demo"),
        }

        namespaces = measure.apply_namespaces(endpoints)

        assert namespaces == {"rig-prd-demo"}


class TestApplyProbePods:
    def test_external_endpoints_krijgen_geen_pod(self, monkeypatch):
        fake = FakeKubectl()
        monkeypatch.setattr(measure, "kubectl", fake)
        endpoints = {"internet": External(ip="93.184.216.34")}
        measure.apply_probe_pods(endpoints, flows=[])

        assert fake.aanroepen == []

    def test_een_systeem_pod_krijgt_geen_probe_pod(self, monkeypatch):
        """Een probe-pod op kube-dns zou in de bestaande Service belanden en de DNS
        van het meetcluster overnemen (zie measure.py SYSTEEM_NAMESPACES)."""
        fake = FakeKubectl()
        monkeypatch.setattr(measure, "kubectl", fake)
        endpoints = {"kube-dns": Pod(namespace="kube-system", labels={"k8s-app": "kube-dns"})}
        flows = [Flow(van="tenant", naar="kube-dns", poort=53, verwacht="open", waarom="")]

        measure.apply_probe_pods(endpoints, flows)

        assert fake.aanroepen == []

    def test_een_pod_krijgt_zijn_labels_en_luisterpoorten(self, monkeypatch):
        fake = FakeKubectl()
        monkeypatch.setattr(measure, "kubectl", fake)
        endpoints = {"rig-db": Pod(namespace="rig-prd-operations", labels={"cnpg.io/cluster": "rig-db"})}
        flows = [Flow(van="tenant", naar="rig-db", poort=5432, verwacht="open", waarom="")]

        measure.apply_probe_pods(endpoints, flows)

        apply_calls = [c for c in fake.aanroepen if c[0] == ("apply", "-f", "-")]
        assert len(apply_calls) == 1
        manifest = json.loads(apply_calls[0][1])
        assert manifest["metadata"]["name"] == "probe-rig-db"
        assert manifest["metadata"]["namespace"] == "rig-prd-operations"
        assert manifest["metadata"]["labels"] == {"cnpg.io/cluster": "rig-db"}
        container = manifest["spec"]["containers"][0]
        assert container["ports"] == [{"containerPort": 5432}]
        assert "nc -l -p 5432" in container["command"][-1]

        wait_calls = [c for c in fake.aanroepen if c[0][0] == "wait"]
        assert wait_calls == [
            (("wait", "--for=condition=Ready", "pod/probe-rig-db", "-n", "rig-prd-operations", "--timeout=120s"), None)
        ]


class TestApplyPolicies:
    def test_alleen_policies_in_een_bestaande_namespace_worden_toegepast(self, monkeypatch):
        fake = FakeKubectl()
        monkeypatch.setattr(measure, "kubectl", fake)
        policies = [
            {"metadata": {"name": "in-scope", "namespace": "rig-prd-demo"}, "spec": {}},
            {"metadata": {"name": "buiten-scope", "namespace": "rig-prd-operations"}, "spec": {}},
        ]

        toegepast = measure.apply_policies(policies, namespaces={"rig-prd-demo"})

        assert toegepast == ["rig-prd-demo/in-scope"]
        assert len(fake.aanroepen) == 1
        args, input_text = fake.aanroepen[0]
        assert args == ("apply", "-f", "-")
        assert "in-scope" in input_text
        assert "buiten-scope" not in input_text


class TestTargetAddress:
    def test_een_external_endpoint_geeft_zijn_ip_direct_zonder_kubectl(self, monkeypatch):
        fake = FakeKubectl()
        monkeypatch.setattr(measure, "kubectl", fake)

        adres = measure._target_address(External(ip="93.184.216.34"), "internet")

        assert adres == "93.184.216.34"
        assert fake.aanroepen == []

    def test_een_pod_endpoint_leest_het_podip_via_kubectl(self, monkeypatch):
        fake = FakeKubectl(uitkomsten=[_voltooid(stdout="10.244.0.7\n")])
        monkeypatch.setattr(measure, "kubectl", fake)

        adres = measure._target_address(Pod(namespace="rig-prd-demo"), "tenant-app")

        assert adres == "10.244.0.7"
        args, _ = fake.aanroepen[0]
        assert args == (
            "get",
            "pod",
            "probe-tenant-app",
            "-n",
            "rig-prd-demo",
            "-o",
            "jsonpath={.status.podIP}",
        )


class TestMeasureFlow:
    def test_een_bron_buiten_het_cluster_kan_niet_meten(self):
        endpoints = {"internet": External(ip="93.184.216.34"), "doel": Pod(namespace="rig-prd-demo")}
        flow = Flow(van="internet", naar="doel", poort=443, verwacht="open", waarom="")

        with pytest.raises(TypeError, match="bron buiten het cluster"):
            measure.measure_flow(flow, endpoints)

    def test_exitcode_nul_is_open(self, monkeypatch):
        fake = FakeKubectl(
            uitkomsten=[_voltooid(stdout="10.244.0.7"), _voltooid(returncode=0)],
        )
        monkeypatch.setattr(measure, "kubectl", fake)
        endpoints = {"bron": Pod(namespace="rig-prd-demo"), "doel": Pod(namespace="rig-prd-operations")}
        flow = Flow(van="bron", naar="doel", poort=5432, verwacht="open", waarom="")

        assert measure.measure_flow(flow, endpoints) is True

    def test_een_timeout_exitcode_is_dicht(self, monkeypatch):
        fake = FakeKubectl(
            uitkomsten=[_voltooid(stdout="10.244.0.7"), _voltooid(returncode=1)],
        )
        monkeypatch.setattr(measure, "kubectl", fake)
        endpoints = {"bron": Pod(namespace="rig-prd-demo"), "doel": Pod(namespace="rig-prd-operations")}
        flow = Flow(van="bron", naar="doel", poort=5432, verwacht="dicht", waarom="")

        assert measure.measure_flow(flow, endpoints) is False

    def test_zonder_adres_voor_het_doel_is_het_een_fout(self, monkeypatch):
        fake = FakeKubectl(uitkomsten=[_voltooid(stdout="")])
        monkeypatch.setattr(measure, "kubectl", fake)
        endpoints = {"bron": Pod(namespace="rig-prd-demo"), "doel": Pod(namespace="rig-prd-operations")}
        flow = Flow(van="bron", naar="doel", poort=5432, verwacht="open", waarom="")

        with pytest.raises(RuntimeError, match="geen adres"):
            measure.measure_flow(flow, endpoints)


class TestMain:
    def test_weigert_als_de_context_niet_het_meetcluster_is(self, monkeypatch):
        fake = FakeKubectl(uitkomsten=[_voltooid(stdout="kind-mijn-sandbox\n")])
        monkeypatch.setattr(measure, "kubectl", fake)

        def boom(*args, **kwargs):
            raise AssertionError("mag niet aangeroepen worden bij een verkeerde context")

        monkeypatch.setattr(measure, "apply_namespaces", boom)
        monkeypatch.setattr("sys.argv", ["measure.py"])

        assert measure.main() == 2

    def test_draait_door_op_het_meetcluster_en_meldt_eens_bij_overeenstemming(self, monkeypatch, capsys):
        contextcall = _voltooid(stdout=f"{measure.MEETCLUSTER_CONTEXT}\n")
        fake = FakeKubectl(uitkomsten=[contextcall])
        monkeypatch.setattr(measure, "kubectl", fake)
        monkeypatch.setattr(measure, "apply_namespaces", lambda endpoints: {"rig-prd-demo"})
        monkeypatch.setattr(measure, "apply_probe_pods", lambda endpoints, flows: None)
        monkeypatch.setattr(measure, "apply_policies", lambda policies, namespaces: [])
        monkeypatch.setattr(measure, "odcn_policies", list)

        flow = Flow(van="a", naar="b", poort=1, verwacht="open", waarom="")
        monkeypatch.setattr(measure, "load_matrix", lambda: (_endpoints("a", "b"), [flow]))
        monkeypatch.setattr(
            measure, "evaluate_flow", lambda flow, endpoints, policies: type("V", (), {"allowed": True})()
        )
        monkeypatch.setattr(measure, "measure_flow", lambda flow, endpoints: True)
        monkeypatch.setattr("sys.argv", ["measure.py"])

        assert measure.main() == 0
        assert "het over elke regel eens" in capsys.readouterr().out

    def test_een_afwijking_tussen_berekend_en_gemeten_geeft_exitcode_1(self, monkeypatch):
        contextcall = _voltooid(stdout=f"{measure.MEETCLUSTER_CONTEXT}\n")
        fake = FakeKubectl(uitkomsten=[contextcall])
        monkeypatch.setattr(measure, "kubectl", fake)
        monkeypatch.setattr(measure, "apply_namespaces", lambda endpoints: {"rig-prd-demo"})
        monkeypatch.setattr(measure, "apply_probe_pods", lambda endpoints, flows: None)
        monkeypatch.setattr(measure, "apply_policies", lambda policies, namespaces: [])
        monkeypatch.setattr(measure, "odcn_policies", list)

        flow = Flow(van="a", naar="b", poort=1, verwacht="open", waarom="")
        monkeypatch.setattr(measure, "load_matrix", lambda: (_endpoints("a", "b"), [flow]))
        monkeypatch.setattr(
            measure, "evaluate_flow", lambda flow, endpoints, policies: type("V", (), {"allowed": True})()
        )
        monkeypatch.setattr(measure, "measure_flow", lambda flow, endpoints: False)
        monkeypatch.setattr("sys.argv", ["measure.py"])

        assert measure.main() == 1

    def test_only_beperkt_de_flows_tot_een_endpoint(self, monkeypatch):
        contextcall = _voltooid(stdout=f"{measure.MEETCLUSTER_CONTEXT}\n")
        fake = FakeKubectl(uitkomsten=[contextcall])
        monkeypatch.setattr(measure, "kubectl", fake)
        monkeypatch.setattr(measure, "apply_namespaces", lambda endpoints: {"rig-prd-demo"})
        monkeypatch.setattr(measure, "apply_probe_pods", lambda endpoints, flows: None)
        monkeypatch.setattr(measure, "apply_policies", lambda policies, namespaces: [])
        monkeypatch.setattr(measure, "odcn_policies", list)

        flows = [
            Flow(van="a", naar="b", poort=1, verwacht="open", waarom=""),
            Flow(van="a", naar="c", poort=1, verwacht="open", waarom=""),
        ]
        monkeypatch.setattr(measure, "load_matrix", lambda: (_endpoints("a", "b", "c"), flows))
        gemeten_flows = []

        def fake_measure_flow(flow, endpoints):
            gemeten_flows.append(flow)
            return True

        monkeypatch.setattr(
            measure, "evaluate_flow", lambda flow, endpoints, policies: type("V", (), {"allowed": True})()
        )
        monkeypatch.setattr(measure, "measure_flow", fake_measure_flow)
        monkeypatch.setattr("sys.argv", ["measure.py", "--only", "b"])

        assert measure.main() == 0
        assert [f.naar for f in gemeten_flows] == ["b"]

    def test_een_flow_met_een_systeem_endpoint_wordt_overgeslagen(self, monkeypatch, capsys):
        """measure_flow zou op zo'n flow een probe-pod in kube-system zoeken die er
        nooit komt (TestApplyProbePods hierboven); main() moet die flow dus niet meten."""
        contextcall = _voltooid(stdout=f"{measure.MEETCLUSTER_CONTEXT}\n")
        fake = FakeKubectl(uitkomsten=[contextcall])
        monkeypatch.setattr(measure, "kubectl", fake)
        monkeypatch.setattr(measure, "apply_namespaces", lambda endpoints: {"rig-prd-demo"})
        monkeypatch.setattr(measure, "apply_probe_pods", lambda endpoints, flows: None)
        monkeypatch.setattr(measure, "apply_policies", lambda policies, namespaces: [])
        monkeypatch.setattr(measure, "odcn_policies", list)

        endpoints = {
            "tenant": Pod(namespace="rig-prd-demo"),
            "kube-dns": Pod(namespace="kube-system", labels={"k8s-app": "kube-dns"}),
        }
        flow = Flow(van="tenant", naar="kube-dns", poort=53, verwacht="open", waarom="")
        monkeypatch.setattr(measure, "load_matrix", lambda: (endpoints, [flow]))
        monkeypatch.setattr(
            measure, "evaluate_flow", lambda flow, endpoints, policies: type("V", (), {"allowed": True})()
        )

        def boom(flow, endpoints):
            raise AssertionError("mag niet gemeten worden: er is geen probe-pod in kube-system")

        monkeypatch.setattr(measure, "measure_flow", boom)
        monkeypatch.setattr("sys.argv", ["measure.py"])

        assert measure.main() == 0
        assert "OVERGESLAGEN" in capsys.readouterr().out


class TestIsSysteem:
    """Een pod in een systeemnamespace krijgt geen eigen probe-pod: die zou in de
    bestaande Service belanden en het verkeer van het echte onderdeel overnemen."""

    def test_een_pod_in_een_systeemnamespace_is_systeem(self):
        assert measure._is_systeem(Pod(namespace="kube-system", labels={"k8s-app": "kube-dns"}))

    def test_een_pod_in_een_gewone_namespace_is_geen_systeem(self):
        assert not measure._is_systeem(Pod(namespace="rig-prd-operations", labels={"app": "operations-manager"}))

    def test_een_extern_adres_is_geen_systeem(self):
        assert not measure._is_systeem(External(ip="93.184.216.34"))
