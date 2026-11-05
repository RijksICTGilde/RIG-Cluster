# Het Keycloak-thema in NLDD

## Wat het is

Het thema waarmee Keycloak zijn inlogschermen tekent. Het heet `nl-design-system`, staat in
`images/keycloak-zad/theme/` en wordt door `images/keycloak-zad/Dockerfile` in de image
gezet. Het is gebouwd op het **NLDD Design System** (`@nldd/design-system`), dezelfde
componentenlaag als de portal van ZAD.

Tot RC-424 was dit de release-jar `keycloak-nl-design-system.jar` van
[MinBZK/keycloak-theme](https://github.com/MinBZK/keycloak-theme), gebouwd op ROOS. Wie
inlogde ging van een NLDD-portal naar een ROOS-inlogscherm en weer terug. Die jar is eruit;
de MinBZK-repo blijft bestaan en wordt niet gewijzigd.

## Hoe het eruitziet

Elke pagina heeft dezelfde schil (`login/template.ftl`):

```
nldd-shell (tinted)
└ nldd-page
  ├ slot=header → nldd-skip-link > nldd-top-navigation-bar
  │                 logo-title = ${env.ORGANIZATION}, website-title = de naam van het realm
  │                 slot=utility → de taalkeuze (nldd-menu-bar met een navigation-menu)
  └ nldd-simple-section (gecentreerd)
      └ nldd-container (max 480px)
          ├ nldd-title met de <h1> van de pagina
          ├ nldd-banner met de melding van Keycloak
          └ wat de pagina zelf levert
```

De naam van de organisatie komt uit de omgevingsvariabele `ORGANIZATION`, zoals in het
vorige thema.

## De pagina's

Welke FreeMarker-pagina onze flows bereiken volgt uit de realmblauwdrukken in
`opi/configs/keycloak/` en uit de providers in `images/keycloak-zad/extensions/`.

| Pagina | Wanneer | Gemeten door |
|---|---|---|
| `login.ftl` | inloggen met gebruikersnaam en wachtwoord, en de IdP-knop voor SSO-Rijk | `test_keycloak_image_thema.py` |
| `login-username.ftl` | een flow die de gebruikersnaam apart vraagt; geen blauwdruk van OPI bouwt die | `test_keycloak_image_thema.py`, op een eigen realm |
| `login-password.ftl` | de wachtwoordstap van diezelfde gesplitste flow | idem |
| `login-otp.ftl` | de OTP-stap van `sso-support-otp` en van master | `test_keycloak_image_thema.py` |
| `login-config-totp.ftl` | `CONFIGURE_TOTP`, zie `keycloak-verplichte-otp.md` | idem |
| `login-reset-password.ftl` | wachtwoord vergeten | idem |
| `login-update-password.ftl` | `UPDATE_PASSWORD` | idem |
| `login-update-profile.ftl` | `UPDATE_PROFILE`, en de review-stap van first-broker-login | idem |
| `login-verify-email.ftl` | `VERIFY_EMAIL`, zie `keycloak-mail.md` | idem |
| `login-idp-link-confirm.ftl` | `account-link: confirm`, zie `keycloak-auto-link.md` | met de hand |
| `login-idp-link-email.ftl` | de standaard first-broker-login van Keycloak | met de hand |
| `login-page-expired.ftl` | een verlopen authenticatiesessie | met de hand |
| `select-authenticator.ftl` | "probeer een andere manier" | met de hand |
| `register.ftl` | registratie, waar die aanstaat | `test_keycloak_image_thema.py` |
| `logout-confirm.ftl` | uitloggen bevestigen, zie `keycloak-always-clear-session-logout.md` | idem |
| `info.ftl` | een bevestiging na een actielink uit een e-mail; na wachtwoord vergeten toont Keycloak 26.8 de melding op de inlogpagina | met de hand |
| `error.ftl` | elke fout, en de melding van de `RequireClientRoleAuthenticator` | idem |
| `welcome/index.ftl` | de welkomstpagina van de server | idem |

`user-profile-commons.ftl` tekent de velden van het user profile en wordt door
`register.ftl` en `login-update-profile.ftl` gebruikt.

De vijf pagina's met "met de hand" vragen iets wat de testcontainer niet heeft: een externe
IdP, een flow met meer dan een alternatief, een verlopen sessie, of een actielink uit een e-mail. Er is dus geen toets die
ze rendert; hun beeld beoordeel je op de sandbox.

`login-username.ftl` en `login-password.ftl` horen bij een flow die de twee stappen splitst
(`auth-username-form` plus `auth-password-form`). Geen blauwdruk van OPI bouwt zo'n flow, dus
in productie zie je ze niet. Ze blijven wel in het thema, want een realm die de stappen ooit
splitst mag niet stil terugvallen op de parent `base`; de toets bouwt die flow daarom zelf op
een tweede proefrealm. Ze staan niet op de schermafdrukken: hun opmaak is die van de twee
helften van `login.ftl`, die er wel op staat.

### Wat het thema NIET dekt

- **Geen mailthema.** `emailTheme` blijft leeg; Keycloak levert zelf een `base/email` met
  een Nederlandse bundel. Zie `keycloak-mail.md`.
- **Geen admin-thema.** De admin-console blijft die van Keycloak.
- **De account-console** is de React-console van Keycloak. Het type `account` bestaat alleen
  omdat realms er met `accountTheme` naar wijzen, en is een dunne laag op `base`.
- **Geen reCAPTCHA.** De registratiepagina kent die tak niet meer: hij laadt een script van
  Google, en de inlogpagina haalt niets buiten de image op. Onze realms zetten registratie
  niet aan.

## Waarom de naam `nl-design-system` blijft

De naam klopt inhoudelijk niet meer, maar elke bestaande realm in productie heeft hem in de
database staan (`loginTheme`, `adminTheme`, `accountTheme`). Een nieuwe naam vraagt een
realm-update over de hele vloot, en een realm die naar een onbekend thema wijst valt stil
terug op het standaardthema van Keycloak. Met dezelfde naam is de wissel alleen een andere
image. De MinBZK-jar is eruit, dus er botsen geen twee thema's met dezelfde naam.

## Hoe het gebouwd wordt

Een node-fase in `images/keycloak-zad/Dockerfile` draait `npm ci` tegen
`images/keycloak-zad/theme/package.json` en kopieert uit `@nldd/design-system` de
stijlbladen, de fonts, de vertalingen, de bundel en het favicon naar de `common`-resources
van het thema. Op `BUILDPLATFORM`, want het resultaat is architectuuronafhankelijk.

Het thema gaat als **map** naar `/opt/keycloak/themes/nl-design-system/` en niet als jar
naar `providers/`. Een map vraagt geen `META-INF/keycloak-themes.json` en geen jar-stap, en
Keycloak leest hem bij de start even goed; `kc.sh build --optimized` raakt hem niet.

**De pagina haalt niets buiten de image op.** De bundel, de stijlbladen, de fonts en het
favicon komen van de eigen host. De inlogpagina is de plek waar een derde partij het minst
mag meekijken, en op een afgesloten cluster zou een CDN een pagina zonder opmaak geven.
`test_keycloak_eigen_image.py` bewaakt dat in de bron en
`test_keycloak_image_thema.py` op de gerenderde pagina en in de hele stijlketen.

**De pagina heeft JavaScript nodig.** NLDD is een webcomponentenlaag: zijn invoervelden
leveren hun waarde via `ElementInternals` (form-associated custom elements), dus zonder de
bundel post het formulier niets. Dat is de prijs van hetzelfde design system als de portal;
het ROOS-thema was CSS-only. De bundel komt uit de image en van dezelfde host als de pagina.

## Een berichtsleutel toevoegen

De sleutels staan in `images/keycloak-zad/theme/nl-design-system/login/messages/`. Zet een
nieuwe in **beide** bestanden (`messages_nl.properties` en `messages_en.properties`). Alles
wat daar niet staat komt uit de bundels van Keycloaks eigen basisthema, inclusief de
Nederlandse; je zet er dus alleen in wat we zelf schrijven of anders willen verwoorden.

Vroeger ging dit in de MinBZK-repo (zie `client-access-restriction.md`); dat is niet meer zo.

## NLDD ophogen

```bash
cd images/keycloak-zad/theme
npm install @nldd/design-system@<versie>   # werkt package.json en package-lock.json bij
```

Renovate pakt dit `package.json` met zijn npm-manager vanzelf op. Draai daarna
`task test-keycloak-image` en bekijk de beelden; een hoofdversiesprong van NLDD kan
attributen hernoemen.

## Toetsen en beelden

```bash
task test-keycloak-image     # bouwt de image en meet elke pagina in een echte Keycloak
```

Dat draait onder meer `tests/integration/test_keycloak_image_thema.py`: dat doorloopt de
flows zoals een browser, rendert elke bereikbare pagina, logt echt in met gebruikersnaam en
wachtwoord en met een OTP, en controleert dat er geen verzoek buiten de eigen host uitgaat.
`tests/test_keycloak_eigen_image.py` leest de Dockerfile en de bron: geen MinBZK-jar, geen
ROOS-klasse, een gepinde NLDD met lockfile.

De beelden maak je met Playwright, tegen diezelfde container:

```bash
cd operations-manager/python
uv run python scripts/keycloak_thema_shots.py --wachtwoord '<uit task test-keycloak-image>'
```

Dat levert elke pagina in nl en en, op bureaublad- en telefoonbreedte, met een contactvel in
`tests/e2e/screenshots/keycloak-thema/index.html`.

## Uitrollen

Publiceren, pinnen en promoveren gaan zoals bij elke wijziging aan deze image; zie
`keycloak-eigen-image.md`. De laatste stap voor een merge is een inlog via SSO-Rijk op de
sandbox: de testcontainer heeft geen externe IdP, dus die weg is daar niet te meten.
