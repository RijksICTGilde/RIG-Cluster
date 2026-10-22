# Secret Templates

This directory contains Kubernetes Secret templates that are processed by the `generate-infrastructure-secrets-for-cluster` task to create SOPS-encrypted secrets for different cluster environments.

## How It Works

1. **Templates** - YAML files in this directory serve as templates for generating secrets
2. **Annotations** - Fields requiring password generation are marked with `@secret-gen` annotations
3. **Processing** - The `task generate-infrastructure-secrets-for-cluster` command processes these templates for the cluster selected with `task select-cluster`
4. **Encryption** - Secrets are encrypted using SOPS with the AGE key from `security/key.txt`
5. **Output** - Generates SOPS-encrypted `.sops` files in `../config/overlays/<cluster-name>/`

## Adding New Secrets

To add a new secret template:

1. **Create the template file** (e.g., `my-service-secret.yaml`):
```yaml
apiVersion: v1
kind: Secret
metadata:
  name: my-service-credentials
  namespace: rig-system
type: Opaque
stringData:
  USERNAME: myuser
  PASSWORD: "changeMe123!" # @secret-gen:random:16
  API_KEY: "defaultkey" # @secret-gen:random:32
```

2. **Use annotations for fields that need generated passwords**:
   - `# @secret-gen:random:16` - Random password (16 characters)
   - `# @secret-gen:random:20` - Random password (20 characters)
   - `# @secret-gen:bcrypt:16` - BCrypt hash of random password (16 chars)

3. **Run the generator**:
```bash
task select-cluster
task generate-infrastructure-secrets-for-cluster
```

## Supported Password Types

- **`random:N`** - Random alphanumeric password of N characters
- **`bcrypt:N`** - BCrypt hash of a random N-character password (for ArgoCD, etc.)

## Een template geldt voor ELK cluster

Een waarde zonder `@secret-gen`-annotatie gaat letterlijk mee, naar elk cluster waarvoor
de generatie draait. Een geheime waarde die hier vast staat is dus een gedeeld geheim op
sandbox, staging en productie tegelijk: zo kwam `auth: "admin:CHANGE-THIS-PASSWORD"` uit
`chisel-auth-secret.yaml` op odcn-staging terecht.

De regel die daaruit volgt:

- **elke geheime waarde draagt `@secret-gen`.** Een gebruikersnaam, een host of een
  afzenderadres is geen geheim en mag vast staan;
- **vaste wachtwoorden horen in een `sandboxed-local`-overlay of een sandbox-taak.** De
  sandbox houdt bewust zijn vaste wachtwoorden, maar via `FIXED_PASSWORD` in
  `sandbox:generate-infrastructure-secrets`, niet via een waarde in een template;
- een veld dat echt geen geheim is, staat met de reden op `NIET_GEHEIM` in
  `operations-manager/python/tests/test_secrettemplate_geen_vaste_waarde.py`. Die toets
  faalt op elk ander veld zonder annotatie, en is de poort die dit vasthoudt.

De generatie heeft geen samengestelde vorm: ze vult per veld. Wil een app `gebruiker:wachtwoord`
in één waarde, splits het dan (zie `chisel-auth-secret.yaml`) en voeg het in het Deployment
samen met env-expansie `$(VAR)`.

## Een bestaand cluster met een oude waarde

De generatie slaat een secret over dat er al staat: dat is de bescherming tegen ongevraagde
rotatie, en het betekent dat een cluster met de oude waarde die houdt. Welke weg hem vervangt
hangt ervan af of alleen de WAARDE verandert of ook de VELDNAMEN.

Alleen de waarde: de rotatieweg, die het secret per component kent.

```bash
task select-cluster
task edit-secret -- --component <component> --check    # drift meten, verandert niets
task edit-secret -- --component <component> --apply    # app, bestand, sync, herstart
```

Ook de veldnamen: de bewerkmodus, `task edit-secret` zonder `--component`.

```bash
task select-cluster
task edit-secret               # cluster, dan "Secret-velden bewerken", dan het secret, dan per veld
```

De rotatieweg kan dit niet, en faalt er stil in. Ze vult de velden die het BESTAANDE secret
heeft (`values = dict(ctx.current)`), en `apply()` gooit elk templateveld weg dat daar niet in
staat. Op odcn-staging, waar `chisel-auth-credentials` nog één veld `auth` draagt, levert dat
een secret op dat `auth` houdt en `username` mist, dus de `secretKeyRef` van het chisel-Deployment
vindt zijn key niet; `--check` strandt daarvoor al op de ontbrekende `password`. De bewerkmodus
loopt `fields_of(template)` af en is dus de weg die de nieuwe veldnamen oplevert.

De componenten staan in `scripts/secret_rotate.py`; elke template heeft daar een rij of
een benoemde uitzondering, en `coverage_gaps()` houdt dat sluitend. Een nieuwe template
zonder rij maakt die poort rood.

## Important Notes

- Only fields with `@secret-gen` annotations will have passwords generated
- Fields without annotations keep their template values
- The task generates both encrypted secrets and a password overview file
- All secrets must use `stringData` format (not `data`)
- Generated files follow the kustomize overlay pattern

## Generated Structure

After running it with `sandboxed-local` selected:

```
../config/overlays/sandboxed-local/
├── kustomization.yaml          # References decrypt-sops.yaml
├── decrypt-sops.yaml          # Lists all encrypted files
├── my-service-secret.yaml.sops # Your encrypted secret
└── ...other-secrets.yaml.sops
```

## Password Overview

The task also generates `secrets-overview-<cluster>.txt` in the project root containing all generated passwords for secure storage. This file is automatically gitignored.
