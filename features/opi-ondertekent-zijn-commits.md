# OPI ondertekent zijn commits

OPI is de schrijver van de argo-repo en de deployments-repo, en ArgoCD rolt uit wat daar op
`main` staat. Dat een commit van OPI komt was tot nu toe een aanname: de afzender staat in de
commit, maar dat is tekst die iedereen kan zetten. Met een GPG-handtekening is de herkomst
aantoonbaar, wat BIO2 8.32 (wijzigingsbeheer) en 8.04 (toegang tot broncode) vragen.

Elke commit die OPI maakt draagt een handtekening van een sleutel per cluster. Het afdwingen
in ArgoCD (`signatureKeys` op het AppProject) staat in
`features/ondertekende-gitops-commits.md`.

## Hoe het werkt

`GIT_SIGNING_KEY_FILE` wijst naar een gemounte ASCII-armored GPG-privésleutel zonder
passphrase. Bij het opstarten importeert OPI die sleutel in een eigen `GNUPGHOME` onder
`TMPDIR` en onthoudt de fingerprint (`opi/utils/git_signing.py`). Niet onder `settings.TEMP_DIR`:
dat is op de sandbox een PVC, en de keyring hoort vluchtig te zijn.

De git-connector gebruikt die identiteit op al zijn commitwegen:

| weg | hoe |
|---|---|
| `commit_and_push`, `commit_changes`, `commit_and_push_changes` | `user.signingkey` + `commit.gpgsign true` in de git-config van de werkmap |
| de initial commit van een leeg repo | dezelfde git-config |
| de rebase na een push-conflict | dezelfde git-config: git ondertekent de commits die hij opnieuw maakt |
| `build_commit` (de ProjectStore-weg) | `-S<fingerprint>` bij `commit-tree`, want plumbing leest `commit.gpgsign` niet |

`GNUPGHOME` gaat als omgeving mee bij elk git-commando, zodat gpg de geïmporteerde sleutel
vindt. Een wrapper via `gpg.program` is daarvoor niet nodig.

## Zonder sleutel

Is `GIT_SIGNING_KEY_FILE` niet gezet of staat er geen bestand (de mount is `optional`, dus een
cluster zonder secret start gewoon), dan logt OPI bij het opstarten een WARNING en commit het
zonder handtekening, precies zoals voorheen. Een bestand dat gpg niet kan importeren is een
configuratiefout: dan start OPI niet, met het pad in de foutmelding.

## Configuratie

| waar | wat |
|---|---|
| `GIT_SIGNING_KEY_FILE` | pad van de sleutel, in `bootstrap/.../operations-manager/base/deployment.yaml` gezet op `/etc/opi/git-signing/signing-key.asc` |
| Secret `operations-manager-git-signing-key` | sleutel `signing-key.asc`, in de namespace van OPI, als volume gemount met `optional: true` |

## Gebruik

De sandbox maakt zijn eigen sleutel aan tijdens `task sandbox:setup`:

```bash
task sandbox:create-git-signing-key-secret   # genereert security/sandbox-git-signing-key.asc als die er niet is
```

De publieke sleutel en de fingerprint van het cluster waar je naar wijst, voor de ArgoCD-stap
en om een commit na te kijken:

```bash
task git-signing-key:public
```

Controleren of een commit echt ondertekend is:

```bash
git log --show-signature -3
```

Op productie zet een beheerder de sleutel zelf neer: genereer er een zoals de sandboxtaak dat
doet, en zet hem versleuteld als Secret in de overlay van het cluster. Zolang dat niet is
gebeurd, logt OPI daar de waarschuwing en blijven de commits onondertekend.

## Afhankelijkheden

- `gpg` in het OPI-image (`gnupg2`, staat er al) en lokaal voor de taken hierboven
- `opi/utils/git_signing.py` en de git-connector (`opi/connectors/git.py`)
