# De tests die `task` aanroepen vinden een shim in plaats van het programma

**Status**: plan, nog niets gebouwd.
**Datum**: 2026-09-17
**Aanleiding**: zes tests zijn rood op main, gemeten op macOS met `task` geinstalleerd via asdf. Ze zijn niet rood in CI, want daar is `task` niet geinstalleerd en slaan ze over.

## De zes

```
tests/test_kind_registry.py::TestTaskfile::test_the_deploy_step_ends_green_on_a_server_without_a_registry
tests/test_sandbox_build_push.py::TestConfigureOverlay::test_task_fills_in_repo_override_and_tag[gewijzigd]
tests/test_sandbox_build_push.py::TestConfigureOverlay::test_task_fills_in_repo_override_and_tag[ongetrackt]
tests/test_sandbox_build_push.py::TestGenericUpdateOnSandbox::test_sandbox_stops_before_anything_runs
tests/test_sandbox_build_push.py::TestGenericUpdateOnSandbox::test_local_still_loads_into_kind
tests/test_sandbox_build_push.py::TestGenericUpdateOnSandbox::test_local_is_not_stopped
```

## Een oorzaak, twee gezichten

Alle zes zoeken het programma met `shutil.which("task")`. Waar `task` via een versiemanager is geinstalleerd geeft dat niet het programma terug maar een SHIM: een scriptje dat `exec asdf ...` doet. Zo'n shim heeft twee dingen nodig die de tests hem niet geven, en daarom ziet dezelfde fout er twee keer anders uit.

`test_kind_registry._prune` bouwt een eigen, kale omgeving:

```python
"PATH": f"{bindir}:/usr/bin:/bin:/usr/local/bin{extra_path}"
```

`extra_path` is de map van de shim, maar `asdf` zelf staat elders (hier `/opt/homebrew/bin`) en die map zit er niet in. De shim vindt zijn eigen motor niet: `exec: asdf: not found`, exit 127.

`test_sandbox_build_push` laat de omgeving intact maar draait met een `cwd` in een tmp-repo. asdf leidt de versie af uit `.tool-versions`, omhoog vanaf de werkmap, en buiten de repo is die er niet: `No version is set for command task`, exit 126.

De gedeelde aanname is dat een pad uit `shutil.which` betekent dat het programma uitvoerbaar is. Voor een shim is dat niet waar: die heeft context nodig, en de tests nemen die context juist weg.

Dit is geen mac-probleem. Een versiemanager gedraagt zich op Linux hetzelfde; de scheidslijn loopt tussen "via een versiemanager" en "direct geinstalleerd".

## Wat we bouwen

Een gedeelde hulpfunctie die het ECHTE programma teruggeeft, en de zes tests die hem gebruiken in plaats van `shutil.which`.

Het uitgangspunt: deze tests horen op een ontwikkelmachine te DRAAIEN, niet netjes over te slaan. Overslaan is de uitweg als `task` er echt niet is, niet als hij er wel is maar achter een shim.

1. Zoek `task` op de gewone manier op.
2. Stel vast of dat werkt door hem echt aan te roepen, losgekoppeld van de omstandigheden die de tests creeren: met een werkmap buiten de repo en een kale PATH. Slaagt dat, dan is dit pad het programma en zijn we klaar.
3. Slaagt dat niet, dan is het waarschijnlijk een shim. Vraag de versiemanager naar het echte pad (`asdf which task` geeft hier `~/.asdf/installs/task/3.41.0/bin/task`) en toets dat pad met dezelfde proef. Gemeten: dat pad draait wel vanuit `/tmp` met `PATH=/usr/bin:/bin`.
4. Levert ook dat niets op, dan pas overslaan, met een melding die zegt WAAROM: `task` gevonden op <pad>, maar niet aanroepbaar, en wat de proef teruggaf. Een kale skip die alleen "task ontbreekt" zegt terwijl `which` hem wel vond is misleidend, en dat is precies hoe deze zes onopgemerkt konden blijven.

De proef uit stap 2 is wat de tests eigenlijk willen weten, en hij is goedkoop: een `--version` met een timeout.

Waar de zes tests vandaag `shutil.which("task")` doen, vragen ze het voortaan aan die functie, en ze geven het gevonden pad door aan `subprocess.run` in plaats van de naam. `test_kind_registry._prune` hoeft dan ook de map van de shim niet meer aan PATH te plakken; een absoluut pad naar het programma heeft dat niet nodig.

Bepaal zelf waar de functie hoort. `tests/` heeft al gedeelde helpers; zet hem daar neer in plaats van hem twee keer te schrijven, en zoek eerst of er al zoiets staat.

## Wat we niet doen

Geen wijziging aan het Taskfile of aan de taken zelf. De zes tests meten iets dat klopt; alleen de manier waarop ze het programma zoeken deugt niet.

Geen `skipif` op het platform, en geen skip die een aanwezige `task` wegpoetst.

CI installeert `task` niet, dus daar blijven deze zes overslaan. Dat is een aparte vraag (willen we ze in CI laten draaien?) en hoort niet in deze PR.

## Klaar als

- De zes tests DRAAIEN en slagen op een machine waar `task` via asdf is geinstalleerd. Niet overgeslagen: gedraaid.
- Op een machine zonder `task` slaan ze over, met een melding die het onderscheid maakt tussen "niet gevonden" en "gevonden maar niet aanroepbaar".
- `shutil.which("task")` komt in deze twee testbestanden niet meer voor als toets op uitvoerbaarheid.
- De hele unitsuite, `ruff check`, `ruff format` en `pyright` zijn schoon.
