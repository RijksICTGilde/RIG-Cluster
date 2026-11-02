"""De manifesten van de proefmigratie: een kopie binnen het cluster, nergens bereikbaar.

`task keycloak-proefmigratie` past deze manifesten toe in de namespace van `rig-db` en haalt
ze daarna weg. Ze horen bij geen ArgoCD-app, want ze mogen niet blijven staan; daarom leest
alleen deze toets ze.

Wat hier fout kan gaan, gaat stil fout: een Ingress of een LoadBalancer zou een kopie van de
hele identiteitsvoorziening op het web zetten, en args die van de basis afwijken laten de
proef iets anders meten dan productie straks start.
"""

from pathlib import Path

import pytest
import yaml
from tests.test_keycloak_eigen_image import _basis_container

REPO_ROOT = Path(__file__).resolve().parents[3]

PROEF = REPO_ROOT / "keycloak-migration/proefmigratie"

#: Het label waarop de task opruimt. Zonder label op elke resource blijft er iets staan.
PROEF_LABEL = ("rig.nl/proefmigratie", "keycloak")

#: Soorten die een kopie van de identiteitsvoorziening van buiten bereikbaar zouden maken.
NIET_TOEGESTAAN = ("Ingress", "Route", "IngressRoute")


def _documenten() -> list[dict]:
    documenten: list[dict] = []
    for pad in sorted(PROEF.glob("*.yaml")):
        documenten.extend(d for d in yaml.safe_load_all(pad.read_text()) if d)
    return documenten


def _van_soort(soort: str) -> dict:
    gevonden = [d for d in _documenten() if d["kind"] == soort]
    assert len(gevonden) == 1, [d["kind"] for d in _documenten()]
    return gevonden[0]


def test_de_manifesten_bestaan_en_zijn_leesbaar() -> None:
    soorten = sorted(d["kind"] for d in _documenten())
    assert soorten == ["Cluster", "Deployment", "Service"], soorten


@pytest.mark.parametrize("soort", NIET_TOEGESTAAN)
def test_geen_enkele_ingang_van_buiten(soort: str) -> None:
    assert soort not in [d["kind"] for d in _documenten()]


def test_de_service_is_clusterip() -> None:
    """Een NodePort of LoadBalancer zou de kopie buiten het cluster brengen."""
    assert _van_soort("Service")["spec"]["type"] == "ClusterIP"


def test_de_args_zijn_die_van_de_basisdeployment() -> None:
    """De proef meet alleen iets als hij start zoals productie start."""
    proef = _van_soort("Deployment")["spec"]["template"]["spec"]["containers"][0]
    assert proef["args"] == _basis_container()["args"]


def test_de_image_in_git_is_geen_echte_tag() -> None:
    """De task vult de image met yq in voor de apply.

    Stond hier een echte tag, dan zou `kubectl apply -f` zonder de task een vaste versie
    tegen een volledige kopie van de identiteitsdatabase starten. Het woord faalt op de
    pull in plaats daarvan.
    """
    proef = _van_soort("Deployment")["spec"]["template"]["spec"]["containers"][0]
    assert proef["image"].endswith(":promoveer-eerst"), proef["image"]


def test_er_vertrekt_geen_mail() -> None:
    """De relay-host wijst naar een adres dat niet bestaat, en nergens staat een echte relay.

    De overige ZAD_MAIL_RELAY-variabelen staan er niet: zonder wachtwoord en afzender
    weigert de provider al bij de eerste verzending.
    """
    container = _van_soort("Deployment")["spec"]["template"]["spec"]["containers"][0]
    mail = {e["name"]: e.get("value") for e in container["env"] if e["name"].startswith("ZAD_MAIL_RELAY_")}
    assert list(mail) == ["ZAD_MAIL_RELAY_HOST"], mail
    assert mail["ZAD_MAIL_RELAY_HOST"].endswith(".invalid"), mail


def test_geen_scrape_token_in_de_proef() -> None:
    """Zonder `RIG_METRICS_TOKEN` is `/rig-metrics` op de kopie dicht (503) en niet open."""
    container = _van_soort("Deployment")["spec"]["template"]["spec"]["containers"][0]
    assert "RIG_METRICS_TOKEN" not in [e["name"] for e in container["env"]]


def test_de_proef_draait_tegen_de_kopie_en_niet_tegen_rig_db() -> None:
    container = _van_soort("Deployment")["spec"]["template"]["spec"]["containers"][0]
    db_url = next(e["value"] for e in container["env"] if e["name"] == "KC_DB_URL")
    assert "keycloak-proef-rw" in db_url, db_url
    assert "rig-db" not in db_url, db_url


def test_de_kopie_leest_rig_db_met_de_rol_van_keycloak() -> None:
    """Dezelfde rechten als de pod die deze data schrijft, dus niets extra's."""
    extern = _van_soort("Cluster")["spec"]["externalClusters"]
    assert len(extern) == 1, extern
    assert extern[0]["connectionParameters"]["user"] == "keycloak", extern
    assert extern[0]["password"]["name"] == "keycloak-db-credentials", extern


def test_elke_resource_draagt_het_opruimlabel() -> None:
    naam, waarde = PROEF_LABEL
    for document in _documenten():
        labels = document["metadata"].get("labels", {})
        assert labels.get(naam) == waarde, (document["kind"], labels)


def test_de_podtemplate_en_de_pvcs_dragen_het_label_ook() -> None:
    """Zonder dit label op de pods en PVC's van CNPG blijft er na het opruimen opslag staan."""
    naam, waarde = PROEF_LABEL
    cluster = _van_soort("Cluster")
    assert cluster["spec"]["inheritedMetadata"]["labels"][naam] == waarde
    pod = _van_soort("Deployment")["spec"]["template"]["metadata"]["labels"]
    assert pod[naam] == waarde, pod


def test_de_manifesten_noemen_geen_namespace() -> None:
    """De task kiest de namespace; een vaste namespace zou de proef op de sandbox vastpinnen."""
    for document in _documenten():
        assert "namespace" not in document["metadata"], document["kind"]


def test_de_kopie_zet_de_pdb_uit_onder_twee_instances() -> None:
    """Zonder dit weigert `pdb-cnpg-cluster-validation` de proef op ODCN.

    Dezelfde eis als in de odcn-staging-overlay van rig-db en in
    `manifests/postgresql-cluster.yaml.jinja`.
    """
    spec = _van_soort("Cluster")["spec"]
    if spec["instances"] < 2:
        assert spec.get("enablePDB") is False, spec.get("enablePDB")
