# Sleutels per cluster, en waar ze vandaan komen

Status: plan, 24 augustus 2026. Aanleiding: er komt een tweede productiecluster bij (werknaam **fundament**), en de sandbox op de dev-server kan nu niet deployen omdat de AGE-sleutels niet in git zitten en er geen manier is om ze te provisioneren. Dat zijn twee kanten van hetzelfde probleem: nergens in de repo staat opgeschreven wélke sleutel bij welk bestand hoort, dus is er ook niets om aan te leveren.

Dit plan hoort bij [wat-een-tweede-cluster-van-ons-vraagt.md](wat-een-tweede-cluster-van-ons-vraagt.md) en werkt daar één regel uit: *"Fundament heeft een eigen sleutel nodig."* Die andere taak inventariseert het hele cluster; deze gaat alleen over sleutels, secrets en hoe ze een machine bereiken.

**Scope.** Dit plan raakt `Taskfile.yaml`, een nieuwe `.sops.yaml`, en een nieuw script onder `scripts/`. Het verandert **geen enkel versleuteld bestand** en **geen enkele overlay**. De leeskant blijft ongemoeid, zodat elke stap los te mergen is zonder een cluster om te gooien. Wat er aan DistributedClaude-kant moet gebeuren staat in de laatste paragraaf en is een andere repo, dus geen onderdeel van deze taak.

## Wat er nu is, gemeten

### Drie sleutels, en wat ze afschermen

| bestand | schermt af | scope |
|---|---|---|
| `security/key.txt` | SOPS-secrets van `local` **en** `odcn-production` | twee clusters, één sleutel |
| `security/sandbox-key.txt` | SOPS-secrets van `sandboxed-local` | één cluster |
| `security/developer-key.txt` | het wildcard-cert `security/tls/sandbox-wildcard/*.age` | gedeeld, niet clustergebonden |

Alle drie gitignored (`.gitignore`: `/security/*` met uitzonderingen voor `readme.md` en `tls/`, en `/security/tls/**/*.pem`).

**Het eerste dat opvalt:** `security/key.txt` dekt zowel `local` als `odcn-production` (`Taskfile.yaml:604`, `668`, `1460`, en de ternary op `857`, `939`, `989`). Wie lokaal kan bouwen, kan daarmee productiesecrets ontsleutelen. Dat is vandaag al de duurste eigenschap van de opzet, los van fundament.

**Het tweede:** `developer-key.txt` is géén clustersleutel maar een teamsleutel. Die hoort dus ook na deze wijziging geen clustersuffix te krijgen.

### De sleutel wordt op twintig plekken per regelnummer gelezen

`sed -n '2p'` voor de publieke sleutel, `sed -n '3p'` voor de private. Dat werkt alleen bij exact het `age-keygen`-uitvoerformaat.

- **Hardgecodeerd `security/key.txt`** (7×): regels 233, 254, 282, 307, 346, 391, **970**
- **Hardgecodeerd `security/sandbox-key.txt`** (7×): regels 2528, 2545, 2636, 2851, 3260, **3304**, **3311**
- **Via `{{.KEY_FILE}}`** (6×): regels 618, 683, 927, 960, 994, 1619

Veertien van de twintig noemen het bestand dus hard en gaan helemaal niet door `KEY_FILE` heen. Een vierde cluster erbij betekent daar veertien keer handwerk, en de drie `skaffold`-taken (`965`, `3304`, `3311`) zitten er middenin: dat is precies waar de sessies vastlopen.

Het lezen per regelnummer is ook wat provisioning breekbaar maakt: levert iets de sleutel aan zonder de twee commentaarregels ervoor, dan is `SOPS_AGE_KEY` leeg en faalt ksops met een obscure fout in plaats van "sleutel niet gevonden".

### De recipient wordt ad hoc meegegeven, er is geen `.sops.yaml`

`find . -name .sops.yaml` levert niets op. In plaats daarvan staat op vier plekken:

```
sops --encrypt --output-type yaml --age "$AGE_PUBLIC_KEY" ...
```

regels **397**, **928**, **1621**, **2637**. Eén `$AGE_PUBLIC_KEY`, afgeleid uit één `KEY_FILE`, dus per definitie één recipient per run. Nergens staat opgeschreven welke bestanden bij welke sleutel horen; dat is een variabele geworden.

Daarnaast versleutelen `encrypt-value-age` (regel 290) en `encrypt-value-age-base64` (regel 265) losse *waarden* met `age -r` direct: geen sops, dus `.sops.yaml` geldt daar niet. Die twee lezen allebei hard `security/key.txt` (regels 282, 307). Volgens `security/readme.md` worden die waarden door de operations-manager in het cluster ontsleuteld met de `sops-age-key`-secret, dus dit is óók een clustersleutel. **Vandaag produceren die taken dus waarden die alleen op ODCN/local leesbaar zijn, ongeacht welk cluster je geselecteerd hebt.** Voor een fundament-cluster is dat stilzwijgend fout.

### Wat een verse checkout mist

Naast de drie sleutels: `.env-taskfile-current` (clusterkeuze, gelezen via `dotenv:` op regel 3: dus bij *parse*-tijd, wat de reden is dat `_generate-secrets-shared` op regel 1464 expliciet controleert of het bestand er al stond), `operations-manager/python/.env.<cluster>.secrets`, de gegenereerde `*.sops.yaml` in de sandbox-overlays, en `.sandbox-mode` / `.sandbox-sso`.

### Wat het gedeelde-repo-vraagstuk betreft

De versleutelde bestanden staan **al per cluster gepartitioneerd**: elke `decrypt-sops.yaml` (ksops-generator) leeft in zijn eigen overlay-map en wijst alleen naar bestanden van dát cluster: `bootstrap/rig-system/kustomize/overlays/{local,odcn-production,sandboxed-local}/`, idem voor `operations-manager/overlays/`, en tien vergelijkbare onder `infrastructure/`. Er is geen bestand dat door twee clusters gelezen moet worden.

**Gevolg: één GitHub-repo delen dwingt geen gedeelde sleutel af.** Fundament kan een eigen sleutel krijgen zonder eigen Forgejo. Dat er nu één sleutel voor twee clusters is, is een gevolg van hoe de recipient wordt meegegeven, niet van de indeling van de repo.

## Het model

Een AGE-sleutel is een eigenschap van **een verzameling bestanden met hetzelfde leesbeleid**: noem het een domein. Meestal valt een domein samen met een cluster; niet altijd (het wildcard-cert loopt over clusters heen).

De regel: **één sleutel per domein, vernoemd naar het domein.** `.sops.yaml` `path_regex` is de plek waar staat welke bestanden bij welk domein horen. Of twee domeinen dezelfde sleutelwaarde krijgen is dan een keuze in de waardeopslag, niet in code: en te herzien zonder iets te herschrijven.

Naamgeving voor de omgeving: `ZAD_AGE_KEY_<CLUSTER>` (hoofdletters, `-` wordt `_`), plus `ZAD_AGE_DEVELOPER_KEY` zonder suffix. Het variabele deel achteraan, zodat `ZAD_AGE_KEY_` één greppable namespace is.

## Wat er moet gebeuren

Zes stappen, in deze volgorde, elk apart te mergen.

### 1. Sleutelpad afleiden uit het cluster

Vervang `security/key.txt` / `security/sandbox-key.txt` door `security/keys/<cluster>.txt`, waarbij `<cluster>` gewoon `CLUSTER_TYPE` is. Eén `KEY_FILE`-definitie op één plek:

```
KEY_FILE: '{{default (printf "security/keys/%s.txt" (default "local" .CLUSTER_TYPE)) .KEY_FILE}}'
```

Daarmee vervallen de drie ternaries (`857`, `939`, `989`) en worden de veertien hardgecodeerde paden `{{.KEY_FILE}}`. `developer-key.txt` blijft staan waar hij staat: die is niet clustergebonden.

**Migratie zonder iets te hercoderen:** `security/keys/local.txt` en `security/keys/odcn-production.txt` beginnen als kopie van de huidige `key.txt`, `security/keys/sandboxed-local.txt` als kopie van `sandbox-key.txt`. Alles blijft dus leesbaar. Dat `local` en `odcn-production` nu dezelfde waarde hebben is een feit in de waardeopslag, niet in code: en daarmee later te splitsen met alleen stap 2.

Laat `security/key.txt` en `security/sandbox-key.txt` één release lang werken als terugval, met een waarschuwing, zodat een checkout van iemand anders niet ineens breekt.

### 2. Recipient-beleid naar `.sops.yaml`

Nieuw bestand in de root:

```yaml
creation_rules:
  - path_regex: .*/overlays/odcn-production/.*\.sops\.yaml$
    age: age1odcn...
  - path_regex: .*/overlays/fundament/.*\.sops\.yaml$
    age: age1fundament...
  - path_regex: .*/overlays/sandboxed-local/.*\.sops\.yaml$
    age: age1sandbox...
  - path_regex: .*/overlays/local/.*\.sops\.yaml$
    age: age1local...
```

Alleen **publieke** sleutels, dus dit bestand hoort gewoon in git: en is meteen de leesbare vastlegging van wie wat mag lezen, die nu nergens bestaat.

Haal daarna `--age "$AGE_PUBLIC_KEY"` weg op regels **397**, **928**, **1621**, **2637**: die vlag overrulet `creation_rules`, dus zolang hij er staat doet `.sops.yaml` niets.

Twee uitzonderingen die géén `.sops.yaml` gebruiken en dus expliciet een recipient houden:
- `encrypt-value-age` (290) en `encrypt-value-age-base64` (265): `age -r` op een losse waarde. Laat die de recipient uit `{{.KEY_FILE}}` halen in plaats van hard uit `security/key.txt`, anders blijven ze voor het verkeerde cluster versleutelen.
- `encrypt-secret` (353) werkt op een willekeurig `FILE`-pad; als dat pad geen regel matcht, valt sops terug op niets. Geef die taak een duidelijke fout in dat geval in plaats van een stille mislukking.

### 3. Sleutel op inhoud lezen, niet op regelnummer

Vervang overal:

```bash
SOPS_AGE_KEY="$(sed -n '3p' {{.KEY_FILE}})"
AGE_PUBLIC_KEY="$(sed -n '2p' {{.KEY_FILE}} | sed 's/# public key: //')"
```

door:

```bash
SOPS_AGE_KEY="$(grep -m1 '^AGE-SECRET-KEY-' {{.KEY_FILE}})"
AGE_PUBLIC_KEY="$(age-keygen -y {{.KEY_FILE}})"
```

Dan werkt zowel het volledige `age-keygen`-formaat als een kaal sleutelbestand, en hoeft provisioning geen bestandsindeling na te bouwen. `age-keygen -y` leidt de publieke sleutel af uit de private, dus de commentaarregel is nergens meer nodig.

Voeg één controle toe op de plek waar `KEY_FILE` gedefinieerd wordt: leeg resultaat → stop met *"geen AGE-sleutel voor cluster X in <pad>"*. Vandaag levert dat een ksops-fout op die niets over sleutels zegt.

### 4. Een resolver, zodat de herkomst inwisselbaar wordt

Nieuw: `scripts/resolve-age-key.sh`. Eén functie, drie bronnen in volgorde:

```bash
resolve_age_key() {                       # $1 = cluster, of "developer"
  local var="ZAD_AGE_KEY_$(echo "$1" | tr 'a-z-' 'A-Z_')"
  [ "$1" = developer ] && var=ZAD_AGE_DEVELOPER_KEY
  [ -n "${!var:-}" ]           && { printf '%s' "${!var}"; return; }   # env  (CI, dclaude)
  [ -s "security/keys/$1.txt" ] && { grep -m1 '^AGE-SECRET-KEY-' "security/keys/$1.txt"; return; }
  command -v op >/dev/null     && op read "op://ZAD/age-$1/key"        # later: vault
  return 1
}
```

En een `scripts/materialize-keys.sh` die dit één keer aan het begin van een setup draait en de bestanden neerzet als ze ontbreken (`umask 077`, nooit overschrijven wat er al ligt). Dat is het enige wat CI en een dclaude-container hoeven aan te roepen.

Waarom dit nu al de moeite is: het maakt de *herkomst* van een sleutel één functie. Of dat later een vault wordt, 1Password, of SOPS met een KMS-recipient is dan een regel erbij in plaats van een migratie door de hele Taskfile.

### 5. `.env-taskfile-current` uit het kritieke pad

De clusterkeuze is geen geheim maar blokkeert wel alles, omdat `dotenv:` bij parse-tijd leest. Laat `materialize-keys.sh` het bestand aanmaken uit `ZAD_CLUSTER_TYPE` als het ontbreekt:

```bash
[ -e .env-taskfile-current ] || cp ".env-taskfile-${ZAD_CLUSTER_TYPE:-sandboxed-local}" .env-taskfile-current
```

Let op de valkuil uit het andere plan: ODCN gebruikt twee mapnamen (`odcn` voor infrastructure, `odcn-production` voor bootstrap). Het sleutelpad volgt `CLUSTER_TYPE`, dus `odcn-production`.

Breid tegelijk `select-cluster` (regel 33) uit van een hardgecodeerde `case` naar een lijst afgeleid uit de aanwezige `.env-taskfile-*`-bestanden. Dan kost een vierde cluster daar geen regel.

### 6. Opruimen wat alleen bestond vanwege het sleutelpad

`sandbox:generate-infrastructure-secrets` (2561) en `sandbox:generate-bootstrap-secrets` (2574) zijn letterlijk `_generate-secrets-shared` met `CLUSTER_TYPE`, `CLUSTER_FOLDER`, `KEY_FILE` en `FIXED_PASSWORD` vastgezet. Zodra `KEY_FILE` uit `CLUSTER_TYPE` volgt, valt die regel weg en blijven het dunne wrappers om `FIXED_PASSWORD: admin1234`: behoud ze daarvoor, maar haal `KEY_FILE` eruit.

`sandbox:generate-age-key` (2522) en `generate-age-key` (226) kunnen één taak worden met een clusterargument.

Dat is de opbrengst die dit meer maakt dan opschonen: minder plekken die uit de pas kunnen lopen bij het volgende cluster.

## De toets

- `task deploy-operations-manager` werkt op `local`, `sandboxed-local` en `odcn-production` zonder dat er ergens een clusternaam in een `if` staat;
- een verzonnen vierde cluster toevoegen kost: een `.env-taskfile-<naam>`, een sleutel in `security/keys/`, een regel in `.sops.yaml`, en de overlays: geen enkele wijziging in `Taskfile.yaml`;
- `ZAD_AGE_KEY_SANDBOXED_LOCAL=... ./scripts/materialize-keys.sh && task sandbox:setup` draait op een verse checkout zonder één interactieve vraag over sleutels;
- alle bestaande `*.sops.yaml` blijven ontsleutelbaar: **niets is opnieuw versleuteld** in deze taak;
- een sleutelbestand zonder de twee commentaarregels werkt net zo goed als één met;
- een ontbrekende sleutel geeft de melding *"geen AGE-sleutel voor cluster X"*, niet een ksops-stacktrace;
- `grep -rn "security/key.txt\|security/sandbox-key.txt" Taskfile.yaml` levert alleen nog de terugvalmelding op.

## Waar op te letten

**De leeskant mag niet veranderen.** sops leest de recipients uit het bestand zelf; `creation_rules` gelden alleen bij versleutelen. `SOPS_AGE_KEY=...`, ksops en ArgoCD blijven dus werken zoals ze werken. Als een stap dat wél verandert, is die stap fout.

**Meerdere recipients per bestand mag.** `age:` in een creation_rule neemt een kommalijst. Waar twee clusters écht hetzelfde bestand moeten lezen (het wildcard-cert is de kandidaat) versleutel je naar beide *publieke* sleutels, zonder dat ze dezelfde private sleutel delen. Dat is strikt beter dan één gedeelde master, want er kan er later één uit.

**`sops updatekeys` is het gereedschap** als een domein van recipients verandert: cluster erbij, teamlid eruit. Dat her-versleutelt naar de recipients uit `creation_rules` zonder naar disk te ontsleutelen. **Niet nodig in deze taak**, wel het antwoord op "en als fundament straks een eigen sleutel krijgt".

**Laat waarden samenvallen, maar alleen in de waardeopslag.** `local` en `odcn-production` mogen dezelfde sleutelwaarde houden zolang dat zo uitkomt. Nergens in de Taskfile mag staan dát ze dat doen: anders is de ternary terug onder een andere naam.

**Splits productie af zodra het kan.** Zodra stap 1 er staat is `security/keys/odcn-production.txt` een aparte waarde geven puur een kwestie van `sops updatekeys` op de odcn-overlays. Dat is de eigenlijke winst van dit plan en zou niet lang moeten blijven liggen: vandaag ligt de productiesleutel op elke machine die lokaal kan bouwen.

**De sleutel hoort niet in een agent-container.** De sandbox draait sessies die PR-inhoud lezen, dus een container met een productiesleutel én netwerktoegang is een exfiltratiedoelwit. Regel: alleen de sandbox-sleutel komt ooit in zo'n container. Overweeg daarnaast of de runner op de host de `kustomize build` kan doen en de container alleen de gerenderde manifests geeft: dan hoeft de wortelsleutel er helemaal niet in. Dat is geen onderdeel van deze taak, maar het bepaalt wel dat stap 1 een echte scheiding moet zijn en geen naamswijziging.

**Geen vault in deze taak.** Een vault verplaatst secret-zero; hij lost hem alleen op waar een identiteit is om aan te binden (GitHub Actions OIDC), en niet in een container met `--network=host` en een gemounte docker.sock. Stap 4 is er zodat die keuze later goedkoop is; hem nu maken is voorbarig.

## Wat hierna nodig is, in DistributedClaude

Deze taak maakt de repo *provisioneerbaar*; het aanleveren gebeurt elders. Voor de volledigheid, zodat de twee kanten op elkaar aansluiten:

- `~/.dclaude/secrets/<project>.env` op de server, meegegeven met `docker run --env-file` (niet losse `-e`: blijft uit `ps` en shell-history);
- `images/claude-sandbox/entrypoint.sh:232` draait `scripts/materialize-keys.sh` als die bestaat, vóór `.dclaude/setup.sh`: ook zonder `--setup`, want zonder sleutels kan de sessie niets;
- `scripts/sandbox-cluster` kan zijn `save-secrets` / `restore_secrets` kwijt zodra de sleutels uit de omgeving komen;
- in CI dezelfde variabelenamen als GitHub Actions secrets, dus dezelfde aanroep.

Het contract tussen beide is precies één ding: **de namen `ZAD_AGE_KEY_<CLUSTER>` en `ZAD_AGE_DEVELOPER_KEY`.** Leg die vast in een `.env.provisioning.example` in deze repo, zodat de andere kant er iets aan kan aansluiten.
