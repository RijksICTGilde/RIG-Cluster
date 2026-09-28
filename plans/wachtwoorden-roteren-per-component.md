# Wachtwoorden roteren per component

Status: **voorstel, niets gebouwd.** Gemeten op odcn-production op 26-09-2026, met de toen geldende secrets.

Aanleiding: het Keycloak-adminwachtwoord roteren kostte die avond vier losse handelingen in twee pijplijnen, waarvan één met de hand in Keycloak. Voor MinIO, PostgreSQL en Redis moet dat met een script kunnen. Dit document zoekt eerst uit waarom het nu niet kan, en stelt dan voor in welke orde dat te repareren.

## 1. Wat er nu staat

Elf SOPS-versleutelde infra-secrets in `infrastructure/bootstrap/infrastructure/secrets/config/overlays/odcn/`, samen 51 velden:

| secret | velden |
|---|---|
| `keycloak-admin-secret` | `KEYCLOAK_ADMIN`, `KEYCLOAK_ADMIN_PASSWORD` |
| `keycloak-mail-secret` | `smtp-password` |
| `mail-db-credentials-secret` | `username`, `password` |
| `mail-relay-secret` | `MAIL_FROM_LOCAL`, `MAIL_DOMAIN`, `MAIL_RELAY_ADMIN_USERNAME`, `MAIL_RELAY_ADMIN_PASSWORD` |
| `minio-admin-secret` | `MINIO_ROOT_USER`, `MINIO_ROOT_PASSWORD` |
| `pgadmin-secret` | `PGADMIN_DEFAULT_EMAIL`, `PGLADMIN_DEFAULT_PASSWORD` |
| `postgres-admin-secret` | `username`, `password` |
| `prometheus-metrics-auth-secret` | `token` |
| `redis-admin-secret` | `REDIS_PASSWORD` |
| `transip-secret` | `TRANSIP_ACCOUNT_NAME`, `TRANSIP_PRIVATE_KEY` |
| `vault-init-secret` | `root-token`, `unseal-key-1..3` |

Daarnaast `operations-manager-env-secrets` (21 velden) in `bootstrap/rig-system/kustomize/operations-manager/overlays/odcn-production/`, gegenereerd uit `operations-manager/python/.env.odcn-production.secrets`.

Let op de typefout in `pgadmin-secret`: het veld heet `PGLADMIN_DEFAULT_PASSWORD`, met een L. Niet aanraken zonder te kijken wat de pgadmin-deployment leest; een rotatiescript moet de bestaande naam gebruiken en hem niet stilzwijgend verbeteren.

## 2. Het echte probleem: vier wachtwoorden staan dubbel

Gemeten door elke waarde in het env-secret te hashen en tegen elke waarde in de infra-secrets te leggen:

| in het env-secret van OPI | is dezelfde waarde als |
|---|---|
| `DATABASE_ADMIN_PASSWORD` | `postgres-admin-secret/password` |
| `MINIO_ADMIN_SECRET_KEY` | `minio-admin-secret/MINIO_ROOT_PASSWORD` |
| `REDIS_PASSWORD` | `redis-admin-secret/REDIS_PASSWORD` |
| `KEYCLOAK_ADMIN_PASSWORD` | `keycloak-admin-secret/KEYCLOAK_ADMIN_PASSWORD` |

Dit is geen dubbele opslag van hetzelfde ding, het zijn twee kanten van één afspraak: het infra-secret is wat de component als zijn wachtwoord instelt, het env-secret is wat OPI gebruikt om ermee te praten. Lopen ze uiteen, dan kan OPI niet bij die component, en dat merk je pas als iets faalt.

Dat er nu niets uiteen loopt is geen structurele garantie maar het resultaat van handwerk. De rotatie van 26-09 laat zien hoe smal die marge is: het Keycloak-wachtwoord stond op drie plekken met twee verschillende waarden, en de derde plek (`keycloak-admin-secret.yaml.yaml.sops.yaml`) was een bestand dat door niets gerenderd werd en dat niemand kende.

Er is nog een vijfde match, maar die is onschuldig: `MINIO_ADMIN_ACCESS_KEY` is gelijk aan `MINIO_ROOT_USER`, aan `KEYCLOAK_ADMIN` en aan `MAIL_RELAY_ADMIN_USERNAME`, omdat die alle vier letterlijk `admin` zijn. Gebruikersnamen, geen wachtwoorden.

## 3. Vier componenten, vier mechanismen

Dit is de kern van waarom één generiek rotatiescript niet bestaat: het secret bijwerken is per component iets anders waard.

| component | hoe het zijn wachtwoord krijgt | wat rotatie vraagt |
|---|---|---|
| **PostgreSQL** (CNPG) | `superuserSecret: postgres-admin-credentials` met `enableSuperuserAccess: true` (`postgresql/database/base/cluster.yaml:17,31`) | alleen het secret bijwerken. De operator synchroniseert het naar de database. Geen restart, geen SQL. |
| **MinIO** | `envFrom` op `minio-admin-credentials` (`minio/controller/base/deployment.yaml:51`) | secret bijwerken **en** de pod herstarten. Het root-wachtwoord wordt bij elke start uit de env gelezen. |
| **Redis** | het startscript schrijft `user default on >$REDIS_PASSWORD ...` naar `/data/users.acl` (`redis/controller/base/deployment.yaml:34`) | secret bijwerken **en** de pod herstarten. Het ACL-bestand wordt bij boot opnieuw geschreven. |
| **Keycloak** | `KEYCLOAK_ADMIN_PASSWORD` uit `keycloak-admin-credentials`, maar **alleen bij de eerste start van een verse cluster**; daarna is de Keycloak-database leidend | het secret bijwerken doet niets. Het wachtwoord moet via de Admin API of de UI om. Dit is waarom het op 26-09 met de hand ging. |

De consequentie: PostgreSQL is declaratief, MinIO en Redis zijn declaratief-plus-restart, en Keycloak is het enige geval dat een echte API-aanroep nodig heeft. Een script dat alleen het eerste kan, is voor drie van de vier onvolledig.

Waar kan zo'n script op bouwen? `opi/connectors/postgres.py:623` heeft al een `ALTER USER ... WITH PASSWORD`, en `opi/connectors/minio_mc.py` praat al met MinIO. Voor Keycloak zit het adminwerk in `opi/manager/keycloak_manager.py`. Die zijn gebouwd voor projectgebruikers, niet voor de adminaccounts van het platform, maar het zijn de plekken waar een handler zijn werk hoort te doen in plaats van een tweede client ernaast te zetten.

## 4. Voorstel: eerst de bron, dan het script

Mijn advies is om de volgorde niet om te draaien, want de vorm van het script hangt af van de beslissing over de env-file.

### Stap A: één bron per wachtwoord, en dat lukt maar voor de helft

Het idee is om de vier dubbele waarden uit `.env.odcn-production.secrets` te halen en OPI ze rechtstreeks uit het infra-secret te laten lezen. De naamgeving is daarvoor geen bezwaar: twee van de vier heten al hetzelfde aan beide kanten (`REDIS_PASSWORD`, `KEYCLOAK_ADMIN_PASSWORD`), en voor de andere twee mapt een `secretKeyRef` de naam:

```yaml
- name: DATABASE_ADMIN_PASSWORD
  valueFrom:
    secretKeyRef:
      name: postgres-admin-credentials
      key: password
```

Dat is geen nieuw patroon: dezelfde deployment doet het al zo voor `KEYCLOAK_ADMIN_CLIENT_SECRET` en de OTP-velden (`overlays/odcn-production/patches/deployment.yaml:41-64`). `envFrom` kan geen veldnamen mappen, losse `env`-entries wel.

**Maar een `secretKeyRef` kan niet over namespaces heen, en de namespaces lopen uiteen.** Gemeten op de gerenderde odcn-bouw:

| component | het secret landt in | zijn consumer staat in | bron |
|---|---|---|---|
| PostgreSQL | `rig-system` | `rig-system` (het CNPG-cluster) | `postgresql/database/overlays/odcn/kustomization.yaml:3` |
| MinIO | `rig-system` | `rig-prd-operations` | `minio/controller/overlays/odcn/kustomization.yaml:3` |
| Redis | `rig-prd-operations` | `rig-prd-operations` | |
| Keycloak | `rig-prd-operations` | `rig-prd-operations` | |

OPI staat in `rig-prd-operations`. Dus stap A werkt zonder meer voor **Redis en Keycloak**, en niet voor PostgreSQL: dat secret hoort bij het CNPG-cluster in `rig-system` en is daar op zijn plek. Voor die twee blijft een eigen kopie in de env-file nodig, of er moet een expliciete replicatie van dat ene veld naar `rig-prd-operations` komen. Dat laatste is een nieuwe voorziening en verdient een eigen afweging; het is geen bijproduct van dit voorstel.

**En MinIO is een bevinding op zichzelf.** De deployment staat in `rig-prd-operations` en leest `envFrom: secretRef: minio-admin-credentials`, terwijl dat secret volgens git in `rig-system` landt. Die twee kunnen niet bij elkaar komen. MinIO draait wel in productie, dus er moet in `rig-prd-operations` een `minio-admin-credentials` staan die niet uit deze git-boom komt. Dat is drift, en die moet eerst vastgesteld en opgelost worden, want zolang dat zo staat weet niemand welke waarde MinIO werkelijk gebruikt. Dit is niet met de git-boom te beantwoorden; het vraagt een blik in het cluster.

Wat stap A oplevert waar hij wel kan: het infra-secret wordt de enige bron, de env-file houdt alleen wat écht van OPI is (`SECRET_KEY`, `OIDC_CLIENT_SECRET`, `GRAFANA_TOKEN`, de `BACKUP_S3_*`, de `LOGWATCHER_*`, de vlaggen), en het rotatiescript hoeft één plek te schrijven in plaats van twee synchroon te houden.

Wat het kost: de OPI-pod moet herstarten om nieuwe env-waarden op te pikken, en dat was al zo. En de deployment-patch wordt langer.

### Stap B: het rotatiescript

Vorm: dezelfde als de bestaande rotatietooling, dus een ingang met een streepje en de logica in een module ernaast (`scripts/README.md` legt uit waarom). Voorstel voor de naam, geen bestaand ding: `scripts/rotate-password.py` met `password_rotation.py`.

De kern is een handler per component, want stap 3 laat zien dat het mechanisme echt verschilt. Een handler beschrijft:

- welk secret en welk veld het wachtwoord houdt
- hoe een nieuwe waarde gegenereerd wordt (hergebruik `secret_edit.generate()`, dat de `@secret-gen`-regel van de Taskfile volgt)
- wat er ná het schrijven moet gebeuren: niets (PostgreSQL), een rollout restart (MinIO, Redis), of een API-aanroep (Keycloak)
- hoe te controleren dat het gelukt is: verbinden met het nieuwe wachtwoord

Dat laatste punt is wat het script meer waard maakt dan de handmatige route. Nu is "het is gelukt" een aanname; een handler die na de rotatie een verbinding opzet met de nieuwe waarde maakt er een meting van.

Uitbreidbaar betekent concreet: een nieuw component toevoegen is één handler bijschrijven, en de lijst met handlers is de enige plek die de ingang kent. Geen `if component == ...` verspreid over het script. De mapping-tabel die `features/futures/kube-secrets-to-env.md` al voorstelt (env-var, secret, key) is dezelfde tabel die deze handlers nodig hebben; die hoort één keer te bestaan en niet twee keer.

## 5. In welke orde

1. **De MinIO-namespace uitzoeken.** Dit gaat voor, want zolang onduidelijk is welk secret MinIO werkelijk leest, roteert een script naar een bestand dat niets doet. Vraagt een blik in het cluster: staat er een `minio-admin-credentials` in `rig-prd-operations`, en komt die overeen met wat in git staat?
2. **Stap A voor Redis en Keycloak**, waar de namespaces al kloppen. Dat haalt twee van de vier dubbelingen weg en is een kleine, geïsoleerde wijziging in de deployment-patch.
3. **Het script, te beginnen met Redis en PostgreSQL.** Redis omdat "secret plus restart" het patroon is dat MinIO daarna hergebruikt, PostgreSQL omdat de operator het werk doet en de handler dus bijna leeg is.
4. **Keycloak als laatste.** Enige geval met een echte API-aanroep, en het enige waar een fout je buitensluit uit het systeem dat je nodig hebt om hem te repareren.
5. **PostgreSQL en MinIO in de env-file laten** tot er een besluit is over cross-namespace secrets. Dat is een eigen afweging, geen bijproduct hiervan.

## 6. Wat hier nog niet in staat

De zeven andere secrets (mail-relay, mail-db, keycloak-mail, pgadmin, prometheus-token, transip, vault) zijn niet onderzocht op hun rotatiemechanisme. `vault-init-secret` hoort daar waarschijnlijk niet bij: unseal-keys roteren is een eigen procedure en geen wachtwoordwissel.

Of het cluster-secret overeenkomt met wat in git staat is nooit vastgesteld, voor geen enkel component. Alles in dit document is gemeten aan de git-kant. De MinIO-bevinding hierboven laat zien dat dat niet theoretisch is: daar kan de gerenderde git-boom niet kloppen met een draaiende component, en dat verschil is precies wat niemand meet. Een controle die deze drift zou vinden bestaat niet, en die is misschien meer waard dan het rotatiescript zelf.

Ten slotte: rotatie haalt de oude waarde niet uit de git-historie. Elke versie van elk secret staat versleuteld in de repo, dus wie ooit de AGE-sleutel had, kan alle oude wachtwoorden lezen. Dat maakt de bevinding dat die sleutel publiek is geweest zwaarder dan hij op zichzelf lijkt, en het is een argument om rotatie niet als afsluiting van dat incident te beschouwen.
