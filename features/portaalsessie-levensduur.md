# Levensduur van een portaalsessie

## Wat het is

Een sessie in het ZAD-portaal eindigt uiterlijk een vaste tijd na het inloggen, ook als je
de hele dag doorwerkt, en uitloggen maakt elke sessie van jou ongeldig, op elk apparaat.
Een platformbeheerder kan dat laatste ook voor een ander doen.

Daarvoor gelden drie grenzen naast elkaar:

| grens | instelling | standaard | wat hij doet |
|---|---|---|---|
| inactiviteit | `SESSION_MAX_AGE_SECONDS` | 8 uur | De cookie wordt bij elke sessie-mutatie opnieuw getekend en de server-side rij verlengt bij activiteit: je sessie vervalt na zoveel tijd zonder verzoek. |
| absolute levensduur | `SESSION_ABSOLUTE_MAX_SECONDS` | 12 uur | Gerekend vanaf het inloggen, hoe actief je ook bent. Daarna log je opnieuw in. |
| intrekken | geen | | Uitloggen, of een platformbeheerder, maakt elke sessie van dat e-mailadres ongeldig: de rijen verdwijnen ook echt (server-side sessies, zie `server-side-sessies.md`). |

Alle drie worden ze afgedwongen in de middleware, in de handshake van de logs-WebSocket en
op `/api/logs/pods/...`: ze lezen dezelfde sessie, opgebouwd uit de cookie (alleen het id)
en de rij erachter.

## Hoe het werkt

Bij het inloggen zet `/auth/callback` het tijdstip in de sessie als `login_at`. Elk verzoek
loopt daarna langs `valid_session_user` in `opi/core/session_guard.py`. Die weigert een
sessie die geen `login_at` draagt, die ouder is dan de absolute levensduur, of waarvan
`login_at` voor het intrekkingsmoment van die gebruiker ligt. Een geweigerde sessie wordt
ook in de browser weggegooid, zodat dezelfde cookie niet elk verzoek opnieuw wordt
afgekeurd, en je komt op de inlogpagina.

Het intrekken staat in de tabel `session_revocations` in rig-db: een rij per e-mailadres
met het moment waarvoor sessies vervallen. De `UserService` houdt die tabel in het geheugen
bij, zoals hij dat met de allowlist doet, zodat de controle per verzoek geen database
raakt. Bij het opstarten wordt het geheugen uit de tabel gevuld, dus een pod die herstart
weigert dezelfde sessies nog. Dit leunt erop dat er een OPI-pod per cluster draait.

## Sessies van een gebruiker beëindigen

Op `/admin/users` staat bij elke gebruiker de knop "Sessies beëindigen", achter dezelfde
controle als de rest van die pagina: alleen een platformbeheerder komt erbij. De handeling
komt met wie, voor wie en vanaf wanneer in de log terecht, tot er een gebeurtenissentabel
is.

De knop eindigt de portaalsessie, niet de SSO-sessie bij Keycloak: bij een volgend bezoek
krijgt de gebruiker daar stil een nieuwe sessie, met een vers inlogtijdstip. Gebruik hem om
een sessie af te sluiten, niet om een account buiten te houden. Daarvoor haal je de
gebruiker van `/admin/users` af, want dan valt het e-mailadres uit de allowlist.

## Bij de uitrol

Een sessie van voor deze wijziging draagt geen `login_at` en vervalt daarmee: iedereen die
op het moment van de uitrol is ingelogd, logt een keer opnieuw in. Kondig dat aan.

## Wat hier niet in zit

- Backchannel-logout vanuit Keycloak naar OPI. Logt iemand ergens anders uit zijn
  Keycloak-sessie, dan merkt het portaal dat pas bij het volgende inloggen.
- De sessies van de authorization wall voor tenant-applicaties. Dat is oauth2-proxy, met
  een eigen levensduur.

## Belangrijkste bestanden

- `opi/core/session_guard.py` - de controle, gedeeld door de middleware en de WebSocket
- `opi/core/config.py` - `SESSION_MAX_AGE_SECONDS`, `SESSION_ABSOLUTE_MAX_SECONDS`
- `opi/api/auth_routes.py` - `login_at` bij het inloggen, intrekken bij het uitloggen
- `opi/services/session_revocation_service.py` - intrekken en inlezen
- `opi/services/persistence/session_revocations.py`, `opi/migrations/versions/008_add_session_revocations.py`
- `opi/web/router_user_admin.py` - de actie voor de platformbeheerder
- `opi/templates_lotc/bg/admin-users.html.j2` - de knop en zijn bevestiging
