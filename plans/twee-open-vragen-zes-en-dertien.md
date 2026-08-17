# De laatste twee open vragen uit de zad-cli-doorloop

Status: plan, 13 augustus 2026. Vraag **6** en **13** uit `plans/vragen-uit-zad-cli.md`; de derde open vraag (12) loopt als eigen taak omdat hij blokkerend is. Onder allebei staat `<!-- ruimte voor RIG-Cluster -->`.

## 6. Waarom is een net aangemaakt project asynchroon?

Een ontwerpvraag, geen storing: er is niets kapot, ze wachten netjes. Maar de sleutel die `POST /v2/projects` teruggeeft is ongeveer 3,5 seconde lang ongeldig (401), en ze vragen of dat wachten er hoort te zijn.

Hun lezing van onze code, die bevestigd of gecorrigeerd moet worden:

* de sleutel wordt **synchroon** gemaakt: `generate_base_project_file()` geeft `(project_dict, api_key)` terug vóór de taak bestaat;
* authenticatie kijkt naar het **record** en niet naar de sleutel (`get_project_store().get()` plus `compare_digest`), dus zolang de store het project niet kent is elke sleutel ongeldig, vandaar 401;
* de taak die dat record aanmaakt raakt **het cluster niet**: `rollout: False`, want het project heeft nog geen deployments.

Loop die drie na en zeg per punt of het klopt. Als het klopt, dan is de vervolgvraag eerlijk te beantwoorden: **als er niets uit te rollen valt, waarom is het aanmaken dan een taak?** Twee geldige antwoorden, en beide zijn beter dan geen antwoord:

* het hoort synchroon te zijn en dat is een verbetering die we willen; zeg dan wat het kost;
* het hoort asynchroon te blijven (een commit naar git, retries, één weg voor alle mutaties); zeg dan waarom, zodat zij hun draaiboek erop kunnen bouwen in plaats van erop te wachten.

Wat er in beide gevallen bij hoort: **is die 401 het juiste antwoord?** Een sleutel die geldig wordt zodra een taak klaar is, is iets anders dan een sleutel die niet klopt. Als er een antwoord bestaat dat "nog niet, probeer zo weer" zegt, is dat bruikbaarder dan 401 voor een client die niet kan zien welke van de twee het is.

## 13. `:refresh` belooft een webadres dat niet bestaat

Twee endpoints spreken elkaar tegen op hetzelfde moment, over dezelfde deployment. Project `p1-wan` heeft één component `worker`: geen poort, geen `publish-on-web`, dus geen ingress.

```
POST /v2/projects/{p}/:refresh   -> urls.productie.urls = {"worker": "https://worker-productie-p1-wan..."}
GET  /v2/projects/{p}/deployments/productie -> {"status":"Healthy","urls":{}}
curl https://worker-productie-p1-wan...  -> 404
```

Het deployment-endpoint heeft gelijk en de refresh niet. Zoek uit **waar de refresh die URL vandaan haalt**: kennelijk uit de naamgeving (wat het adres *zou* zijn) in plaats van uit wat er werkelijk gegenereerd is. Het deployment-endpoint kijkt blijkbaar wel naar de echte ingressen.

Eén bron, en dat moet de bron zijn die weet of er een ingress is. Een adres teruggeven dat 404 geeft is erger dan geen adres teruggeven: een client die dat opslaat of doorgeeft verspreidt een dood adres.

Kijk ook of de UI dezelfde fout maakt; "Publieke links" op de deploymentpagina komt uit `deployment.ingress_links`, en of dat dezelfde bron is als een van deze twee is het nagaan waard.

## De toets

- vraag 6 draagt een antwoord dat per punt zegt of hun lezing klopt, plus het besluit synchroon of asynchroon met de reden, plus of die 401 blijft;
- vraag 13 draagt een antwoord dat zegt waar de verkeerde URL vandaan kwam en wat de ene bron nu is;
- `:refresh` geeft voor `p1-wan` geen URL meer terug, of dezelfde die het deployment-endpoint geeft;
- er is een test die faalt op de oude code;
- beide `<!-- ruimte voor RIG-Cluster -->` zijn weg.

## Waar op te letten

**Vraag 6 is geen bug.** Het antwoord mag "dit blijft zoals het is" zijn; wat niet mag is geen antwoord, want dan blijven zij op iets wachten waarvan ze niet weten of het weggaat.

**Bij vraag 13 gaat het om de bron, niet om de tekst.** Twee endpoints die uit dezelfde plek lezen kunnen niet meer uiteenlopen; twee endpoints die allebei "netjes" hun eigen antwoord samenstellen wel.
