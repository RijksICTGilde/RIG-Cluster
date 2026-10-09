# Rolcontrole in de authorization wall zelf

Status: uitgesteld op 26 augustus 2026, niets van gebouwd. Het idee is gemeten en werkend bevonden, maar de vorm die het zou moeten krijgen (een optionele rol op de dienst) maakt de afhankelijkheid tussen de auth wall en `restrict-access` losser, en dat is een grotere verandering dan de winst nu rechtvaardigt. Dit document bewaart de meting en de ontwerpvraag zodat we het later kunnen oppakken zonder opnieuw te beginnen.

## Waarom dit er ligt

Op 25 augustus 2026 bleek de rolcontrole van `restrict-access` te omzeilen. Ze hing in de forms-subflow van de browser flow, waar de Cookie-stap als alternatief voor staat, dus een bestaande realm-sessie kwam er ongetoetst langs. Zo'n sessie was makkelijk te krijgen bij elke andere client in dezelfde realm. Dat gat is gedicht in commit `eb1f7e51`: de rolcontrole staat nu naast het inloggen in plaats van erin, en draait dus ook op de Cookie-weg. Zie `features/client-access-restriction.md`.

Wat daarna overblijft is geen gat maar een scherpe rand. De wall zelf controleert nog steeds alleen of er een geldige sessie is (`--email-domain=*` plus `email_verified`), en leunt voor de autorisatie volledig op Keycloak. Zolang de poort op de juiste clients hangt is dat genoeg. Maar elke client die iemand aan de realm toevoegt zonder dat iemand hem in `_apply_access_restriction` bijzet, valt buiten de rolcontrole, en dat geldt vandaag voor `additional-clients`, voor de invite-client (bewust) en voor de ingebouwde account-console. Een rolcontrole in de proxy is daar ongevoelig voor, want die kijkt naar het token en niet naar de weg waarlangs de sessie ontstond.

## Waarom we het nu niet bouwen

Het zou een optionele instelling worden, en dat is precies waar de prijs zit. Vandaag is de relatie eenduidig: `authorization-wall` heeft `services/keycloak/config/restrict-access` in zijn `requires`, dus de wall aanzetten dwingt de rolcontrole af en de wizard vergrendelt het vinkje ("Vereist door: Authorization Wall"). Er is één rol, hij staat op één plek, en hij geldt.

Een eigen optionele rol op de wall zet daar een tweede waarheid naast. Dan bestaat er een toestand waarin de wall een rol eist die Keycloak niet eist, en een toestand waarin het andersom is, en een waarin ze allebei iets eisen maar niet hetzelfde. De harde `requires` is dan niet meer vanzelfsprekend, want als de wall zijn eigen rol kan afdwingen, waarom zou `restrict-access` dan nog verplicht zijn. Dat is geen regel die je erbij schrijft, dat is een ontwerpbeslissing over wat de auth wall IS. En het raakt de dienstdefinitie, het configmodel, het JSON-schema, het formulier, de API en de migratie van bestaande projecten.

Voor de duidelijkheid: de winst is diepteverdediging, geen dichten van een openstaand gat. Dat verschil is de reden dat het kan wachten.

## Wat er gemeten is

Alles hieronder is nagemeten op 25 augustus tegen een echte Keycloak 25.0.6 (dezelfde versie als productie) en een echte `quay.io/oauth2-proxy/oauth2-proxy:v7.7.1` (hetzelfde image als in `manifests/sidecar-authorization-wall.yaml.jinja`), niet uit documentatie overgenomen.

De vlag bestaat in ons image en werkt alleen met de keycloak-provider:

```
--allowed-group strings   restrict logins to members of this group
--allowed-role strings    (keycloak-oidc) restrict logins to members of these roles
```

Realmrollen als kale naam (`--allowed-role=allowed-user`), clientrollen als `client-id:rolnaam`. Dat vraagt `--provider=keycloak-oidc` in plaats van het huidige `--provider=oidc`.

Met die twee vlaggen, en verder exact onze sidecar-configuratie:

| | rechtstreeks naar de muur | via een andere client in de realm, daarna de muur |
|---|---|---|
| gebruiker MET de rol | door de muur | door de muur |
| gebruiker ZONDER de rol | Keycloak weigert | **muur weigert, 403** |

In de proxylog: `AuthSuccess ... groups:[... role:allowed-user ...]` voor de rolhouder, `AuthFailure: unauthorized` voor de ander. Ter vergelijking, dezelfde omweg met de huidige configuratie (`--provider=oidc`, geen rolcontrole) vóór de Keycloak-fix: `DOOR DE MUUR (upstream bereikt)`.

Eén ding is verplicht meegeleverd werk en geen detail. De keycloak-provider valideert de audience van het access token, en onze clients zetten zichzelf daar niet in. Zonder aanpassing eindigt elke aanmelding op:

```
Error creating session during OAuth2 callback: audience from claim aud with value [account]
does not match with any of allowed audiences map[proj-app:{}]
```

Opgelost door een `oidc-audience-mapper` met `included.client.audience: <client-id>` op de deployment-client, wat een wijziging in `create_deployment_client` betekent. Daarna liep de hele keten.

## Wat het bouwen zou inhouden

1. `manifests/sidecar-authorization-wall.yaml.jinja`: `--provider=keycloak-oidc` en `--allowed-role=<rol>`. Verifieer met een golden manifest.
2. `AuthorizationWallService.contribute_manifest_context`: de rol in de sjabloonvariabelen zetten. Die leest vandaag alleen de banner, dus de rol moet erbij, in de vorm die oauth2-proxy verwacht (realmrol kaal, clientrol als `client:rol`).
3. `opi/connectors/keycloak.py`, `create_deployment_client`: de audience mapper op de vertrouwelijke client.
4. Een test die vastlegt dat de rol in het manifest dezelfde is als die in `restrict-access`, want twee plekken die hetzelfde moeten zeggen lopen anders uit elkaar.
5. Migratie: bestaande walls krijgen bij de eerstvolgende verwerking een rolcontrole erbij. Wie vandaag binnenkomt houdt toegang, want Keycloak eist die rol al; het verschil raakt alleen wie er via een omweg in kwam.

## Gemeten op 5 oktober 2026, en twee correcties

De projectbestanden in `rig-cluster-projects-github/projects/` zijn geteld. Elf projecten hebben een authorization wall, en de rolpoort staat er zo:

| `restrict-access` | projecten |
|---|---|
| `enabled: true` | ai-3rt, dp-bn7, hwmaw-ovh, fp-unj, no-ks4, toets-hn7 |
| `enabled: false` | mpfb-8wh, mpfm-w3h |
| geen blok | dsm1j2-2ws, jongo-lh2, tvas-7pb |

**Correctie 1.** Dit document stelt hierboven dat de wall aanzetten de rolcontrole afdwingt, en gebruikt dat als argument tegen een eigen rol ("Er is een rol, hij staat op een plek, en hij geldt"). Dat geldt voor 6 van de 11. `locked_by_service="authorization-wall"` is een formuliermechanisme: de wizard vergrendelt het vinkje, maar een projectbestand mag `enabled: false` zeggen en doet dat ook. Voor 5 van de 11 walls is er dus geen rolpoort, en die draaien op authenticatie zonder autorisatie. Dat haalt de bodem onder het hoofdargument voor uitstel weg, en het maakt smaak A zwakker dan hij hierboven lijkt: hergebruik van de `restrict-access`-rol levert bij die vijf niets op, want daar staat geen rol.

**Correctie 2.** Er is **geen aparte Keycloak-client** nodig, en dit document stelt dat ook niet voor. Nodig is een `oidc-audience-mapper` op de client die er al is, omdat `--provider=keycloak-oidc` de `aud`-claim valideert en Keycloak daar standaard `account` in zet. De clients die hierboven genoemd worden (`additional-clients`, de invite-client, de account-console) zijn bestaande clients die buiten de rolcontrole vallen, en dus de scherpe rand, niet een voorstel. Dit stond verkeerd gelezen in een sessie en is het vastleggen waard: er komt geen tweede inlogsplitsing bij.

**Vervallen rand.** Hierboven staat dat onze sidecar geen `--cookie-expire` en geen `--cookie-refresh` zet, dus dat een sessie 168 uur leeft zonder hercontrole en een afgenomen rol pas bij de volgende aanmelding werkt. Sinds commit `e51b5ad25` staat er `--cookie-expire=10h` en `--cookie-refresh=3m`. Of oauth2-proxy `--allowed-role` bij een refresh opnieuw toetst en niet alleen bij het aanmaken van de sessie is niet gemeten; dat hoort bij het oppakken getoetst te worden en niet aangenomen.

## De vraag die hier nog niet stond: per component

De wall hangt aan een component, maar zijn config zit op projectniveau: `config_api_fields` en `config_editables` geven alleen iets terug voor `ConfigLayer.PROJECT`, en dat ene veld is de bannertekst. Het gebruik waar deze wens uit voortkomt is per applicatie: iedereen mag inloggen, maar alleen wie `admin` heeft opent **deze** app. Een project met drie componenten achter een wall wil dan drie verschillende rollen kunnen zetten. Dat is geen extra veld maar een nieuwe configuratielaag voor deze dienst, en de comment in `authorization_wall/__init__.py` houdt die laag vandaag bewust leeg omdat de banner projectbreed is.

Dat verandert ook hoe smaak B gelezen hoort te worden. De aanname hierboven is dat een wallrol naast de `restrict-access`-rol een tweede waarheid over hetzelfde zet. In de lezing die de wens voedt zijn het twee verschillende poorten: `restrict-access` beantwoordt "mag je authenticeren bij dit realm", de wallrol beantwoordt "mag je deze applicatie openen". Dat is geen duplicaat maar een laag eronder, en als dat de bedoeling is hoort niet alleen het veld erbij, maar hoort ook de harde `requires` plus de vergrendeling heroverwogen te worden. Anders bestaat het veld wel, maar blijft de interessantste stand (iedereen kan inloggen, alleen admin komt erdoor) onbereikbaar via de wizard.

## Het tussenscherm, een losse vraag

Hoort hier omdat het dezelfde dienst raakt, maar het is een andere afweging dan de rolcontrole.

De wall antwoordt een sessieloze navigatie met een 403 en een inlogkaart met een knop (`--skip-provider-button=false`). Bij een levende SSO-sessie in Keycloak is dat een scherm van ongeveer twee seconden: de bediener klikt op Inloggen en is binnen zonder wachtwoord. In de productielogs van `rig-prd-mpfm-w3h` is dat terug te zien, bijvoorbeeld op 1 oktober 07:26:41 en 07:27:13, telkens 403 op `/`, twee seconden later `/oauth2/start`, en daarna binnen. Het registreert nauwelijks als scherm, en daardoor is het lang onopgemerkt gebleven.

Met `--skip-provider-button=true` verdwijnt die klik: de muur stuurt rechtstreeks door en Keycloak beslist of er een scherm nodig is. De prijs is dat `sign_in.html` dan nooit meer rendert, en dat is de enige plek waar `--banner` staat. Vijf projecten hebben daar echte tekst: hwmaw-ovh, jongo-lh2, dp-bn7, fp-unj ("Toegang op uitnodiging") en no-ks4 ("Dit is invite only."). Bij de laatste twee vertelt die tekst iemand juist waarom hij geweigerd kan worden, dus stil laten vervallen is geen optie.

De wegen, geen van de drie gekozen: de kaart houden voor projecten met een banner (maar dan blijft het tussenscherm bestaan voor precies de projecten die een boodschap hebben), de tekst naar het Keycloak-inlogthema verhuizen (past bij de wens, maar het is niet uitgezocht of dat per realm kan zonder een thema per project), of de banner laten vervallen en de vijf eigenaren inlichten.

## De ontwerpvraag, en twee smaken

**Smaak A, geen nieuw veld.** De wall gebruikt de rol die al in `services/keycloak/config/restrict-access` staat. Geen schemawijziging, geen tweede waarheid, de `requires` blijft hard, en de gebruiker kiest niets nieuws. Dit is het meeste effect voor de minste complexiteit, en het is niet wat "een optionele rol op de wall" betekent. Als we dit ooit oppakken is dit de variant die eerst op tafel hoort.

**Smaak B, een eigen optioneel veld op de auth-wall-config.** Dit is wat de complexiteit veroorzaakt, en de vragen die er dan liggen:

- Wat betekent een wallrol die verschilt van de `restrict-access`-rol? Twee poorten achter elkaar, dus effectief een EN, en dat moet iemand kunnen zien in het scherm.
- Mag de wallrol gezet zijn terwijl `restrict-access` uit staat? Dan laat Keycloak iedereen in de realm inloggen en houdt alleen de proxy de deur dicht. Dat is een geldige architectuur, maar het is een andere belofte dan de dienst nu doet.
- Blijft `services/keycloak/config/restrict-access` dan in `requires` staan? Zo ja, waarom, en zo nee, wat gebeurt er met de vergrendeling in de wizard (`locked_by_service="authorization-wall"` op `KEYCLOAK_RESTRICT_ACCESS`)?
- Wat doen bestaande projecten? Leeg laten betekent terugvallen op de `restrict-access`-rol, en dan is smaak B in de praktijk smaak A met een ontsnappingsluik.

## Randen die bij het oppakken getoetst moeten worden

De rol wordt gecontroleerd bij het aanmaken van de proxysessie, niet per verzoek. Onze sidecar zet geen `--cookie-expire` en geen `--cookie-refresh`, dus de sessie leeft standaard 168 uur zonder hercontrole (`expiry:168h0m0s ... refresh:disabled` in de proxylog). Iemand de rol afnemen werkt dus pas bij de volgende aanmelding, net als bij Keycloak zelf. Wil je dat korter, dan is dat een aparte keuze over de levensduur van die cookie.

Verder: realmrollen staan standaard in het token, clientrollen alleen als de client in `resource_access` voorkomt, dus de clientrol-variant vraagt een extra controle. En het gedrag bij weigeren blijft een 403 met een pagina, niet een 302, wat voor healthchecks hetzelfde blijft als vandaag.

## De verificatie-opstelling opnieuw opbouwen

Twee containers, geen sandbox nodig. Een `quay.io/keycloak/keycloak:25.0.6` in dev-mode met een realm die met OPI's eigen connectorcode is opgebouwd (`create_deployment_client`, `create_restricted_browser_flow_realm_role`, `set_client_authentication_flow_override`), plus een `oauth2-proxy:v7.7.1` met de vlaggen uit het sjabloon en `--upstream=static://200`. Twee gebruikers, één met en één zonder de rol. Let op één ding dat een uur kost als je het niet weet: de proxy draait in een container en de browser op de host, dus de issuer moet voor allebei dezelfde naam hebben. Zet daarvoor `frontendUrl` als realm-attribuut, anders mint Keycloak tokens met de ene hostnaam terwijl de proxy de andere verwacht en krijg je `id token issued by a different provider`.
