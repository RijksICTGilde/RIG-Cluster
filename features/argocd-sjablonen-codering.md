# De ArgoCD-sjablonen coderen elke waarde als YAML-scalar

OPI schrijft per project drie ArgoCD-objecten in `zad-argo-user-applications`: een
repository-Secret, een AppProject en een Application per deployment. De waarden daarin komen
uit het projectbestand (`repositories[].name`, `.username`, `.branch`, het ontsleutelde
wachtwoord) en gaan letterlijk een manifest in dat ArgoCD toepast. Twee sloten houden dat
heel: de codering op het sjabloon en een patroon op het projectbestand.

## Het slot op het sjabloon

Elke variabele in deze vier sjablonen gaat door de filter `yaml_scalar`
(`opi/generation/manifests.py`):

- `manifests/argo-repository-https.yaml.jinja`
- `manifests/argo-repository.yaml.jinja`
- `manifests/argocd-application.yaml.jinja`
- `manifests/argocd-appproject.yaml.jinja`

`yaml_scalar` laat de YAML-emitter quoten, dus elke geldige string overleeft de rit: een
backslash, een dubbele quote, een regeleinde, een `---` en een astraal teken. Naieve quoting
(`"{{ x }}"`) breekt op een backslash en vouwt een losse CR stil om tot een spatie;
`| tojson` schrijft een astraal teken als surrogaatpaar, waar SOPS op afbreekt. Daarom gaat
ook de SSH-variant, die `tojson` gebruikte, nu door `yaml_scalar`.

De sync-wave-annotatie blijft een string (`"0"`, `"1"`): kubernetes leest annotaties als
strings.

Een repository zonder wachtwoord levert nu `password: ""` in plaats van een leeg veld dat
YAML als `None` teruggeeft. `scripts/argo_rotation.py` rekende al op die vorm, en brengt zo'n
secret in step met zijn projectbestand.

## Het slot op het projectbestand

`$defs/repository` in `opi/schemas/project_v2.json`:

| Veld | Regel |
|---|---|
| `name` | kleine letters, cijfers en streepjes, begint en eindigt alfanumeriek (de naam wordt een Kubernetes-secretnaam) |
| `username` | letters, cijfers en `._@-`, geen witruimte |
| `branch` | letters, cijfers, `/` en `._-`, geen `..` en geen witruimte |
| `url`, `path` | zie `features/repository-pad.md` |

Een weigering noemt het veld (`repositories/0/branch`) en de beschrijving uit het schema. Er
is geen migratie: een bestand dat de regel niet haalt, wordt bij de eerstvolgende verwerking
geweigerd.

Het ontsleutelde wachtwoord komt niet uit het schema maar uit AGE, dus dat wordt bij de bron
gecontroleerd: `ArgoManager.prepare_repository_variables` weigert een waarde met `\r`, `\n`
of een NUL-byte, met een melding die de repository noemt en niet de waarde.

## Toetsen

- `operations-manager/python/tests/test_argocd_templates_escaping.py`: rendert de vier
  sjablonen per variabele met structuurtekens en eist precies een document met de waarde
  letterlijk op zijn plek.
- `operations-manager/python/tests/test_repository_naam_gebruiker_en_branch.py`: de patronen
  en de wachtwoordcontrole.

Elk ander sjabloon in `manifests/` valt onder `tests/test_template_injection_sweep.py`.
