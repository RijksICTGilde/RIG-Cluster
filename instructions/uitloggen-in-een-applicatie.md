# Uitloggen in een applicatie op dit platform

Het contract, in een regel: **wis je eigen sessie voordat je de browser naar het
end-session endpoint van Keycloak stuurt.** Reken er niet op dat de browser via
`post_logout_redirect_uri` bij je terugkomt, want in deze federatie komt hij niet terug.

Dit geldt voor elke applicatie die via Keycloak inlogt, niet alleen voor ZAD.

## Waarom de terugreis niet komt

De keten loopt van de applicatie via het projectrealm naar `rig-platform` en vandaar naar
SSO Rijk (BZK). BZK's uitlogendpoint toont een pagina en stuurt geen SAML `LogoutResponse`
terug. Keycloak zou daar eindeloos op blijven wachten, dus `rig-platform` gebruikt de
`always-clear-session-logout`-shim: die wist de sessie eerst en stuurt de browser daarna
naar BZK. Zie `features/keycloak-always-clear-session-logout.md`, inclusief waarom je de
shim niet moet "repareren" om de keten wel terug te laten komen: dat is gemeten en het is
slechter.

Het gevolg, en dat is het stukje dat je moet weten: **de keten eindigt op de
uitlogpagina van BZK.** Daar staat de gebruiker, en daar blijft hij. Keycloaks
`finishBrowserLogout` draait niet, dus er wordt niemand teruggestuurd en de iframes die
front-channel logout zouden uitvoeren worden nooit gerenderd.

Dat is op productie gemeten; de meting staat in de sectie "De terugreis komt niet" van
`features/keycloak-always-clear-session-logout.md`. In je log is het te zien aan de tweede
klik op uitloggen: die ruimt de applicatiesessie alsnog op en levert
`LOGOUT_ERROR ... error="session_expired"` op omdat de Keycloak-sessie er dan al niet meer
was. Dat is geen storing.

## Zo doe je het goed

`request.session.clear()` **voor** de redirect. ZAD doet dat in
`opi/api/auth_routes.py` (`logout`): sessie leeg, gebruiker uit de user-service, en dan
pas naar het end-session endpoint. Daarom heeft ZAD dit probleem niet.

```python
request.session.clear()
return RedirectResponse(end_session_url)
```

De vorm verschilt per framework, de regel niet: wat je zelf kunt opruimen ruim je op
voordat je de besturing uit handen geeft.

## Kun je dat niet zelf?

Dan kan het platform het voor je laten aanroepen, met `logout-path` op het
`keycloak`-dienstblok. Keycloak zet dat pad als front-channel logout URL op de client van
je deployment. Zie `features/keycloak-logout-path.md`.

Let op de beperking: **binnen deze federatie wordt die URL niet aangeroepen, en dat gaat
niet veranderen.** Het projectrealm hoort de uitlog als server-naar-server POST, en zo'n
verzoek heeft geen browser om de iframes in te renderen; twee manieren om dat te omzeilen
zijn gemeten en vallen af. `logout-path` helpt je dus alleen als iemand rechtstreeks op je
projectrealm inlogt. De regel bovenaan dit document is daarom geen tijdelijke tussenweg
maar het antwoord.

## Valkuilen bij het meten

- **Een blijvende `KEYCLOAK_SESSION`-cookie bewijst niet dat de sessie leeft.** Alleen
  `finishBrowserLogout` wist die cookie, en die draait per ontwerp niet. Meet sessies via
  `/admin/realms/<realm>/users/<id>/sessions`.
- **Keycloak logt geslaagde logouts op DEBUG.** Stilte in het log betekent niet dat er
  niets gebeurd is. De faalhandtekeningen zijn
  `KC-SERVICES0057 Logout for client ... failed` (POST mislukt),
  `LogoutToken verification ... failed` (token afgewezen), `logout_failed` (botsing) en
  `session_expired` (geweigerd voor alles). Grep op die letterlijke tekst: "failed to
  logout" matcht niets.
- **Twee clients met bijna dezelfde naam.** `<project>-<cluster>-platform` in realm
  `rig-platform` is het applicatierealm als afnemer van het platform;
  `<project>-<deployment>` in het applicatierealm is de applicatie zelf.
