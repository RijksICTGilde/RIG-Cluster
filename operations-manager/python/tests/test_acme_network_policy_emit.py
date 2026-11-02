"""Unit test for ProjectManager._emit_acme_network_policy.

Voor RC-336 opende de ACME-policy poort 80 en 8089 voor elke pod in de namespace. De
geextraheerde helper mag alleen de HTTP-01-solver selecteren, alleen poort 8089 openen
en de bron beperken tot de router van het cluster. Mocking volgt het patroon van
test_sleep_mode_emit.py: de manifest generator wordt vervangen, de methode wordt
rechtstreeks aangeroepen.
"""

from unittest.mock import MagicMock

from opi.core.cluster_config import get_ingress_controller_selector
from opi.manager.project_manager import ProjectManager


def _emit(cluster_name: str = "odcn-production") -> tuple[str, dict]:
    pm = ProjectManager.__new__(ProjectManager)
    pm._manifest_generator = MagicMock()
    pm._manifest_generator.create_manifest_file.return_value = "/tmp/out/acme-http-prod-network-policy.yaml"

    result = pm._emit_acme_network_policy(
        deployment_name="prod",
        namespace="rig-demo",
        cluster_name=cluster_name,
        output_dir="/tmp/out",
    )
    _, kwargs = pm._manifest_generator.create_manifest_file.call_args
    return result, kwargs


def test_de_bestandsnaam_volgt_de_bestaande_naamconventie() -> None:
    result, _ = _emit()
    assert result == "acme-http-prod-network-policy.yaml"


def test_alleen_de_solverpod_en_alleen_poort_8089() -> None:
    _, kwargs = _emit()
    values = kwargs["values"]
    assert values["pod_selector"] == 'acme.cert-manager.io/http01-solver: "true"'
    assert values["ports"] == [8089]


def test_de_bron_is_de_ingress_controller_van_het_cluster() -> None:
    _, kwargs = _emit(cluster_name="odcn-production")
    assert kwargs["values"]["from_selector"] == get_ingress_controller_selector("odcn-production")


def test_een_ander_cluster_krijgt_zijn_eigen_router_als_bron() -> None:
    _, kwargs = _emit(cluster_name="sandboxed-local")
    assert kwargs["values"]["from_selector"] == get_ingress_controller_selector("sandboxed-local")


def test_geen_sops_voor_een_networkpolicy() -> None:
    _, kwargs = _emit()
    assert kwargs["use_sops"] is False


def test_namespace_en_output_dir_worden_doorgegeven() -> None:
    _, kwargs = _emit()
    assert kwargs["values"]["namespace"] == "rig-demo"
    assert kwargs["output_dir"] == "/tmp/out"
