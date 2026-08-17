# Technische review: BIO en NORA

Status: plan, 13 augustus 2026. Dit is een **reviewopdracht**, geen bouwtaak. Wat eruit komt is een oordeel met bewijs, niet een stapel wijzigingen. Vind je iets dat evident en klein is, meld het; repareren is een volgende beslissing.

ZAD is een zelfservice-deploymentplatform voor de rijksoverheid: gebruikers definiëren hun infrastructuur in een YAML-projectbestand en het platform richt databases, opslag, authenticatie en Kubernetes-resources in via GitOps. Dat betekent dat een fout hier niet één applicatie raakt maar de scheiding tussen projecten van verschillende teams.

## Wat wij van je vragen

Beoordeel het systeem tegen twee kaders, en beide in díé volgorde:

1. **BIO** (Baseline Informatiebeveiliging Overheid): de beveiligingsmaatregelen die voor een rijksoverheidssysteem gelden.
2. **NORA** (Nederlandse Overheid Referentie Architectuur): de architectuurprincipes, met nadruk op de principes die voor een platform als dit betekenis hebben.

**Geen algemene checklist afvinken.** Een bevinding die net zo goed over een willekeurig ander systeem had kunnen gaan, is geen bevinding. Wat wij willen weten is waar DIT systeem, met DEZE code, afwijkt van wat die kaders vragen, met de vindplaats erbij.

## Waar je moet kijken, en wat er al bekend is

Dit is geen uitputtende lijst maar een startpunt, zodat je niet opnieuw hoeft te ontdekken wat er staat.

**Geheimen.** SOPS + AGE voor bestanden, AGE voor runtime (`opi/utils/age.py`, `opi/utils/sops.py`). Elke sleutel per project. In `security/` staan sleutelbestanden die niet in de repo horen; controleer wat daar wel en niet gecommit is, en wat er in de historie zit. **Bekend voorval van vandaag:** een script schreef zijn ingelogde sessiekoekje naar `scripts/.sandbox-sessie.json` en dat is meegecommit en gepusht (nu verwijderd en in `.gitignore`). Dat is precies het soort fout dat je zoekt; kijk of er meer van zijn.

**Toegang.** SSO via Keycloak, plus een API-sleutel per project. `opi/middleware/authorization.py` (RBAC en gebruikersisolatie), `opi/middleware/security_headers.py`, en de CSRF-laag. De rollen zijn admin, owner en ontwikkelaar. Vraag: kan een sleutel van project A iets bij project B, in welke weg dan ook?

**Een bewust genomen risico dat je moet wegen.** Bij een restore mag de aanroeper een externe bestemming opgeven (host, gebruiker, wachtwoord) en er wordt niet gecontroleerd of die bij hem hoort. De redenering is opgeschreven in `plans/vragen-uit-zad-cli.md` onder vraag 7: je moet die credentials al kennen, en de restore-pod draait in je eigen namespace onder je eigen NetworkPolicy. Wij vinden dat verdedigbaar; zeg of jij dat ook vindt en wat BIO daarvan zou vinden.

**Netwerk.** NetworkPolicies per project, ingress per component, TLS per deployment. Er staat vandaag een allow-all-masker in git in afwachting van per-component policies; zoek dat op en weeg het.

**Auditsporen.** `opi/services/runs_service.py` en `opi/services/approvals.py` houden bij wie wat deed. Keycloak-audit-events staan in de realm-blueprints maar zijn op productie nog niet aan. Vraag: is achteraf te reconstrueren wie welke wijziging heeft doorgevoerd, en hoe lang blijft dat bewaard?

**Multi-tenancy.** Elk project krijgt een eigen namespace, database(gebruiker), bucket en Keycloak-realm. De scheiding daartussen is het hart van dit systeem. Zoek naar plekken waar die scheiding leunt op een naam in plaats van op een afgedwongen grens.

## Wat een bruikbare bevinding is

Per bevinding: **wat**, **waar** (bestand en regel), **wat er mis kan gaan** (een concreet scenario, geen categorie), **welk BIO- of NORA-punt het raakt**, en **hoe zwaar** je het weegt.

Zeg er ook bij wat je NIET hebt kunnen vaststellen. "Ik kon niet nagaan of X" is bruikbaar; een vermoeden dat als constatering is opgeschreven niet.

En noem expliciet wat er **goed** staat. Een review die alleen gebreken opsomt geeft geen beeld van waar het systeem staat, en het is voor ons net zo belangrijk om te weten welke maatregelen we niet opnieuw hoeven te bedenken.

## Waar op te letten

**Meet, lees niet alleen.** Er draait een sandbox (`*.sandbox.rijksapp.dev`) en er is `scripts/kijk_sandbox.py` om als ingelogde gebruiker een pagina op te halen. Een aanname over gedrag die niet is nagespeeld, is deze week vier keer fout gebleken.

**Productie is read-only.** `kubectl` lezen mag; wijzigen gaat uitsluitend via Git en ArgoCD.

**Geen wijzigingen aan de code in deze taak**, op een evidente tikfout na. Dit is een review; wat eruit komt bepaalt wat er daarna gebeurt.
