# De Keycloak-image opnieuw op de nieuwe main

**Status**: plan, het werk bestaat al maar staat op een tak die niet te mergen is.
**Datum**: 2026-10-12
**Aanleiding**: de main heeft een nieuwe geschiedenis gekregen. De tak met de afgeronde Keycloak-image heeft daarmee geen gemeenschappelijk punt meer, dus hij is niet te mergen en het werk staat stil.

## Wat er aan de hand is

`git merge-base` tussen de tak `een-keycloak-image-met-onze-eigen-providers-erin` en de huidige `forgejo/main` geeft niets terug, en `git merge-tree` weigert met "refusing to merge unrelated histories". Een rebase helpt niet, een merge ook niet: er is geen gedeelde voorouder. De inhoud is wel gewoon leesbaar vanaf die tak, dus dit is overzetten en niet opnieuw bouwen.

Op de huidige main staat van dit werk niets: geen `images/keycloak-zad/`, geen `scripts/build-keycloak-image.sh`, en `base/deployment.yaml:67` pint nog `quay.io/keycloak/keycloak:25.0.6` met de initContainer die bij elke podstart twee jars van github.com haalt.

Het contract ligt al vast en verandert niet: `plans/een-keycloak-provider-komt-uit-een-release.md` staat op main en blijft leidend. Dit document beschrijft alleen wat er bij het overzetten geldt.

## Wat er klaar is en dus mee moet

Op die tak, gemeten tegen de oude main-top `d5450ad1e`: 34 bestanden, 2175 toevoegingen, 621 verwijderingen. Daarin zitten `images/keycloak-zad/Dockerfile` (meerfasig, een Maven-fase op `BUILDPLATFORM`, het thema met een `sha256sum -c`, en `kc.sh build` met de vlagverdeling), `scripts/build-keycloak-image.sh`, `scripts/promoveer-keycloak-image.sh`, `scripts/bron-tag.sh`, de taken in `Taskfile.yaml`, de overlay-wijzigingen (de sandbox met zijn eigen `images:`-pin, odcn met het rcr-pad en een pull-secret patch), het weghalen van de initContainer, de provider-ConfigMap en de ingecheckte jar, `features/keycloak-eigen-image.md`, en ongeveer 700 regels tests.

Er zitten drie rondes feedback in die tak die niet verloren mogen gaan: de vlagverdeling waardoor een verse pod niet meer augmenteert, de vorm van de build-en-deploy van de operations-manager met CalVer, en de namen `publish-keycloak-image` en `pin-keycloak-image`.

Het migratieplan naar 26.7.4 staat op diezelfde tak (`plans/de-keycloak-gaat-naar-26-en-de-providers-moeten-mee.md`, commit `868b98698`). Dat hoort bij issue #189 en moet mee.

## Wat we bouwen

Overzetten, niet herontwerpen. Neem de inhoud van die tak over op een verse tak van de huidige main en houd daarbij de contracten die al vastliggen:

1. `publish-keycloak-image` berekent de CalVer-tag in de vorm van de taak `calver`, bouwt en pusht via de gedeelde taak `docker-build-and-push`, en pint daarna de sandbox-overlay.
2. `pin-keycloak-image` zet een gegeven tag in een overlay, met de commit- en deployregels erbij en de terugvalweg, zoals `pin-operations-manager-image` dat doet. Promoveren naar productie is dezelfde taak met base als doel.
3. De guards blijven: weigeren te pinnen als de image niet zonder inlog te halen is, en base mag een tag dragen die niet bestaat zolang er niets gepromoveerd is, zodat vergeten promoveren luid faalt.
4. Geen bouwtaak zonder push. Wil je iets om te debuggen, dan is dat een vlag op `publish-keycloak-image` die niet pusht en niet pint.
5. De jars blijven eigen artefacten met hun eigen bouw- en testtaken per module.

## Waar het handwerk zit

Main is in dit gebied doorgelopen. Er staat nu een taak `publish-keycloak-custom-mapper` (`Taskfile.yaml:1859`) die op de oude basis niet bestond, terwijl onze wijziging 223 regels aan datzelfde bestand toevoegt. Verzoen die twee tot één patroon in plaats van twee publiceerwegen naast elkaar, en zeg in de PR wat je hebt samengevoegd en waarom.

Kijk voor `scripts/bron-tag.sh` eerst wie hem leest voordat je iets weghaalt: hij vult ook de globale Taskfile-variabele `SANDBOX_OM_TAG`, en precies dat script trok in een eerdere ronde elke taak in de repo omver.

## Wat we niet doen

De versiesprong naar 26.7.4. Die staat in issue #189 en in het migratieplan, en begint pas als dit werk op productie draait.

Promoveren naar productie binnen deze taak. De volgorde blijft: bouwen, op de sandbox toetsen, promoveren, dan mergen. De eerste drie stappen zijn handwerk van een mens met toegang tot GHCR en het sandboxcluster.

## Klaar als

- De tak komt van de huidige main en is conflictvrij te mergen, getoetst met `git merge-tree`.
- `images/keycloak-zad/`, de twee scripts, de taken en de overlay-wijzigingen staan erop, en de initContainer, de provider-ConfigMap en de ingecheckte jar zijn weg uit de keycloak-manifests.
- Er is één publiceerpatroon voor Keycloak, niet twee: `publish-keycloak-custom-mapper` en het nieuwe werk zijn verzoend.
- Het migratieplan uit `868b98698` staat in `plans/`.
- `uv run ruff check .`, `uv run ruff format .` en `uv run pyright` schoon, en de tests van allebei de Java-modules groen.
- De bouwvlaggen en de startvlaggen spreken elkaar niet tegen, zodat een verse pod niet augmenteert. Dit is op de sandbox nog niet gemeten; noem in de PR dat die meting de laatste stap voor de merge is.
