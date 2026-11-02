# De sessiecookie verdwijnt als de browser sluit

Wie `https://zad.rijksapp.nl/dashboard` opent nadat de browser is afgesloten, is nog ingelogd. Dat hoort niet: een portaalsessie hoort bij een browsersessie.

## Oorzaak (gemeten)

`ServerSideSessionMiddleware` zet de sessiecookie met `Max-Age={SESSION_MAX_AGE_SECONDS}` (`opi/middleware/server_side_session.py:245`, 8 uur via `opi/server.py:805` en `opi/core/config.py:199`). Een cookie met `Max-Age` is persistent: de browser bewaart hem na afsluiten. Binnen het inactiviteitsvenster is de sessie op de server nog geldig, dus je bent gewoon weer binnen.

Eén waarde, `max_age`, doet in de middleware twee dingen:

1. het `Max-Age`-attribuut op de cookie: hoe lang de browser hem bewaart;
2. `self.signer.unsign(raw_value, max_age=self.max_age)` (`server_side_session.py:210`): hoe lang de server een ondertekening accepteert.

Het tweede is de inactiviteitsgrens en blijft. Het eerste moet weg.

Waarom het er staat: de test `tests/test_session_cookie_max_age.py` zegt "before this was added the cookie had no Max-Age and the session never expired server-side". Dat klopte onder Starlette's `SessionMiddleware`, waar `max_age` beide rollen had. Sinds RC-390 (server-side sessies) handhaaft de server de grens zelf, op de ondertekening en met de store-rij (`opi/services/session_store.py:51`). De cookie hoeft daar niet meer voor te zorgen.

## Wat er moet komen

### 1. Middleware: de cookie wordt een sessiecookie

- `opi/middleware/server_side_session.py`: het `Max-Age`-attribuut verdwijnt uit de `Set-Cookie` bij een mutatie (`:241-247`). `max_age` blijft een parameter van de middleware en blijft de inactiviteitsgrens op `unsign`. Het verwijderpad (`:250-258`, `expires=Thu, 01 Jan 1970`) blijft zoals het is.
- Geen nieuwe instelling of vlag om het persistente gedrag terug te zetten (YAGNI). Is er toch een reden om het configureerbaar te maken, zeg dat in de PR.
- Pas de docstring of het commentaar bij `max_age` aan zodat duidelijk is dat het de servergrens is, niet de levensduur van de cookie. Kort, en in het Engels.
- `opi/server.py:793-806` blijft verder gelijk: `same_site="lax"`, `https_only`, `max_age=settings.SESSION_MAX_AGE_SECONDS`.

### 2. Tests

- `tests/test_session_cookie_max_age.py`: hou de bestandsnaam (de ASVS-zelfbeoordeling, `docs/asvs-zelfbeoordeling.md:88-91`, verwijst ernaar), maar draai de bewering om en herschrijf de module-docstring:
  - `Set-Cookie` bij een mutatie draagt geen `Max-Age` en geen `expires`;
  - `httponly`, `samesite=lax`, `path=/` en, bij `https_only=True`, `secure` staan er nog op (V3.3.1, V3.3.2 en V3.3.4 in de ASVS-tabel steunen op deze file, dus die moeten aantoonbaar blijven);
  - `test_create_app_wires_session_max_age` blijft: de servergrens moet nog steeds bedraad zijn. Pas de docstring aan zodat hij de servergrens beschrijft en niet de cookie.
- `tests/test_server_side_session.py:136` (`test_the_cookie_has_max_age_httponly_and_path`): hernoemen en omdraaien, geen `Max-Age` meer.
- Nieuw, en dit is de kernbewering: een cookie die geldig getekend is maar ouder dan `max_age` wordt nog steeds geweigerd (de sessie is leeg, de handler ziet geen gebruiker). Bevries de tijd met de bestaande aanpak in die testmodule, of met `freezegun` als die al gebruikt wordt. Zonder deze test bewijst de taak alleen dat een attribuut weg is, niet dat de grens blijft staan.
- `tests/test_portaalsessie_levensduur.py` en `tests/test_sessies_intrekken.py` blijven ongewijzigd groen.

### 3. Andere cookies (nagelopen, geen wijziging)

- CSRF-cookie (`opi/utils/csrf.py:103`): zonder `max_age`, dus al een sessiecookie. Geen wijziging.
- Kleurschema-cookie (`opi/web/router.py:2909`, `SCHEME_COOKIE`, een jaar): een weergavevoorkeur, geen authenticatie. Geen wijziging.
- Keycloak: `rememberMe` staat op `False` in `opi/connectors/keycloak.py:255` en `:2266`. Zonder remember-me zijn `KEYCLOAK_IDENTITY` en `KEYCLOAK_SESSION` sessiecookies, dus ook die verdwijnen bij afsluiten. Dat is configuratie en valt buiten deze code. Na de uitrol nakijken in de browser (zie hieronder).

Vind je bij het bouwen nog een cookie die authenticatie draagt en persistent is, noem hem in de PR en wijzig hem niet zonder overleg.

### 4. Documentatie

- `features/portaalsessie-levensduur.md`: in de tabel bij "inactiviteit" en in een korte nieuwe alinea: de cookie is een sessiecookie en verdwijnt als de browser sluit; de inactiviteitsgrens zit op de server (de ondertekening en de store-rij), niet in de cookie. Noem dat een browser die tabbladen herstelt (Chrome "Doorgaan waar je was gebleven", Firefox sessieherstel, Safari) ook sessiecookies terugzet, en dat op macOS een venster sluiten de browser niet afsluit. Dan blijven de 8 en 12 uur de grens.
- `features/server-side-sessies.md:60`: de regel over inactiviteit en de ondertekening van de cookie zo bijwerken dat hij niet suggereert dat de cookie zelf vervalt.
- `docs/asvs-zelfbeoordeling.md`: alleen aanpassen als een verwijzing door deze taak niet meer klopt.

## Wat wordt schrijfbaar, welke guard

Niets. Er komt geen schrijfweg bij, en geen grens wordt ruimer: de servergrens op inactiviteit en de absolute grens blijven zoals ze zijn. De cookie leeft korter (tot de browser sluit) of even lang (een browser die sessies herstelt), nooit langer dan de server toestaat.

## Afspraken

- Code, identifiers, testnamen, commentaar en docstrings in het Engels; docs in het Nederlands.
- Geen em dashes.
- Bestaande helpers en de bestaande in-memory store (`tests/inmemory_session_store.py`) hergebruiken.
- `uv run ruff check . --fix`, `uv run ruff format .`, `uv run pyright` groen.

## Buiten scope

- Uitrollen. Na de uitrol, met de hand: in de browser-devtools nakijken dat `session` en de Keycloak-cookies op "Session" staan, en dat de browser na echt afsluiten (zonder tabbladherstel) op de inlogpagina uitkomt.
- Een kortere inactiviteits- of absolute grens.
- Backchannel-logout vanuit Keycloak.
- De cookies van oauth2-proxy (authorization wall voor tenant-applicaties).
