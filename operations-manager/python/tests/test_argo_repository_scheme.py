"""Een gitserver binnen het cluster draait op plain http en moet basic auth krijgen.

De keuze tussen ``argo-repository-https.yaml.jinja`` (username/password) en
``argo-repository.yaml.jinja`` (sshPrivateKey) hing aan ``startswith("https://")``. Een
in-cluster Forgejo op ``http://forgejo.rig-system.svc.cluster.local:3000/...`` viel daardoor
in de SSH-tak: ArgoCD kreeg een secret met een lege sshPrivateKey en zonder wachtwoord, en
kon de deployments-repo niet lezen. TLS voegt binnen het cluster niets toe, dus de URL
https maken om de code tevreden te houden is de verkeerde kant op repareren.

Rood (kale https-check): de http-URL levert False en dus het SSH-sjabloon.
Groen: http en https kiezen allebei het sjabloon met username/wachtwoord, ssh niet.
"""

import pytest
from opi.manager.argo_manager import uses_http_basic_auth


@pytest.mark.parametrize(
    "url",
    [
        "http://forgejo.rig-system.svc.cluster.local:3000/rig-admin/zad-deployments.git",
        "https://forgejo.fundament-poc.rijksapp.dev/rig-admin/zad-deployments.git",
        "https://github.com/RijksICTGilde/rig-cluster-application-test.git",
    ],
)
def test_http_en_https_krijgen_basic_auth(url: str) -> None:
    assert uses_http_basic_auth(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "ssh://git@github.com/org/repo.git",
        "git@github.com:org/repo.git",
        "git://localhost:9090/",
        "",
    ],
)
def test_overige_schemas_blijven_ssh(url: str) -> None:
    assert uses_http_basic_auth(url) is False
