# Validatie van de foutpagina voor de authorization wall en van de opgeschreven oorzaak

**Dit plan is uitgevoerd.** RC-244 heeft het op 5 oktober gedraaid en alle vijf poorten gehaald (`gates_passed=5`, `rework_rounds=3`). Drie bevindingen, waarvan twee een bewering uit dit plan weerleggen: voorafmaken is niet alleen een console-handeling, want een lokale invite-registratie en de `users:`-sectie van een keycloak-template doen het ook en geen van beide zet de vlag; en de onbevestigde uitnodiging zit op `complete_local_invite` en niet op de SSO-tak, want `invite_sso_start` stuurt de uitgenodigde naar het projectrealm zelf zodat `complete_sso_invite` zijn `create_user`-tak nooit bereikt. De derde was een gat in de toets die bij dit werk hoorde: de injectietoets dekte het data-blok van de configmap niet, de sectie waar `error.html` in leeft.

De taak zelf staat op `cancelled` en dat is geen oordeel over de uitkomst: zijn basis `claude/authwall-foutpagina` stamt van GitHub's main en is geen voorouder van `forgejo/main`, dus een merge zou zes commits dubbel in de historie zetten. De bevindingen zijn daarom als negen cherry-picks geland via `claude/rc244-correcties`. Wat hieronder staat is de opdracht zoals hij geschreven is; de secties 1 en 10 beschrijven de stand van 2 oktober en zijn niet bijgewerkt.

Dit plan laat niets bouwen. Het werk staat er al, en wat hier gevraagd wordt is een onafhankelijke toets op de uitwerking en op de beweringen die eromheen zijn opgeschreven. De reden dat dit een eigen taak is: de oorzaak is in de sessie van 30 september twee keer verkeerd opgeschreven en twee keer herzien, dus de causale keten is het onderdeel met het hoogste risico en juist dat staat nu in twee documenten die anderen gaan vertrouwen.

## 1. Waar het staat

Vier commits op `forgejo/main`, in deze volgorde: `5747a3133` (de foutpagina plus test en golden file), `aedff3d5d` (de eerste versie van de known issue), `bd80ffb83` (die known issue herschreven met de juiste oorzaak), `e74731c59` (de waarschuwing in de auto-link-handleiding).

Op GitHub `origin/main` staat hiervan niets. Getoetst op inhoud en niet op SHA, want de GitHub-historie is op 26 september herschreven en draagt dezelfde commit-titels onder andere hashes: `git show origin/main:operations-manager/python/manifests/sidecar-authorization-wall.yaml.jinja | grep -c 'error.html:'` geeft 0, hetzelfde commando tegen `forgejo/main` geeft 1.

In productie staat de pagina niet. Van buiten gemeten op 2 oktober: `GET https://nldd-org.rijks.app/oauth2/callback?state=x&code=y` geeft HTTP 500 met 2771 bytes, `<h1 class="subtitle is-1">Internal Server Error</h1>` en een verwijzing naar `bulma.min.css`. Dat is de ingebouwde pagina van oauth2-proxy, en 2771 bytes is exact het formaat van de 500 uit de oorspronkelijke storing. Deze meting is ook de regressietoets aan het eind van dit plan.

## 2. Wat dit plan niet doet

Niet uitrollen, en niets voorstellen dat uitrollen vraagt. Geen GitHub-PR maken. De negen componenten die door de quota-storing uit staan blijven erbuiten, die hebben hun eigen runbook in `docs/redeploy-na-quota-storing-20260930.md` en hun eigen regel in `TODO.md`. De vier commits over die quota-storing (`d4d9969d7`, `e8d059551`, `26794cadd`, `88112c326`) zijn een andere werkstroom en vallen buiten deze validatie. Er wordt ook niets gebouwd aan de `emailVerified`-kant: dat is een apart besluit en staat als zodanig in de known issue.

## 3. Waar het hardst gekeken moet worden

Twee eerdere verklaringen zijn fout gebleken en zijn uit de documentatie verwijderd. Een validator hoort te weten welke, zodat hij niet dezelfde fout opnieuw goedkeurt.

De eerste foute verklaring was dat het e-mailadres bij de eerste login leeg zou zijn geweest door een tijdvenster waarin de platform-client nog niet gereed was. Die is onjuist omdat `_process_identity_providers` platform-clients juist vóór de identity providers afhandelt, en omdat het adres bij een echte eerste login wel degelijk wordt gezet.

De tweede foute verklaring was dat `syncMode: FORCE` op de identity provider de reparatie zou zijn. Die is onjuist om een reden die makkelijk over het hoofd wordt gezien, en punt 6 hieronder vraagt daar expliciet naar.

## 4. Taak A: rendert het template echt, en in alle takken

Het gaat om de sleutel `error.html` in de configmap-sectie van `operations-manager/python/manifests/sidecar-authorization-wall.yaml.jinja`.

Render de configmap met `render_template` uit `opi.generation.manifests` voor `section: configmap`, haal `data["error.html"]` eruit, en voer dat door Go's `html/template` op dezelfde manier als oauth2-proxy het doet: `template.New("").Funcs({ToUpper, ToLower})` gevolgd door `ParseFiles` op het bestand, want dat is wat `addTemplate` in `pkg/app/pagewriter/templates.go` van v7.7.1 doet. Gebruik de struct uit `WriteErrorPage` in `pkg/app/pagewriter/error_page.go`: `Title`, `Message`, `ProxyPrefix`, `StatusCode`, `Redirect`, `RequestID`, `Footer template.HTML`, `Version`.

Goed als: 403, 500, 502 en een niet genoemde code (bijvoorbeeld 404) elk hun eigen `<h1>` opleveren, het woord "Oops" in geen enkele uitkomst voorkomt, en de knop plus de regel met de `RequestID` verdwijnen wanneer `Redirect` en `RequestID` leeg zijn. Fout als een van de takken een Go-template-fout geeft of een lege kop oplevert.

## 5. Taak B: kloppen de bronverwijzingen in de documentatie

`docs/KNOWN-ISSUES.md` en `features/keycloak-auto-link.md` doen uitspraken over Keycloak-gedrag en noemen bestanden en een regelnummer. Toets elke uitspraak tegen tag `25.0.6` van `keycloak/keycloak`, want dat is de versie die in productie draait (`rcr.rijksapps.nl/quay-rig/keycloak/keycloak:25.0.6`).

Te toetsen, met per punt een pass of fail:

De tak die `emailVerified` op true zet staat in `services/src/main/java/org/keycloak/services/resources/IdentityBrokerService.java` binnen `if (Boolean.parseBoolean(isRegisteredNewUser))`, en de voorwaarde bevat naast `isTrustEmail()` ook `!Validation.isBlank(federatedUser.getEmail())` en de auth note `UPDATE_PROFILE_EMAIL_CHANGED`. Het regelnummer 738 is geen contract, de plaatsing binnen die if wel.

`IdpAutoLinkAuthenticator.authenticateImpl` doet niets anders dan `context.setUser(existingUser)` en `context.success()`, en zet dus geen `BROKER_REGISTERED_NEW_USER`.

`IdpCreateUserIfUniqueAuthenticator` zet die auth note wel, en zet het adres niet rechtstreeks maar via de lus over `serializedCtx.getAttributes()`.

`SerializedBrokeredIdentityContext.setEmail` slaat het adres op als attribuut `email`, en `UserAdapter.setAttribute` in `model/jpa` routeert `UserModel.EMAIL` door naar `setEmail`. Dat tweetal is de reden dat een echte eerste login wél geverifieerd uitkomt, en dus de reden dat de storing per gebruiker leek te wisselen.

De `else`-tak voor een gekoppelde gebruiker roept alleen `updateFederatedIdentity` aan, en die roept `setBasicUserAttributes` uitsluitend aan wanneer de sync mode van de provider `FORCE` is.

Onze provider staat op `IMPORT`: `opi/configs/keycloak/sso-only.yaml`, in het blok van `rig-platform-oidc`.

## 6. Taak C: zijn de twee uitgesloten oplossingen echt uitgesloten

Dit is de kern van de review, want hier zat de tweede fout.

Toets of `syncMode: FORCE` een al kapot account repareert. De verwachte uitkomst is nee, omdat `setBasicUserAttributes` via `setDiffAttrToConsumer` loopt en die de consumer alleen aanroept wanneer de nieuwe waarde verschilt van de huidige. Bij een account waarvan het adres al goed staat gebeurt er dus niets. Fout als de validator een pad vindt waarlangs het tóch wordt herberekend, want dan is de known issue op dit punt onjuist en is er een goedkopere oplossing dan daar nu staat.

Toets of een `hardcoded-attribute-idp-mapper` de vlag kan zetten. De verwachte uitkomst is nee, omdat `setSingleAttribute` alleen `firstName`, `lastName`, `email` en `username` als speciale gevallen kent en `emailVerified` dus een gewoon attribuut zou worden. Fout als er in 25.0.6 een mapper of authenticator bestaat die de vlag wel kan zetten, want dan hoort die in de known issue te staan.

Toets of ZAD echt geen weg heeft om een projectgebruiker vooraf aan te maken. De bewering is dat dit een handmatige actie in de Keycloak-console is, en dat is wat de waarschuwing in `features/keycloak-auto-link.md` erop baseert. Meet het met de aanroepplekken van `create_user` met een `realm_name` in `opi/`, en met de velden in `opi/services/catalog/keycloak/editables.py`. Fout als er een portal- of API-weg blijkt te zijn, want dan hoort de fix daar en niet in de handleiding.

## 7. Taak D: security

De configmap wordt gegenereerd uit een Jinja-template met waarden die uit het projectbestand komen. `tests/test_authwall_banner_yaml_injection.py` legt die klasse vast voor de container- en metadata-secties. De bewering over het nieuwe blok is dat het geen enkele Jinja-interpolatie bevat en dus geen nieuwe scalar met gebruikersinvoer in de YAML zet. Toets die bewering, en toets daarbij ook of een projectnaam of banner met een nieuwe regel erin het `error.html`-blok niet uit zijn scalar kan duwen.

Toets daarnaast of de pagina niets prijsgeeft wat een eindgebruiker niet hoort te zien. `Message` wordt bewust niet afgedrukt, omdat die buiten debug-mode de Engelse standaardtekst per statuscode is en in debug-mode de interne foutmelding. Goed als het template `.Message` niet gebruikt en de pagina geen interne pad-, versie- of foutinformatie toont behalve de `RequestID`.

## 8. Taak E: de Python-kant

`uv run pytest tests/test_authwall_error_page.py tests/test_golden_manifests.py tests/test_authwall_banner_yaml_injection.py tests/test_manifests.py tests/test_keycloak_master_admins_slaan_verificatie_over.py -q` hoort groen te zijn, en `uv run ruff check .`, `uv run ruff format --check .` en `uv run pyright` over de gewijzigde bestanden ook.

Twee dingen die expliciet ongemoeid moeten zijn gebleven. De guard uit RC-191 in `tests/test_keycloak_master_admins_slaan_verificatie_over.py` is niet aangepast, en `opi/manager/invite_manager.py` is niet gewijzigd: in de sessie is daar een wijziging gemaakt en weer teruggedraaid, dus een diff tegen `origin/main` op dat bestand hoort leeg te zijn. Fout als er toch een `skip_email_verification` op de invite-weg staat.

Het golden file `tests/golden/manifests/sidecar-authorization-wall-configmap.golden.yaml` hoort overeen te komen met wat het template nu rendert, en hoort beide sleutels te bevatten.

## 9. Poorten

Vijf poorten, en elk hoort apart groen of rood te geven zodat `gates_passed` leesbaar blijft: `template` (taak A), `bron` (taak B), `uitsluiting` (taak C), `security` (taak D), `python` (taak E).

De poort `uitsluiting` is de enige die mag leiden tot nieuw werk in plaats van tot een goedkeuring. Vindt iemand daar een pad dat de vlag wel herberekent of wel kan zetten, dan is dat een vondst die de known issue verandert en die hoort terug te komen als voorstel, niet als stille aanpassing.

## 10. Wat ik niet heb kunnen meten, en wat dus niet als bewezen mag gelden

De feitelijke `emailVerified` op het getroffen account is nooit uitgelezen: het uitlezen van `keycloak-admin-credentials` werd twee keer door de permissie-classifier geblokkeerd. Dat de vlag op false stond is afgeleid uit de foutmelding van oauth2-proxy en bevestigd doordat de gebruiker hem met de hand op On zette en daarna binnenkwam.

Of de twee gebruikers die wel binnenkwamen vooraf waren aangemaakt of bij hun eerste login zijn ontstaan, is niet gemeten. Keycloak-auditevents staan in productie uit. De verklaring daarvoor is afgeleid uit de code en staat als zodanig in de known issue.

Of de negen componenten uit de quota-storing er nu nog zo staan is niet gecontroleerd: de cluster-API was op het moment van schrijven onbereikbaar vanaf deze machine.

## 11. Hoe je weet dat het werkt

Het plan is geslaagd als de vijf poorten groen staan en als elke bewering uit taak B en C met een bronverwijzing is bevestigd of met een bronverwijzing is weerlegd. Een weerlegging is een geldige uitkomst en hoort de documentatie te wijzigen.

De uiteindelijke bevestiging in productie valt buiten dit plan, maar het commando staat hier zodat het later niet opnieuw bedacht hoeft te worden: `curl -s -o /tmp/p.html -w '%{http_code} %{size_download}\n' 'https://nldd-org.rijks.app/oauth2/callback?state=x&code=y'`. Zolang daar 500 met 2771 bytes uitkomt en `bulma.min.css` in het antwoord staat, draait de oude pagina nog.
