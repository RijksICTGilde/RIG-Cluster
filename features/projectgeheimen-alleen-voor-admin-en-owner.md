# Projectgeheimen alleen voor admin en owner

De projectpagina ontsleutelt de geheimen van een project alleen nog voor wie het project
mag wijzigen. Voor een `member` of `developer` staan ze nergens in de response.

## Wat het is

Vier waarden uit een projectbestand zijn geheim en stonden ONTSLEUTELD in de
rendercontext van de detailpagina, voor elk projectlid:

- `config/age-private-key`, de AGE-private-key van het project;
- `config/api-key`, de sleutel die elke muterende project-API-route opent;
- het Keycloak-adminwachtwoord per realm (`services/keycloak/config/realms`);
- de OTP-code van dit moment, afgeleid uit de seed van dat realm.

De rolcontrole stond alleen in de weergave: het blok Configuratie & Secrets zit achter
`{% if user_role in ["admin", "owner"] %}` en het Keycloak-blok levert zichzelf alleen
voor die rollen. Wat de pagina ziet en wat de pagina in handen heeft, waren dus niet
hetzelfde, en een nieuw fragment of een dump van `config` kon erbij.

De selectie van gegevens trekt de grens nu zelf (least privilege, BIO2 5.15 en 8.03):
`opi/web/router.py` ontsleutelt deze vier alleen als het rechtensysteem
`project-key.read` opent voor deze gebruiker, en zet ze anders op `None`. Sinds RC-392
komt dat uit het regelregister (`opi/core/authz/rules.py`) in plaats van uit een rollijst,
dus wat de matrixpagina onder die handeling toont, is wat deze pagina doet. Het
Keycloak-blok en het uitnodigingsblok lezen dezelfde handeling via `ctx.can(...)`; de
bijlagen hangen aan `attachment.manage`.

De AGE-private-key wordt wel voor elke rol ontsleuteld, want de pagina leest er de
env-vars, aliassen en helm-values uit; hij blijft dan server-side en gaat de context niet
in.

## Wat hierdoor verandert

Niets aan wat iemand mag of ziet: de sjablonen toonden deze waarden al alleen aan admin en
owner, en een veld met `None` rendert niet. Wat verdwijnt is de ontsleutelde waarde in de
context van een lid dat hem toch niet te zien kreeg.

Sinds RC-392 is de rol die de poort opent wel veranderd: `project-key.read` staat bij
`admin`, en de blokken hingen aan `ops`. Een lid met alleen `[ops]` kreeg de geheimen dus
wel en een lid met alleen `[admin]` niet, precies omgekeerd aan de regel. Voor bestaande
leden verandert er niets: de schemamigratie naar 2.13 geeft elke oude `admin` of `owner`
de rollen `[admin, ops]`.

Buiten scope blijven de `aliases` en `helm-values` van componenten en deployments: die
ziet elk lid bewust (zie `features/futures/rechten-plan-van-aanpak.md`, 5.6). Voor de
`user-env-vars` gold dat toen ook; sinds RC-388 toont de pagina alleen nog hun namen, en
vragen bekijken en bewerken een verse step-up (zie
[omgevingsvariabelen-achter-step-up.md](omgevingsvariabelen-achter-step-up.md)).

## Dekking

`tests/test_projectpagina_geheimen_per_rol.py` rendert de echte handler met een
projectbestand waarin de vier waarden versleuteld staan, en zoekt door de HTML en
recursief door de rendercontext: door dicts, reeksen en de velden van objecten, zodat ook
een blok dat als `DetailPageSection` binnenkomt meetelt. Alleen `request` blijft buiten de
zoektocht, omdat dat het verzoek van de toets zelf is.

- als `member` en als `developer` komt geen van de waarden voor en rendert de pagina. De
  OTP-code is afgeleid van de seed en dus niet op tekst te vinden: die wordt per realm
  apart op `None` getoetst, net als het wachtwoord;
- als `admin` en als `owner` staan ze er wel, en de OTP-seed nooit.

De rolpoort van `KeycloakService.realm_block` staat daarbij weggepatcht. Zonder dat levert
de dienst voor een `member` helemaal geen blok, en toetst de guard wat de DIENST
tegenhoudt in plaats van wat de handler doorgeeft.
