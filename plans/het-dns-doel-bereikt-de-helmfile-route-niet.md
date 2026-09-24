# Het DNS-doel bereikt de helmfile-route niet

OPI zet bij elke ingress die het zelf rendert de annotatie `external-dns.alpha.kubernetes.io/target`, zodat external-dns een CNAME in onze eigen zone schrijft. Voor deployments die via een helmfile draaien gebeurt dat niet. Die krijgen daardoor een CNAME naar de router-hostname van ODC-Noord, en die verwijzing overleeft de DNSSEC-validatie bij Google niet.

Deze wijziging laat de helmfile-route dezelfde annotatie meegeven als de gewone route, en werkt het migratierunbook bij naar de stand van vandaag.

## Waarom

Op 23-09-2026 bleek `docs.rijksapp.nl` voor een deel van de bezoekers onbereikbaar. Google Public DNS geeft op die naam `SERVFAIL` met EDE 12, "invalid denial of existence". Cloudflare en Quad9 accepteren hetzelfde antwoord wel, dus of iemand er last van heeft hangt af van zijn resolver. Dat maakt de storing lastig te herkennen: de een ziet een zwart scherm met "IP-adres niet gevonden", de ander merkt niets.

De oorzaak is een CNAME die de zonegrens overschrijdt:

```
docs.rijksapp.nl      CNAME  router-rig.rig.prd1.gn2.quattro.rijksapps.nl.   <- faalt
keycloak.rijksapp.nl  CNAME  router.rijksapp.nl.                             <- goed
```

Voor die fout bestaat de oplossing al. `get_external_dns_target_for_hostname` (`opi/core/cluster_config.py:1378`) geeft per hostname het juiste `router.<zone>`, en `project_manager.py` gebruikt hem op drie plekken bij het renderen van ingresses: regel 6450 (per-path), 6518 (root nice-url) en 6588 (bare-domain).

De helmfile-route komt daar niet langs. Die bouwt zijn waarden op in `_process_helmfile_deployment`: `base_values` uit de helmfile-definitie, `deployment_values` uit de deployment, een deep merge, en dan naar `values.to-sops.yaml` (`project_manager.py:4955-4980` op forgejo/main). In dat pad wordt `get_external_dns_target_for_hostname` nergens aangeroepen.

Het gevolg is geen theoretisch gat. Van de 22 hostnames in `rijksapp.nl` stonden er vandaag 10 verkeerd. Zeven daarvan waren ZAD-projecten waar de annotatie al op de ingress stond; die zijn inmiddels gemigreerd en valideren. De drie die overblijven zijn precies de helmfile-projecten: `docs`, `static-docs` en `grist`. Die kunnen niet gemigreerd worden zolang dit gat bestaat, want external-dns zou na het verwijderen van het record dezelfde kapotte waarde terugzetten, dan ook nog met een ownership-marker erop.

## Wat er nu is

Gemeten op 23-09-2026 tegen `forgejo/main` (18d99782f). De lokale checkout loopt 443 commits achter en is niet gebruikt om deze getallen te bepalen.

De charts zijn er klaar voor. In `MinBZK/mijn-bureau-infra` (ref `zad-compatible`) leest elk ingress-blok van de docs-chart `.Values.cluster.ingress.annotations`: `ingress`, `ingressAdmin`, `ingressCollaborationApi`, `ingressCollaborationWS`, `ingressCollaborationsApi` en `ingressMedia`, plus het blok in `values-static-nginx.yaml.gotmpl`. Eén waarde dekt dus `docs.rijksapp.nl` en `static-docs.rijksapp.nl` tegelijk. Dezelfde haak zit in de charts van nextcloud, meet, element en grist.

Dat blok wordt vandaag al gevuld, alleen niet door OPI. In het gegenereerde `values.sops.yaml` van `mb-docs-helmfile/production` staat:

```yaml
cluster:
    ingress:
        type: <versleuteld>
        className: null
        annotations:
            cert-manager.io/issuer: <versleuteld>
            haproxy.router.openshift.io/ip_whitelist: <versleuteld>
```

Die waarden komen uit het versleutelde `helm-values`-blok van de deployment in het projectbestand, met de hand gezet. De string `haproxy-openshift` komt in de OPI-broncode niet voor, dus OPI schrijft dit blok niet.

`_deep_merge_dicts` (`project_manager.py:4194`) is recursief: bij twee maps op dezelfde sleutel worden de sleutels samengevoegd in plaats van dat de een de ander vervangt. Een annotatie die OPI toevoegt komt dus naast de bestaande `cert-manager.io/issuer` en `ip_whitelist` te staan, niet in plaats daarvan.

## Wat er moet gebeuren

### 1. De helmfile-route geeft de target mee

In `_process_helmfile_deployment`, na de deep merge van `base_values` en `deployment_values` en voor het wegschrijven van `values.to-sops.yaml`, wordt `cluster.ingress.annotations` aangevuld met de external-dns target.

De hostname volgt uit de `publish-on-web`-service van de deployment, hetzelfde `base-domain` plus `subdomain` waar de rest van de ingress-generatie mee werkt. Voor `mb-docs-helmfile` is dat `docs.rijksapp.nl`, waarop `get_external_dns_target_for_hostname("odcn-production", "docs.rijksapp.nl")` de waarde `router.rijksapp.nl` teruggeeft.

Randvoorwaarden die het gedrag moeten sturen:

- Geeft de functie `None` terug, dan wordt er niets gezet. Dat is het geval voor hostnames in de eigen postfix-zone van het cluster, die hun DNS via de OpenShift-router krijgen en geen expliciete target nodig hebben.
- Een target die in de projectvalues al expliciet gezet is, wint. De deployment blijft de laatste stem, net als bij elke andere waarde in dit pad.
- Publiceert een deployment meerdere hostnames, dan is `cluster.ingress.annotations` één gedeelde map en kan er maar één waarde in. Binnen één zone is dat geen probleem. Publiceert een deployment hostnames in verschillende zones met verschillende targets, dan moet dat zichtbaar mislukken of gelogd worden, niet stil de laatste winnen.

### 2. Het runbook bijwerken

`docs/dns-router-zone-migration.md` draagt nog de snapshot van 08-05-2026 en klopt op drie punten niet meer.

De categorieën zijn achterhaald. Het runbook zet `algoritmes`, `amt.bzk`, `assessments`, `desa`, `website.desa`, `task-registry` en `frontend-main-wies` onder "Category B, needs project redeploy first". Die zeven hadden de annotatie vandaag al op hun ingress staan en zijn gewoon verwijderd en correct teruggekomen. Vervang de vaste categorielijsten door de vraag die er werkelijk toe doet, met de bijbehorende query: draagt de ingress achter deze naam de target-annotatie al?

Er ontbreekt een categorie. Een project waarvan de manifests niet door OPI gerenderd worden, maar door een externe chart, komt met een OPI-reprocess niet goed. Stap 5 van het runbook schrijft precies dat voor en klopt daar dus niet. Deze wijziging heft die categorie op, maar zolang hij bestond was hij onzichtbaar.

Twee vallen horen erbij, allebei vandaag opgelopen. Een naam kan twee CNAME's tegelijk dragen: `algoritmes` had naast de kapotte ook een handgemaakte goede. Wie alleen de kapotte weggooit, houdt een record over dat er gemigreerd uitziet maar geen ownership-marker heeft, en dus nog steeds buiten external-dns om leeft. Controleer na elk verwijderen de marker en niet alleen het doel. En de zone is de waarheid, niet de clusterinventaris: de dry-run gaf 12 records terwijl het cluster er maar 10 verklaarde. De twee extra, `frontend-productie-wies` en `frontend-production-wies`, hebben geen ingress en komen in geen enkele kubectl-query voor.

### 3. Testen

- Een deployment met een helmfile en een `publish-on-web`-hostname in een zone met een geconfigureerde `external_dns_target` levert die target in `cluster.ingress.annotations`.
- Een hostname zonder geconfigureerde target levert geen annotatie, en laat het blok verder ongemoeid.
- Een deployment die de annotatie zelf al zet, houdt zijn eigen waarde.
- De bestaande annotaties in het blok blijven staan naast de nieuwe.

## Wat hier niet bij hoort

De projectbestanden `mb-docs-helmfile.yaml` en `mb-grist-helmfile.yaml` staan in de projects-repo en worden niet door deze wijziging aangeraakt. Het handmatig toevoegen van de annotatie daar werkt wel, maar dan moet elk volgend helmfile-project het opnieuw doen. Daarom de fix in OPI.

Het verwijderen van de drie DNS-records is een handeling na deze wijziging, geen onderdeel ervan. De volgorde is: deze wijziging uitrollen, het project laten herverwerken, controleren dat de annotatie echt op de ingresses staat, en dan pas de records weggooien volgens het runbook. Die controle is de poort: `mb-docs-helmfile-production` faalt op dit moment zijn ArgoCD-sync op een kapotte Kyverno-policy, dus "sync geslaagd" is daar geen bruikbaar signaal en de ingress moet zelf bekeken worden.

De twee weesrecords wachten op een besluit en blijven staan.
