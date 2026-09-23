# SOPS en AGE met de hand

Een minimale gang door SOPS met een AGE-sleutel, voor wie handmatig een geheim wil
versleutelen of wil begrijpen wat de Taskfile en de CMP-plugin doen. In het normale werk hoef
je dit niet: `task generate-age-key` maakt de sleutel en `task generate-env-secrets-for-operations-manager`
maakt het SOPS-secret. Dit doc is er voor het geval je eronder wilt kijken.

Gaat het om het VERVANGEN van de platformsleutel, dan is `features/sops-sleutel-vervangen.md` het
stappenplan; hier staan de losse handelingen eronder.

Deze werkwijze stond in `sops-sandbox/steps.md`, een oefenmap uit de begindagen van de repo. Die
map is verwijderd omdat er een geldige AGE-sleutel in stond (`sops-key.txt`).

## 1. Een AGE-sleutelpaar maken

```bash
age-keygen -o sops-key.txt
```

Dat bestand bevat de private sleutel (de regel die met `AGE-SECRET-KEY-` begint) en de publieke
helft als commentaar (`age1...`). De publieke helft kun je ook altijd afleiden:

```bash
grep -m1 '^AGE-SECRET-KEY-' sops-key.txt | age-keygen -y
```

In deze repo horen sleutels in `security/`, en die map staat in `.gitignore`.

## 2. Het Kubernetes-secret maken

De CMP-plugin naast ArgoCD en de operations-manager lezen beide een secret met de naam
`sops-age-key`, met het HELE sleutelbestand als waarde onder de key `key`:

```bash
kubectl create secret generic sops-age-key \
  --from-file=key=sops-key.txt \
  -n rig-prd-operations \
  --dry-run=client -o yaml > sops-secret.yaml

kubectl apply -f sops-secret.yaml
```

Het hele bestand en niet alleen de sleutelregel: `bootstrap/rig-system/kustomize/sops-plugin.sh`
haalt er met `grep '^AGE-SECRET-KEY-'` de regel uit.

Dit secret staat in MEER dan een namespace, en niet overal met dezelfde sleutel erin. Wissel je
de platformsleutel, gebruik dan `scripts/set-sops-key-secret.py`: dat selecteert op sleutel en
niet op naam. Zie `features/sops-sleutel-vervangen.md`.

## 3. Een geheim versleutelen

```bash
PUBLIC_KEY="$(grep -m1 '^AGE-SECRET-KEY-' sops-key.txt | age-keygen -y)"

# een bestaand bestand
sops --encrypt --age "$PUBLIC_KEY" secret.yaml > secret.sops.yaml

# of vanaf stdin
echo "password: mysecretpassword123" \
  | sops --encrypt --age "$PUBLIC_KEY" --output-type yaml /dev/stdin > secret.sops.yaml
```

## 4. Nagaan of het werkt

```bash
SOPS_AGE_KEY_FILE=sops-key.txt sops --decrypt secret.sops.yaml
```

Een kustomize-build met de SOPS-generator erin test je zo:

```bash
SOPS_AGE_KEY="$(grep -m1 '^AGE-SECRET-KEY-' security/key.txt)" \
  kustomize build --enable-alpha-plugins --enable-exec \
  --load-restrictor LoadRestrictionsNone <pad>
```

## Waar je op moet letten

- De private sleutel hoort nooit in git. In deze repo: in `security/`, dat is gitignored.
- De publieke helft is geen geheim en mag in documentatie en in de SOPS-metadata staan.
- Gebruik per omgeving een eigen sleutelpaar: `security/key.txt` (platform),
  `security/sandbox-key.txt` (sandbox), `security/developer-key.txt` (het wildcard-certificaat).
- **`sops updatekeys` is geen sleutelwissel.** Dat wisselt de recipients en laat de data key
  staan, dus wie de oude sleutel ooit had opent het bijgewerkte bestand nog steeds. Gebruik
  `sops rotate`; de meting staat in `features/sops-sleutel-vervangen.md`.
