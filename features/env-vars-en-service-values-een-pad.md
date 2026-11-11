# Env-vars en service values: één pad

De waarden van een component (`user-env-vars` en `aliases`) en de config van een service
werden op vijf manieren geschreven: de v2-values-endpoints, het env-vars-venster, de v2
config PUT/DELETE/PATCH, de servicesecties in de UI, en add-component plus self-service als
extra schrijvers van `user-env-vars`. Elke weg had zijn eigen opslagvorm, validatie,
concurrency-strategie en uitrolscope. Sinds RC-481 lopen ze alle door dezelfde use case.

## Waar het pad loopt

```
ingang (route of task handler)
   |  vertaalt invoer naar een changeobject, en uitkomst of exception naar zijn antwoord
   v
opi/manager/service/
   configure.py  configure_service(ServiceConfigChange)        -> ServiceConfigResult
   values.py     set_component_values(ComponentValuesChange)   -> ComponentValuesResult
   rollout.py    roll_out(RolloutRequest)                      -> het processing-blok
   |
   v
opi/manager/project/service_config_mutations.py   de schrijfkern in het projectbestand
```

De use case raist typed exceptions uit `opi/manager/service/errors.py`; elke exception
draagt het `error_type` dat de API en de taakuitslag altijd al gebruikten, zodat een ingang
geen eigen tabel nodig heeft:

| exception | `error_type` |
|---|---|
| `InvalidProjectNameError` | `invalid_project_name` |
| `InvalidTargetError` | `invalid_target` |
| `InvalidValuesError` | `invalid_values` |
| `ChangeValidationError` | `validation_error` |
| `ChangeConflictError` | `conflict` |

Voortgang loopt via het `ProgressReporter`-protocol, zodat de use case niet van `runtime`
afhangt. Een ingang die in het verzoek zelf antwoordt geeft `NullProgress()` mee.

## De uitroltaak

`rollout_project` rolt een **al opgeslagen** wijziging uit naar precies de deployments die
hij raakte. Payload:

```json
{"project_name": "demo", "deployment_names": ["prod"]}
```

`deployment_names` afwezig of `null` betekent projectbreed. De scope komt van
`affected_deployments` in de ingang en is dezelfde lijst die de taakscope vastlegt, zodat
een wachtende taak niet wordt gesuperseded door een ronde die zijn deployments niet doet.

Daarvoor leende een ingang die in het verzoek schrijft het task type `create_project` met
`project_file_already_saved=True`. Dat zei het verkeerde in de takenlijst, kon de ronde niet
versmallen, en reset de OOM-tellers van alle deployments.

## Wat er per ingang veranderde

| | voorheen | nu |
|---|---|---|
| Opslagvorm | API canoniek, venster en creatie de ruwe tekst | overal `component_values.encode` |
| Validatie | per ingang anders, creatie zonder | overal `validate_and_parse_env_vars`, `validate_value_for_storage` en `validate_owned_value` |
| Het formulierveld | generieke `KeyValueConverter` en `KeyValueValidator` | `UserEnvVarsConverter` en `UserEnvVarsValidator` van de service zelf, die hetzelfde doen als de API |
| Concurrency | venster CAS met melding, API opnieuw toepassen | overal `mutate_and_commit_project` |
| Uitrolscope van het venster | hele project via `create_project` | de geraakte deployments via `rollout_project` |
| Autorisatie van het schrijven | venster `env-vars.manage` op de component, API `service.write-values` op het project | overal `service.write-values`; het venster OPENEN blijft `env-vars.manage`, en wie het mag openen maar niet opslaan ziet de waarden zonder opslaanknop |
| Approvals | alleen bij de upsert en in het portaal | bij upsert, clear, patch, add-component en update-component |
| No-op PUT of PATCH | rolde toch uit | rolt niet uit (`MutationResult.committed`) |
| Push-conflict | `internal_error` | `conflict`, met een melding die zegt dat opnieuw proberen de weg is |
| `env_vars` zonder publieke sleutel | viel stil weg | wordt geweigerd |

## De operatie REPLACE

Het env-vars-venster stuurt de HELE set, niet een losse sleutel. Daarvoor heeft
`ValuesOperation` naast `add`, `patch`, `delete` en `clear` nu `replace`: de meegegeven map
wordt de set. Hij weigert geen namen, want hij komt uit een editor die de auteur elke naam
in die set liet zien.

`replace` is bewust GEEN endpoint: de API adresseert per sleutel, zodat twee clients
dezelfde set kunnen bijwerken zonder elkaars sleutels weg te gooien.

## Eén lezer, één locator, één encoder

- **Lezen**: `component_values.decode`. `project_env_vars.read_user_env_vars` is de adapter
  die de leesroutes nodig hebben en houdt alleen de foutvorm aan de rand (`None` in plaats
  van een exception), zodat een pagina met veel componenten niet omvalt over één blok dat
  niet te lezen is. Dit gaat over de LEESROUTES: de uitrolweg heeft zijn eigen lezer
  (`project_file_handler._extract_user_env_vars`), die buiten de meting van RC-481 viel.
- **Lokaliseren**: `component_values.locate`.
- **Versleutelen**: `component_values.encode` (of `encode_with_key` voor een aanroeper die
  de publieke sleutel al heeft, zoals `build_component_config` bij het aanmaken en de
  converter van het formulierveld).

## Wat NIET veranderde

- Een platformveld in de invoer: de API weigert hem met een 422, de UI zet hem stil terug.
  Twee uitkomsten, met opzet: een API-client die een veld stuurt denkt dat hij het heeft
  gezet, en een formulier dat de hele sectie terugstuurt heeft hem nooit zelf bedoeld.
- `aliases` bij het aanmaken zonder publieke sleutel: die worden nog als onversleutelde
  mapping opgeslagen, een vorm die het schema toestaat.
- Het API-contract: geen bestaande succesvolle statuscode veranderde, en `?rollout=false`
  werkt zoals het werkte.
