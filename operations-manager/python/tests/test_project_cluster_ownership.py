"""De grendel op de schrijfweg: dit cluster schrijft alleen aan zijn eigen projectbestanden.

De projectenrepo is gedeeld tussen clusters. Bij het lezen filtert
get_deployments(cluster_filter=True) op CLUSTER_MANAGER, maar op de schrijfweg stond niets,
waardoor een OPI een projectbestand van een ander cluster kon herschrijven. Dat maakt
AGE-versleutelde waarden onleesbaar voor de eigenaar, want de sleutel verschilt per cluster,
en de status van die deployments leeft op een ander cluster.

De controle kijkt naar het top-level veld ``clusters``, want daar zegt een projectbestand
voor welk cluster het bedoeld is. Gemeten op de projectenrepo: alle 48 bestanden noemen daar
precies een cluster.

Deze tests roepen de echte functie aan die save_and_commit_project gebruikt.
"""

import pytest
from opi.core.config import settings
from opi.core.project_schema import ProjectClusterOwnershipError
from opi.manager.project_manager import assert_cluster_owns_project


@pytest.fixture
def op_fundament(monkeypatch):
    monkeypatch.setattr(settings, "CLUSTER_MANAGER", "fundament-poc")


def test_eigen_cluster_mag_opslaan(op_fundament):
    assert_cluster_owns_project({"name": "x", "clusters": ["fundament-poc"]})


def test_vreemd_cluster_wordt_geweigerd(op_fundament):
    with pytest.raises(ProjectClusterOwnershipError) as exc:
        assert_cluster_owns_project({"name": "x", "clusters": ["odcn-production"]})
    bericht = str(exc.value)
    assert "odcn-production" in bericht, "de melding moet zeggen waar het project wel thuishoort"
    assert "fundament-poc" in bericht, "en op welk cluster je nu zit"


def test_meerdere_clusters_wordt_geweigerd(op_fundament):
    """Kan niet werken zolang de AGE-sleutel per cluster verschilt, dus luid weigeren."""
    with pytest.raises(ProjectClusterOwnershipError) as exc:
        assert_cluster_owns_project({"name": "x", "clusters": ["fundament-poc", "odcn-production"]})
    assert "meerdere clusters" in str(exc.value)


def test_zonder_clusters_valt_terug_op_deployments(op_fundament):
    """Oudere vorm zonder clusters-veld: dan bepalen de deployments het."""
    assert_cluster_owns_project({"name": "x", "deployments": [{"name": "d", "cluster": "fundament-poc"}]})
    with pytest.raises(ProjectClusterOwnershipError):
        assert_cluster_owns_project({"name": "x", "deployments": [{"name": "d", "cluster": "odcn-production"}]})


def test_leeg_project_mag_opslaan(op_fundament):
    """Een project in aanbouw is niet van een ander cluster; blokkeren zou aanmaken breken."""
    assert_cluster_owns_project({"name": "nieuw"})
    assert_cluster_owns_project({"name": "nieuw", "deployments": []})


def test_geldt_ook_vanaf_productie(monkeypatch):
    """De grendel werkt beide kanten op, niet alleen om productie te beschermen."""
    monkeypatch.setattr(settings, "CLUSTER_MANAGER", "odcn-production")
    with pytest.raises(ProjectClusterOwnershipError):
        assert_cluster_owns_project({"name": "x", "clusters": ["fundament-poc"]})
