# Meetopstelling voor de authorization wall

Een echte oauth2-proxy voor een echte Keycloak, waarin een storing van de muur eerst
wordt gereproduceerd en daarna verdwijnt. Twee containers, geen cluster en geen sandbox.

## Waarom dit bestaat

De vlaggen op de wall waren alleen getoetst als tekenreeks in een gegenereerd manifest:

```python
assert "--cookie-refresh=3m" in args
```

Zo'n assertie slaagt ook als de vlag niets doet. Ze legt vast dat er iets in het sjabloon
staat, niet dat een browser een werkende pagina krijgt. Deze opstelling draait dat om: per
storing staat er een **controlemeting zonder de vlag**, die de storing reproduceert, naast
de meting met de vlag. Een test die alleen de goede kant toetst weet niet of de storing
ooit bestond.

## Gebruik

```bash
task test-authwall          # draait de metingen
task test-authwall-stop     # haalt de containers weg
task test-authwall-reset    # weg en opnieuw
```

Vraagt werkende Docker. De tests staan in `tests/integration/test_authorization_wall_proxy.py`
en zijn gemarkeerd `requires_infra`, dus de gewone testrun slaat ze over. Zonder Docker
slaan ze zichzelf over in plaats van rood te worden.

De Keycloak blijft met opzet staan tussen runs, zoals de vaste test-Postgres van de
ORM-tests: hergebruik is het punt, en wat een mens kan noemen (`zad-test-keycloak`) kan hij
opruimen.

## Hoe het in elkaar zit

| container | rol |
|---|---|
| `zad-test-keycloak` | `quay.io/keycloak/keycloak:25.0.6`, dezelfde versie als productie, in dev-mode |
| `zad-test-authwall-proxy` | hetzelfde oauth2-proxy-image als het sjabloon, met `--upstream=static://200` |

De realm wordt opgebouwd met OPI's eigen connectorcode (`create_realm`,
`create_deployment_client`, `create_user`), niet met een handgeschreven realm-import, zodat
de meting toetst wat OPI werkelijk aanmaakt.

De vlaggen komen **uit het sjabloon**: `authwall_harness.sjabloon_sidecar()` rendert
`sidecar-authorization-wall.yaml.jinja` en leest de args uit het resultaat. Een test die de
vlaggen opnieuw opschrijft toetst zijn eigen kopie, en dan lopen sjabloon en test uit elkaar
zonder dat iets rood wordt. Om dezelfde reden rendert de opstelling ook de ConfigMap-sectie
van hetzelfde sjabloon: de inlogkaart die de meting terugkrijgt is de echte.

Dat de opstelling nog bij het sjabloon past is apart gepind in
`tests/integration/test_authwall_harness.py`, en die test heeft geen Docker nodig. Een vlag
die uit het sjabloon verdwijnt of een andere naam krijgt wordt dus ook rood in de gewone
testrun, waar de metingen zelf worden overgeslagen.

Drie dingen kosten uren als je ze niet weet: de issuer moet voor de proxy en de testclient
dezelfde naam hebben, `--cookie-secure` moet uit, en `--custom-templates-dir` komt uit een
image en niet uit een mount. Ze staan uitgeschreven in de moduledocstring van
`tests/integration/authwall_harness.py`, bij de code die ze opvangt.

## Wat er gemeten wordt

| meting | zonder de vlag | met de vlag |
|---|---|---|
| `--api-route`, aanvraag om `/bediening.css` zonder sessie | 403, `text/html`, 2315 bytes inlogkaart | 401, geen HTML, 2 bytes |
| `--cookie-refresh`, paginalading voorbij de cookie-grens | 403 op het document, 401 op elke subresource | alle vijf 200, cookie herschreven |
| splitsing van het sessiecookie bij 80 realmrollen | `_oauth2_proxy_0` + `_1`, en de pagina werkt | n.v.t., dit is geen vlag |

De 2315 bytes zijn dezelfde orde als de 163 gevallen in de productielogs van
`rig-prd-mpfm-w3h` (2313 tot 2315 bytes per stuk).

De klokken van de tweede rij zijn teruggeschaald zodat een test seconden duurt in plaats
van tien uur: token 10s, refresh 5s, cookie-expiry 15s. De **ordening** is wat de storing
bepaalt en die blijft gelijk aan productie (refresh < token < expiry). De
productiewaarden zelf staan in het sjabloon en worden in `tests/test_manifests.py` getoetst,
inclusief die ordening.

## Wat er niet in zit

Geen Playwright en geen browser: de storing zit in statuscodes en content-types, en die
zijn met een HTTP-client te meten. Geen sandboxcluster: deze opstelling heeft er niets aan
nodig, en de sandbox is een gedeelde bron met een claim-mechanisme.

## Afhankelijkheden

- Werkende Docker, en netwerktoegang voor de twee images bij de eerste run.
- `features/authorization-wall.md` voor wat de vlaggen zelf doen.
- `plans/authorization-wall-vervolg-sessieherstel.md` voor het open gebleven geval.
