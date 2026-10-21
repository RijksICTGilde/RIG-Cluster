# De AGE-sleutel in het cluster: `sops-age-key`

Het contract achter één secretnaam die in veel namespaces voorkomt, met per namespace een andere inhoud. Wie dat voor één secret aanziet, overschrijft de sleutel van een project of laat er een op de oude sleutel achter.

## Eén naam, twee soorten inhoud

`sops-plugin.sh:23` leest de sleutel zo:

```bash
SOPS_KEY_B64=$(kubectl get secret ${SOPS_KEY_SECRET} -n ${ARGOCD_APP_NAMESPACE} -o jsonpath='{.data.key}')
```

`SOPS_KEY_SECRET` is `sops-age-key` tenzij de Application hem via `plugin.env` overschrijft, en `ARGOCD_APP_NAMESPACE` is de namespace waar die Application naartoe deployt. De plugin haalt de sleutel dus niet uit één centrale plek maar uit de bestemming van wat hij aan het renderen is. Daaruit volgt alles hieronder.

Er zijn twee soorten houders:

| soort | namespace | inhoud | wie zet hem neer |
|---|---|---|---|
| platform | `rig-prd-operations` (prod), `rig-system` (sandbox) | de platformsleutel | bootstrap |
| platform, tweede plek | `rig-prd-ron` (prod), `rig-ron` (sandbox) | dezelfde platformsleutel | bootstrap, elke clustersoort met zijn eigen taak: `bootstrap-argo-system` en `sandbox:create-sops-age-secret` |
| project | `rig-prd-<project>` / `rig-<project>` | de EIGEN sleutel van dat project | OPI, `store_project_sops_key_in_namespace` |

De tweede rij is de val, ook nu de bootstrap hem zet. Dit secret staat in geen enkel manifest: het kan niet uit git komen, het komt dus niet mee met een sync, en het valt alleen op als je ernaar kijkt. De mailrelay rendert `infrastructure/bootstrap/infrastructure/mail/controller/overlays/odcn`, en daar staat een `decrypt-sops.yaml`, dus zonder dit secret rendert die applicatie niet. `bootstrap-argo-system` laat een bestaand secret staan, dus een productiecluster dat de verkeerde sleutel draagt komt daar niet goed door een nieuwe bootstrap: dat is een rotatie, zie onder. `sandbox:create-sops-age-secret` heeft die guard niet en schrijft de sleutel uit `security/sandbox-key.txt` onvoorwaardelijk over de live waarde, in `rig-system` en in `rig-ron`.

## Wat dit betekent als je de platformsleutel vervangt

Nooit alle `sops-age-key`-secrets overschrijven. Dat vervangt de eigen sleutel van elk project door de platformsleutel, en dan is elk projectgeheim onleesbaar.

De regel is: vervang alleen waar de OUDE publieke sleutel in staat. `scripts/set-sops-key-secret.py` doet precies dat, en het is de reden dat het script elke namespace langsloopt in plaats van een vaste lijst af te werken. Zijn dry-run toont twee groepen, en die lijst is de controle vóór de onomkeerbare stap:

```
N carry the OLD platform key and WILL be replaced:   <- hier horen alleen platformhouders
M carry a DIFFERENT key and are left alone:          <- hier hoort elke projectnamespace, met zijn eigen publieke sleutel
```

Gemeten op het sandboxcluster op 24 september 2026: 16 houders, waarvan 2 de platformsleutel (`rig-system`, `rig-ron`) en 14 een eigen projectsleutel. Op productie is het aantal projectnamespaces anders, maar de verhouding is dezelfde en de twee platformhouders heten daar `rig-prd-operations` en `rig-prd-ron`.

## Wie moet herstarten

De plugin niet: die leest het secret bij elke render opnieuw, dus de eerste render na de wissel draait al op de nieuwe sleutel. Forceer die render terwijl je kijkt, in plaats van hem bij een willekeurige sync tegen te komen.

OPI wel. Die krijgt de sleutel als gemount bestand binnen (zie onder) en leest hem eenmalig bij het opstarten, dus een draaiende pod blijft op de oude sleutel staan ook nadat kubelet het bestand heeft bijgewerkt. Een `rollout restart` volstaat, en er is geen nieuwe image voor nodig: er staat geen `secretGenerator` in `bootstrap/`, dus het manifest verandert niet mee.

## Hoe OPI de sleutel binnenkrijgt

Als bestand, niet als omgevingsvariabele. `base/deployment.yaml` mount het secret
`sops-age-key` (item `key`) op `/var/run/secrets/zad/sops-age/key` en noemt dat pad in
`SOPS_AGE_KEY_FILE`; `load_sops_age_key_content` in `opi/core/config.py` leest het bij het
opstarten. Een gezet maar leeg of onleesbaar pad is een harde opstartfout: stil terugvallen
op de omgeving zou een verkeerd gemounte sleutel verbergen tot de eerste ontsleuteling.
`SOPS_AGE_KEY_CONTENT` werkt nog als terugval voor lokale ontwikkeling, met een
waarschuwing in de log, en wordt na het lezen uit `os.environ` verwijderd.

Mode `0440` met `fsGroup: 1001`, niet `0400`: kubelet geeft bestanden in een secret-volume
aan root, en de groep is het enige wat met `fsGroup` naar de uid van OPI te brengen is.

**Geef een kindproces zijn omgeving met `child_env()`, nooit met `os.environ`.**
`child_env()` (`opi/utils/subprocess_env.py`) weert alles met prefix `SOPS_AGE_` plus elke
naam uit de env-bronnen van het deployment: elke `secretKeyRef` en elk secret achter een
`envFrom`, dat laatste zowel uit `base/deployment.yaml` als uit de patches van de overlays
(productie zet er met een inline JSON-patch in `kustomization.yaml` een derde bij,
`operations-manager-access`). Wat een proces echt nodig heeft, geeft de aanroeper expliciet
mee, zoals `sops` zijn `SOPS_AGE_KEY` per aanroep krijgt. Voeg je een geheim aan een van die
bronnen toe, dan faalt `tests/test_platformsleutel_als_bestand.py` tot de naam ook op de
lijst staat; een `envFrom` die de toets niet kent laat hem ook falen. De toets leest elke
Deployment-patch van elke overlay, niet alleen `patches/deployment.yaml`, en ook de inline
patches: een patch met een andere naam of zonder bestand wordt niet genegeerd.

Voor een sops-aanroep is dat geen vormkwestie. `SOPS_AGE_KEY_FILE` is een variabele die
sops ZELF leest, dus een sops die de omgeving van OPI erft ontsleutelt alles op de
platformsleutel zonder dat de aanroeper iets meegaf.

Een aanroep die GEEN `env=` meegeeft erft de volle omgeving, buiten `child_env()` om, dus
is dat net zo fout als `os.environ.copy()`. Geen enkele aanroep in `opi/` doet dat nog, en
`tests/test_platformsleutel_als_bestand.py` leest de bron met een AST-scan: hij faalt op
`subprocess.run`, `subprocess.Popen`, `asyncio.create_subprocess_exec` en
`create_subprocess_shell` zonder `env=`, en op de twee spellingen van `os.environ`.
Schrijf je een nieuwe aanroep, geef dan `child_env()` mee, met daarbovenop wat het
programma echt nodig heeft.

`kopia` is de uitzondering op `child_env()`: die connector bouwt zijn omgeving uit een
allow-list (`_KOPIA_ENV_PASSTHROUGH`), en laat daarmee ook weg wat vandaag nog geen geheim
is. Strikter mag, ruimer niet.

`decrypt_sops_file` in `opi/utils/sops.py` is daarbij de enige die een platformgeheim
expliciet terugkrijgt: hij ontsleutelt platformbestanden, en zonder `SOPS_AGE_KEY` kan dat
niet. Die twee functies hebben vandaag geen aanroeper.

Platte SOPS-inhoud gaat tijdelijk naar `SECRET_TEMP_DIR`, een `emptyDir` met
`medium: Memory`, en niet naar `TEMP_DIR` op de schijf van de node.

## Verder lezen

- `features/sops-sleutel-roteren.md` is het stappenplan voor de vervanging zelf
- `features/send-email.md` heeft de aanzetstappen voor de ron-namespace
- `bootstrap/rig-system/kustomize/sops-plugin.sh` is de plugin die de sleutel leest
