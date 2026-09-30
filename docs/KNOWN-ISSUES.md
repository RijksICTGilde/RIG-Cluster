# Known Issues

- Removing a key from a Kubernetes Secret does not trigger an ArgoCD sync. Related: https://github.com/argoproj/argo-cd/issues/24882

## Het standaardschema kapt stil af (niet gerepareerd)

Genoteerd bij RC-59, bewust niet daar gerepareerd.

`generate_database_schema` maakt de naam van het standaardschema als
`{project}_{deployment}` en kapt die met een kale `name[:63]` af, zonder hash of andere
onderscheidende staart. Twee deployments in hetzelfde project met lange namen die pas na
teken 63 uit elkaar lopen, krijgen dus **hetzelfde standaardschema** en zitten ongemerkt in
elkaars data.

RC-17 heeft dit voor *extra* schema's juist voorkomen: `generate_extra_database_schema`
gooit een `ValueError` in plaats van af te kappen, en sinds RC-59 wordt die controle bij
elke opslag gedraaid, dus ook als er een deployment bijkomt. De standaardweg is daar nooit
in meegenomen.

Waarom het hier blijft staan: een naamgevingsregel wijzigen raakt **bestaande databases**.
Elke oplossing (hard falen, een hash toevoegen, de deploymentnaam begrenzen) is een migratie
met een eigen vraag over wat er met de al aangemaakte schema's gebeurt. Dat hoort een eigen
taak te zijn, niet een bijvangst van een API-uitbreiding.

## Versiebeheer uitzetten doet niets (niet gerepareerd)

Genoteerd bij RC-99, gemeten op de sandbox (build `65c28ed1`), bewust niet daar
gerepareerd.

Het vinkje "Versiebeheer op de bucket" aanzetten werkt: het projectbestand krijgt
`enable-versioning: true` en de bucket krijgt versiebeheer. Het weer UITvinken haalt de
sleutel uit het bestand (`remove_when_none` op `MINIO_ENABLE_VERSIONING_EDITABLE`), en
`MinioManager._apply_bucket_versioning` slaat een bucket zonder sleutel over. De bucket
houdt dus versiebeheer, terwijl het scherm en het bestand zeggen dat er niets aan staat.

Gemeten volgorde (elke stap terug uit het projectbestand in Forgejo gelezen):

| actie | in het bestand | op de bucket |
|---|---|---|
| aanvinken | `enable-versioning: true` | aan |
| uitvinken | geen sleutel | blijft aan |

Waarom het hier blijft staan: de twee voor de hand liggende oplossingen zijn allebei een
besluit dat groter is dan deze taak.

* Het vinkje `false` laten wegschrijven (`remove_when_none` eraf) repareert het uitzetten,
  maar geeft elk project dat langs de minio-configstap loopt een sleutel die niemand
  koos - precies wat RC-99 juist wegnam.
* Afwezig laten betekenen "volg de platformstandaard, dus uit" en dat ook echt afdwingen,
  raakt **bestaande buckets**: buckets waar versiebeheer ooit met de hand aan is gezet,
  worden dan bij de eerstvolgende verwerking gesuspendeerd. Dat is een datavraag met een
  eigen afweging.

Tot dat besluit valt: versiebeheer uitzetten kan met `mc version suspend` op de bucket
zelf, en het bestand is dan al in orde.

## MinIO distribueert zijn eigen images niet meer (niet gerepareerd)

Genoteerd bij RC-227, gemeten op 24 en 25 september 2026.

MinIO heeft zijn publicatiekanalen dichtgezet. Gemeten anoniem, met een publieke buur als
controlebeeld zodat het antwoord over het account gaat en niet over de host:

| adres | uitkomst | controlebeeld |
|---|---|---|
| `docker.io/minio/minio` | 401 op elke tag | `docker.io/library/alpine` geeft 200 |
| `quay.io/minio/minio` | 401 op elke tag | `quay.io/prometheus/busybox` geeft 200 |
| `quay.io/minio/mc` | 401 op elke tag | idem |
| `dl.min.io` | 410 Gone | nvt |

Twee dingen zijn daarmee gerepareerd, en twee blijven staan.

**Gerepareerd.** Server en client staan nu op hetzelfde patroon: een eigen exemplaar in
`ghcr.io/minbzk/base-images`, zodat geen van beide nog van een adres van MinIO afhangt.

* De server op productie komt uit een eigen build uit de broncode van tag
  `RELEASE.2025-07-23T15-54-02Z`; de reden en de bouwwijze staan in
  `infrastructure/bootstrap/infrastructure/minio/controller/overlays/odcn/kustomization.yaml`.
* De `mc`-client komt uit onze eigen spiegel `ghcr.io/minbzk/base-images/mc`, gevuld uit de
  GitHub-release van dezelfde tag. Waarom die spiegel er is en waarom de checksum bij het
  vullen literaal staat, staat in `images/mc/Dockerfile`; hoe je hem bijwerkt bij een nieuwe
  versie, in `images/mc/README.md`.

**Blijft staan 1: de huidige productiepin is niet meer te herbouwen.** Productie draait
`2026.09.02.2241-d81cdab4`, en de Dockerfile op die commit haalt `mc` van `dl.min.io`. Die
build eindigt vandaag op een 410. Een terugrol naar deze pin kan dus alleen met het al
gepubliceerde image, niet met een build uit de bron. Dat lost zichzelf op zodra productie
een pin krijgt van na de reparatie hierboven; tot die tijd is het al gepubliceerde image het
enige exemplaar dat er is.

**Blijft staan 2: local en sandboxed-local wijzen nog naar het dode adres.** Alleen de
odcn-overlays vervangen `quay.io/minio/minio` door de eigen build. De base-deployments
eronder, en `bootstrap/rig-system/kustomize/backup-destination/base/deployment.yaml` die
helemaal geen odcn-overlay heeft, houden de kale quay-referentie. Wat er draait, draait op
wat de node al in zijn cache had: op de sandbox staat de minio-pod nog op
`docker.io/minio/minio` uit augustus, terwijl de base inmiddels quay zegt. Een node zonder
die cache krijgt de image niet meer.

Waarom het hier blijft staan: de eigen build in `ghcr-rig` is een proxypad naar het
rijksregister, en of een Kind-cluster op een werkplek daaruit mag pullen is een andere vraag
dan wat deze taak meet. De keuze (het rijksregister ook voor lokaal gebruiken, een eigen
build ergens publiek neerzetten, of de image in de sandbox-setup meebakken) hoort een eigen
taak te zijn.

## Sandbox Setup

### Forgejo pod restart causing sandbox:sync failure (fixed)

The `sandbox:sync` task could fail with `unable to forward port because pod is not running. Current status=Pending` when the Forgejo pod restarted between `sandbox:init-forgejo` and `sandbox:sync`. The init step (creating admin user and 4 repositories) can cause memory pressure or liveness probe failure, causing the pod to restart. The port-forward fallback in `sandbox:sync` had no wait logic, so it would immediately fail if the pod wasn't Running.

**Fix:** Added `kubectl wait --for=condition=Ready` before the port-forward attempt, giving the pod up to 120s to become Ready again.

### GHCR egress limit causing ArgoCD repo server timeout

During setup, the ArgoCD repo server rollout can time out if GitHub Container Registry returns a `503 Egress is over the account limit` error when pulling `ghcr.io/minbzk/base-images/rig-cmp-argo-kustomize-sops:latest`. This happens because the image uses the `latest` tag with `imagePullPolicy: Always`, so Kubernetes attempts a fresh pull even when the image is already cached on the node.

**Workaround:** Re-run `task sandbox:setup`. The image is typically cached on the Kind node from a previous attempt, and the rollout will succeed once the old pod finishes terminating. The setup is idempotent.

### Secrets overview files blocking re-runs

When `task sandbox:setup` fails partway through, the generated `secrets-overview-*.yaml` files remain in the project root. On the next run, the setup refuses to continue to avoid overwriting passwords you may not have saved yet.

**Workaround:** Delete the leftover overview files and re-run:

```bash
rm -f secrets-overview-*.yaml
task sandbox:setup
```

### ArgoCD operator CRD deletion timeout

The `prepare-argocd-operator` task uses `kubectl replace --force` to apply the ArgoCD operator, which deletes and recreates the CRD. When ArgoCD resources already exist in the cluster (e.g. from a previous partial setup), the CRD deletion blocks on finalizers - the ArgoCD CR has an `argoproj.io/finalizer` that can't be processed because the operator itself is being replaced. This creates a deadlock that hangs indefinitely or fails with `context deadline exceeded`.

**Workaround:** In a separate terminal, remove the finalizer to unblock the deletion:

```bash
kubectl patch argocd argocd -n rig-system --type=json -p='[{"op": "remove", "path": "/metadata/finalizers"}]'
```

The setup will then continue automatically.

## Een vooraf aangemaakte gebruiker komt niet door de authorization wall (niet gerepareerd)

Genoteerd 30-09-2026, gemeten op odcn-production in realm `no-ks4-odcn-production` met
Keycloak 25.0.6.

Twee features spreken elkaar tegen. `features/keycloak-auto-link.md` beschrijft de bedoelde
weg: maak de gebruiker vooraf aan in het projectrealm op e-mailadres, geef hem de rol
`allowed-user` voor de authorization wall, en "no invite link, email verification, or password
step is required". De wall eist tegelijk het omgekeerde: de sidecar draait met
`--insecure-oidc-allow-unverified-email=false` en weigert een id_token waarvan
`email_verified` niet true is. Elk project dat beide gebruikt, en dat is precies het
gedocumenteerde gebruik, blokkeert dus zijn vooraf aangemaakte gebruikers. Zij krijgen op
`/oauth2/callback` een HTTP 500 en in de sidecar staat:

```
[oauthproxy.go:895] Error redeeming code during OAuth2 callback:
    email in id_token (<adres>) isn't verified
```

De keten, uit Keycloak 25.0.6 gelezen. `create_user` in de connector zet `emailVerified` op
false en hangt er `requiredActions: ["VERIFY_EMAIL"]` aan zolang de aanroeper geen
`skip_email_verification` meegeeft (`opi/connectors/keycloak.py:3650`). Logt die gebruiker
daarna via SSO in, dan vindt `idp-create-user-if-unique` een duplicaat op het adres en valt de
flow naar de auto-link-subflow. `IdpAutoLinkAuthenticator` doet alleen
`context.setUser(existingUser); context.success();` en zet geen `BROKER_REGISTERED_NEW_USER`.
In `IdentityBrokerService` zit de tak die `emailVerified` op true zet (regel 738, op grond van
`trustEmail`) **binnen** `if (BROKER_REGISTERED_NEW_USER)`. Een gekoppeld account valt in de
`else` en krijgt alleen `updateFederatedIdentity`, en die roept `setBasicUserAttributes`
uitsluitend bij `syncMode: FORCE` aan terwijl onze IdP op `IMPORT` staat. De vlag blijft dus
false, voor altijd.

Dit is dezelfde fout die `features/keycloak-auto-link.md` onder "Attribute sync on linked
accounts" al eens heeft geraakt en opgelost: "a pre-created account is never created by the
IdP, so with the mappers' original `syncMode: IMPORT` its attributes were never populated".
Daar zijn de mappers op `FORCE` gezet. `emailVerified` is daarbij overgeslagen, want dat is
geen mapper maar een tak in `IdentityBrokerService`.

Wie wel binnenkomt: iedereen die inlogt **zonder** dat er een account voor hem klaarstond.
Die wordt door `idp-create-user-if-unique` echt aangemaakt, dus `BROKER_REGISTERED_NEW_USER`
staat, en `trustEmail` zet de vlag. Vandaar dat het probleem per gebruiker lijkt te wisselen.

Repareren, per getroffen account: zet in Keycloak `Email verified` op On in het projectrealm en
haal de `VERIFY_EMAIL` required action weg. Verwijderen en opnieuw laten inloggen werkt ook,
maar dan verliest de gebruiker zijn rollen, dus dat is hier de slechtere weg.

De structurele oplossing is een vooraf aangemaakt account in een SSO-only realm aanmaken met
`skip_email_verification=True`, de parameter die `create_user` al heeft en die
`opi/manager/keycloak_manager.py:1998` en `:2114` elders al gebruiken. Het adres dient in zo'n
realm alleen om de federated identity te matchen, en die IdP is met `trustEmail: True` al
vertrouwd. Dat is niet gebouwd: het verandert een beveiligingsrelevante default voor alle
projectrealms en hoort een eigen besluit te zijn.

Wat wel gerepareerd is: de wall levert nu een eigen `error.html` mee, zodat de gebruiker geen
kale Engelse 500 ziet maar een Nederlandse pagina met een `RequestID` om aan de beheerder te
geven. Dat maakt de storing leesbaar, niet weg.

Wat niet uitgezocht is: waarom `roos.groot` en `anne.schuth` in dit realm wel binnenkwamen is
afgeleid uit bovenstaande keten, niet gemeten. Keycloak-auditevents staan in productie uit, dus
per gebruiker is niet terug te zien of hij vooraf was aangemaakt of bij de eerste login is
ontstaan. Zet die events aan voordat je dit nog eens onderzoekt.
