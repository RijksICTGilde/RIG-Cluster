"""Het sjabloon en de meetopstelling mogen niet stil van elkaar afdrijven.

De metingen in ``test_authorization_wall_proxy.py`` halen hun vlaggen uit het gerenderde
sjabloon en zetten er een paar om. Verdwijnt of hernoemt een van die vlaggen, dan faalt de
harness luid, maar die metingen hebben Docker nodig en slaan zonder Docker over. Dezelfde
controle staat hier zonder container, zodat een sjabloonwijziging ook in de gewone suite
rood is.
"""

from tests.integration.authwall_harness import (
    met_vlagwaarde,
    proxy_args_voor_opstelling,
    proxy_url,
    sjabloon_sidecar,
    sjabloon_templates,
    zonder_vlag,
)

_ISSUER = "http://zad-test-keycloak:8080/realms/authwall-test"
_CLIENT_ID = "authwall-wall"


def _opstelling_args() -> list[str]:
    return proxy_args_voor_opstelling(sjabloon_sidecar()["args"], _ISSUER, _CLIENT_ID)


class TestDeOpstellingVolgtHetSjabloon:
    def test_elke_vlag_die_de_opstelling_omzet_staat_er_nog(self) -> None:
        """Vijf vlaggen wijzen naar een cluster dat in de opstelling niet staat.

        De omzetting faalt luid als het sjabloon er een niet meer zet; deze test laat die
        meting ook zonder Docker rood worden.
        """
        args = _opstelling_args()

        assert f"--oidc-issuer-url={_ISSUER}" in args
        assert f"--client-id={_CLIENT_ID}" in args
        assert "--upstream=static://200" in args
        assert f"--redirect-url={proxy_url()}/oauth2/callback" in args
        assert "--cookie-secure=false" in args

    def test_de_vlaggen_van_de_controlemetingen_staan_er_nog(self) -> None:
        """Zonder deze drie vlaggen meet geen van de controlemetingen nog iets."""
        args = _opstelling_args()

        assert len(zonder_vlag(args, "--api-route=")) < len(args)
        assert "--cookie-refresh=5s" in met_vlagwaarde(args, "--cookie-refresh=", "5s")
        assert "--cookie-expire=15s" in met_vlagwaarde(args, "--cookie-expire=", "15s")

    def test_de_inlogkaart_komt_uit_het_sjabloon(self) -> None:
        """F1 leest 'beperkte toegang' uit de kaart die in het proxy-image gebakken wordt."""
        templates = sjabloon_templates()

        assert "sign_in.html" in templates
        assert "beperkte toegang" in templates["sign_in.html"]
