# SOPS en AGE met de hand

Een minimale gang door SOPS met een AGE-sleutel, voor wie handmatig een geheim wil
versleutelen of wil begrijpen wat de Taskfile en de CMP-plugin doen. In het normale werk hoef
je dit niet: `task generate-age-key` maakt de sleutel en `task generate-env-secrets-for-operations-manager`
maakt het SOPS-secret. Dit document legt uit wat we precies doen en waarom.

Gaat het om het VERVANGEN van de platformsleutel, dan is `features/sops-sleutel-roteren.md` het
stappenplan; hier staan de losse handelingen eronder.

## 1. Een AGE-sleutelpaar maken

`security/` staat in `.gitignore` en is daarmee de plek waar je lokaal een sleutelbestand kunt
neerzetten om deze handelingen te doen.

Ook `security/` is een tussenoplossing: een sleutel hoort op termijn in een CI/CD-omgeving of een
vault-achtige voorziening en niet in een map op een laptop. Waar precies is de vraag van het
lopende onderzoek naar sleutelbeheer en blast radius (RC-222).

```bash
age-keygen -o security/sops-key.txt
```

Dat bestand bevat de private sleutel (de regel die met `AGE-SECRET-KEY-` begint) en de publieke
helft als commentaar (`age1...`). De publieke helft kun je ook altijd afleiden:

```bash
grep -m1 '^AGE-SECRET-KEY-' security/sops-key.txt | age-keygen -y
```

## 2. Het Kubernetes-secret maken

De CMP-plugin naast ArgoCD en de operations-manager lezen beide een secret met de naam
`sops-age-key`, met het HELE sleutelbestand als waarde onder de key `key`:

```bash
kubectl create secret generic sops-age-key \
  --from-file=key=security/sops-key.txt \
  -n rig-prd-operations \
  --dry-run=client -o yaml > security/sops-secret.yaml

kubectl apply -f security/sops-secret.yaml
```

Elk ZAD project heeft een eigen sops-secret. De platformsleutel zit alleen in de platform
namespaces. Wissel je de platformsleutel, gebruik dan `scripts/set-sops-key-secret.py`. 
Zie `features/sops-sleutel-roteren.md`.

## 3. Een geheim versleutelen

```bash
PUBLIC_KEY="$(grep -m1 '^AGE-SECRET-KEY-' security/sops-key.txt | age-keygen -y)"

# een bestaand bestand
sops --encrypt --age "$PUBLIC_KEY" secret.yaml > secret.sops.yaml

# of vanaf stdin
echo "password: mysecretpassword123" \
  | sops --encrypt --age "$PUBLIC_KEY" --output-type yaml /dev/stdin > secret.sops.yaml
```

## 4. Nagaan of het werkt

```bash
SOPS_AGE_KEY_FILE=security/sops-key.txt sops --decrypt secret.sops.yaml
```

Een kustomize-build met de SOPS-generator erin test je zo:

```bash
SOPS_AGE_KEY="$(grep -m1 '^AGE-SECRET-KEY-' security/key.txt)" \
  kustomize build --enable-alpha-plugins --enable-exec \
  --load-restrictor LoadRestrictionsNone <pad>
```

## Waar je op moet letten

- De private sleutel hoort nooit in git. Lokaal zet je hem voorlopig in `security/`, dat is
  gitignored -- zie hierboven waarom dat een tussenoplossing is en geen eindplek.
- **Een `kind: Secret` telt als de sleutel zelf.** Base64 is geen versleuteling: `data.key`
  bevat het hele sleutelbestand.
- De publieke helft is geen geheim en mag in documentatie en in de SOPS-metadata staan.
- Gebruik per omgeving een eigen sleutelpaar: `security/key.txt` (platform),
  `security/sandbox-key.txt` (sandbox), `security/developer-key.txt` (het wildcard-certificaat).
- **`sops updatekeys` is geen sleutelwissel.** Dat wisselt de recipients en laat de data key
  staan, dus wie de oude sleutel ooit had opent het bijgewerkte bestand nog steeds. Gebruik
  `sops rotate`; zie ook `features/sops-sleutel-roteren.md`.
