# De omgeving in de kopbalk

Naast het ZAD-woordmerk staat een tag met de omgeving waarin je werkt: Sandbox, ODCN Staging of ODCN Production. Wie meerdere omgevingen tegelijk open heeft, ziet zo meteen welk tabblad welk is.

## Hoe het werkt

Het label volgt uit `CLUSTER_MANAGER`, dat elke overlay al zet. Er is geen aparte instelling. De vertaling staat in `CLUSTER_TAGS` in `opi/core/template_helpers.py`. De Jinja-global `cluster_tag` levert het paar (label, kleur) aan `base_lotc.html.j2`. Daar staat de tag in de `action`-slot van `nldd-toolbar-title`, zodat NLDD hem naast het logo uitlijnt.

| `CLUSTER_MANAGER` | Label | Kleur (`nldd-tag`) |
|---|---|---|
| `sandboxed-local*` | Sandbox | accent |
| `local` | Local | accent |
| `odcn-staging` | ODCN Staging | warning |
| `odcn-production` | ODCN Production | neutral |

Een cluster dat niet in de tabel staat, toont zijn eigen naam in neutraal. Zo verschijnt een nieuw cluster nooit zonder label.

## Waarom productie neutraal is

Staging is de omgeving die je niet voor productie wilt aanzien, dus die valt op. Productie krijgt bewust geen rood: een balk die de hele dag alarm slaat, zie je na een dag niet meer.

## Niet te verwarren met

`environment_label` is de OTAP-markering van een deployment (Ontwikkeling, Test, ...), zie `opi/core/project_environment.py`. Dat gaat over het werk van een team, niet over het cluster waar het portaal draait.
