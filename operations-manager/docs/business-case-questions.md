.# Business Case Vragen - Operations Manager (OPI)

Dit document bevat een uitgebreide lijst van vragen die beantwoord moeten worden voor het opstellen van een complete business case voor het Operations Manager project.

---

## 1. Probleemstelling & Huidige Situatie

### 1.1 Het Kernprobleem
- Wat is het specifieke probleem dat Operations Manager oplost?
- Hoe manifesteert dit probleem zich in de dagelijkse praktijk?
- Sinds wanneer speelt dit probleem?
- Waarom is het probleem nu urgent genoeg om aan te pakken?

### 1.2 Impact van het Probleem
- Hoeveel tijd kost het huidige proces voor het opzetten van een nieuwe projectomgeving?
- Hoeveel FTE is er betrokken bij handmatige Kubernetes configuratie?
- Wat zijn de huidige wachttijden voor projectteams om een omgeving te krijgen?
- Hoeveel projecten/teams worden hierdoor beïnvloed?
- Wat zijn de kosten van vertragingen in projectoplevering door infrastructuurwachttijden?

### 1.3 Huidige Werkwijze
- Hoe worden Kubernetes omgevingen momenteel ingericht?
- Welke handmatige stappen zijn er nodig voor een nieuwe omgeving?
- Wie voert deze stappen uit (rollen/functies)?
- Welke tools worden momenteel gebruikt?
- Wat zijn de bekende bottlenecks in het huidige proces?
- Hoe vaak gaan dingen fout in het huidige proces?
- Wat zijn de herstelkosten bij fouten?

### 1.4 Scope van het Probleem
- Hoeveel projecten maken gebruik van Kubernetes binnen de organisatie?
- Hoeveel nieuwe projectomgevingen worden gemiddeld per maand/kwartaal/jaar aangevraagd?
- Welke afdelingen/teams zijn afhankelijk van dit proces?
- Groeit de vraag naar Kubernetes omgevingen? Zo ja, in welk tempo?

---

## 2. Strategische Context

### 2.1 Organisatiestrategie
- Hoe past Operations Manager binnen de bredere IT-strategie van de organisatie?
- Sluit het aan bij DevOps/Platform Engineering initiatieven?
- Ondersteunt het de digitale transformatie doelstellingen?
- Hoe draagt het bij aan de zelfvoorzienendheid van projectteams?

### 2.2 Business Drivers
- Wat zijn de primaire business drivers voor dit project?
- Welke strategische doelen worden ondersteund?
- Is er wetgeving of compliance die dit project noodzakelijk maakt?
- Zijn er concurrentieoverwegingen die meespelen?

### 2.3 Prioriteit
- Hoe belangrijk is dit project ten opzichte van andere IT-initiatieven?
- Wat gebeurt er als dit project niet wordt uitgevoerd?
- Zijn er externe deadlines of verplichtingen?

---

## 3. Voorgestelde Oplossing

### 3.1 Oplossingsoverzicht
- Wat doet Operations Manager precies?
- Wat zijn de kernfunctionaliteiten?
- Hoe verschilt het van de huidige werkwijze?
- Welke technologieën worden gebruikt (FastAPI, GitOps, ArgoCD, etc.)?

### 3.2 Functionaliteiten
- Self-service portaal: welke acties kunnen gebruikers zelf uitvoeren?
- Welke centrale services worden ondersteund (PostgreSQL, Keycloak, Vault, MinIO)?
- Hoe werkt de automatische provisioning?
- Welke omgevingstypes worden ondersteund (POC, Pilot, Production)?
- Hoe worden secrets en credentials beheerd?
- Welke integraties zijn er met bestaande systemen?

### 3.3 Technische Architectuur
- Hoe integreert OPI met de bestaande Kubernetes infrastructuur?
- Welke afhankelijkheden zijn er met andere systemen?
- Hoe wordt beveiliging gewaarborgd?
- Wat is de schaalbaarheid van de oplossing?
- Hoe wordt high availability gerealiseerd?

### 3.4 Gebruikerservaring
- Wie zijn de eindgebruikers van het systeem?
- Hoe ziet de gebruikersreis eruit voor het aanvragen van een omgeving?
- Welke training is nodig voor gebruikers?
- Wat is de verwachte adoptiegraad?

---

## 4. Stakeholderanalyse

### 4.1 Betrokken Partijen
- Wie is de projectsponsor?
- Wie is de business owner?
- Welke afdelingen/teams zijn stakeholder?
- Wie zijn de eindgebruikers?
- Wie zijn de beheerders van het platform?

### 4.2 Verwachtingen per Stakeholder
- Wat verwacht het management van dit project?
- Wat verwachten de projectteams (gebruikers)?
- Wat verwacht het platform/operations team?
- Wat verwacht de security afdeling?
- Zijn er tegenstrijdige verwachtingen?

### 4.3 Draagvlak
- Is er voldoende draagvlak voor dit project?
- Welke weerstand kan worden verwacht?
- Hoe wordt buy-in verkregen bij sceptici?
- Wie zijn de champions/ambassadeurs?

---

## 5. Financiële Analyse

### 5.1 Huidige Kosten (Baseline)
- Wat zijn de huidige jaarlijkse kosten voor handmatige omgevingsprovisioning?
  - Personeelskosten (FTE x uren)
  - Tooling kosten
  - Overhead kosten
- Wat zijn de indirecte kosten?
  - Kosten van vertragingen in projecten
  - Kosten van fouten/incidents
  - Opportuniteitskosten
- Wat zijn de verborgen kosten?
  - Kennisafhankelijkheid van specifieke personen
  - Inconsistentie in omgevingen
  - Security risks door handmatige configuratie

### 5.2 Investeringskosten (CAPEX)
- Wat zijn de ontwikkelkosten tot nu toe geweest?
- Wat zijn de resterende ontwikkelkosten om productie-ready te zijn?
- Welke infrastructuurkosten zijn nodig?
- Welke licentiekosten zijn er?
- Wat zijn de implementatie/migratiekosten?
- Wat zijn de trainingskosten?
- Is er externe ondersteuning nodig? Kosten?

### 5.3 Operationele Kosten (OPEX)
- Wat zijn de jaarlijkse onderhoudskosten?
- Wat zijn de infrastructuurkosten per jaar?
- Hoeveel FTE is nodig voor beheer en ondersteuning?
- Wat zijn de licentiekosten per jaar?
- Wat zijn de trainingskosten voor nieuwe medewerkers?

### 5.4 Besparingen & Baten
- Hoeveel tijd wordt bespaard per omgevingsaanvraag?
- Hoeveel omgevingsaanvragen per jaar x tijdsbesparing = totale tijdsbesparing?
- Wat is de waarde van de tijdsbesparing in euro's?
- Wat zijn de productiviteitswinsten voor projectteams?
- Wat zijn de besparingen door minder fouten/incidents?
- Zijn er besparingen door efficiënter resourcegebruik?
- Zijn er inkomstenvoordelen door snellere time-to-market?

### 5.5 Return on Investment (ROI)
- Wat is de totale investering over 3-5 jaar?
- Wat zijn de totale baten over 3-5 jaar?
- Wat is de ROI?
- Wat is de terugverdientijd?
- Wat is de Net Present Value (NPV)?
- Wat is de Internal Rate of Return (IRR)?

### 5.6 Total Cost of Ownership (TCO)
- Wat is de TCO over 3 jaar?
- Wat is de TCO over 5 jaar?
- Hoe verhoudt de TCO zich tot alternatieven?

---

## 6. Niet-Financiële Baten

### 6.1 Operationele Baten
- Hoeveel sneller kunnen nieuwe omgevingen worden opgezet?
- Hoe verbetert de consistentie van omgevingen?
- Hoe verbetert de reproduceerbaarheid?
- Hoe verbetert de documentatie en auditability?

### 6.2 Strategische Baten
- Hoe ondersteunt dit DevOps/Platform Engineering adoptie?
- Hoe verbetert dit de developer experience?
- Hoe draagt dit bij aan organisatorische wendbaarheid?
- Maakt dit schaalbaarheid mogelijk voor toekomstige groei?

### 6.3 Kwaliteitsverbetering
- Hoe verbetert de kwaliteit van deployments?
- Hoe worden security risico's verminderd?
- Hoe verbetert de compliance?
- Hoe verbetert de betrouwbaarheid?

### 6.4 Kennismanagement
- Hoe vermindert dit kennisafhankelijkheid van individuen?
- Hoe wordt kennis geborgd in het systeem?
- Hoe verbetert dit onboarding van nieuwe teamleden?

---

## 7. Risico's & Mitigatie

### 7.1 Technische Risico's
- Wat zijn de technische complexiteiten?
- Welke afhankelijkheden zijn er met externe systemen?
- Wat als ArgoCD, Keycloak of andere componenten falen?
- Wat zijn de risico's rond security en credentials?
- Hoe wordt omgegaan met backward compatibility?
- Wat zijn de risico's van vendor lock-in?

### 7.2 Organisatorische Risico's
- Is er voldoende kennis in-house om dit te bouwen en onderhouden?
- Wat als key developers vertrekken?
- Is er voldoende capaciteit naast andere projecten?
- Hoe wordt omgegaan met veranderende prioriteiten?
- Wat als de adoptie tegenvalt?

### 7.3 Projectrisico's
- Wat zijn de risico's rond planning en budget?
- Wat als de scope uitbreidt (scope creep)?
- Wat als de requirements veranderen?
- Zijn er afhankelijkheden met andere projecten?

### 7.4 Operationele Risico's
- Wat als het platform down gaat?
- Hoe wordt disaster recovery geregeld?
- Wat zijn de risico's voor bestaande omgevingen bij migratie?
- Hoe wordt rollback geregeld bij problemen?

### 7.5 Risicomitogatie
- Welke maatregelen worden genomen per risico?
- Wat zijn de contingency plans?
- Welke risico's zijn geaccepteerd?
- Hoe worden risico's gemonitord?

---

## 8. Implementatie & Planning

### 8.1 Huidige Status
- Wat is de huidige ontwikkelstatus van Operations Manager?
- Welke functionaliteiten zijn af?
- Welke functionaliteiten moeten nog worden gebouwd?
- Wat is de technische schuld?

### 8.2 Fasering
- Welke fasen worden onderscheiden?
- Wat zijn de deliverables per fase?
- Welke milestones worden gehanteerd?
- Wanneer is Minimum Viable Product (MVP) bereikt?
- Wanneer is volledige productie-readiness bereikt?

### 8.3 Tijdlijn
- Wat is de verwachte doorlooptijd tot productie?
- Wat zijn de kritieke pad activiteiten?
- Welke afhankelijkheden beïnvloeden de planning?
- Is er een pilotfase gepland?
- Wanneer start de brede uitrol?

### 8.4 Resources
- Hoeveel FTE is nodig voor ontwikkeling?
- Hoeveel FTE is nodig voor beheer na oplevering?
- Welke competenties zijn nodig?
- Is er externe inhuur nodig?

### 8.5 Migratie
- Hoe worden bestaande omgevingen gemigreerd?
- Is er een parallelle operatie periode?
- Hoe lang duurt de migratie?
- Wat is de impact op lopende projecten?

---

## 9. Alternatieven

### 9.1 Geïdentificeerde Alternatieven
- Wat zijn de alternatieven voor Operations Manager?
  - Niets doen (status quo)
  - Commercial off-the-shelf (COTS) oplossingen
  - Open source alternatieven (Backstage, Crossplane, etc.)
  - Managed Kubernetes platforms (OpenShift, Rancher, etc.)
  - Hybride aanpak

### 9.2 Vergelijking per Alternatief
Voor elk alternatief:
- Wat zijn de voor- en nadelen?
- Wat zijn de kosten?
- Wat is de time-to-value?
- Hoe goed past het bij de organisatie?
- Wat zijn de risico's?

### 9.3 Make vs Buy Analyse
- Waarom eigen ontwikkeling in plaats van een bestaande oplossing?
- Wat zijn de unieke requirements die eigen ontwikkeling rechtvaardigen?
- Wat zijn de lange termijn implicaties van eigen ontwikkeling?

---

## 10. Governance & Compliance

### 10.1 Governance
- Hoe wordt het project bestuurd?
- Wie heeft decision making authority?
- Hoe worden wijzigingen beheerd (change management)?
- Hoe wordt voortgang gerapporteerd?

### 10.2 Compliance
- Aan welke regelgeving moet worden voldaan?
- Zijn er specifieke security eisen (ISO 27001, NEN 7510)?
- Zijn er privacy/GDPR overwegingen?
- Zijn er sector-specifieke eisen?
- Hoe wordt compliance aangetoond?

### 10.3 Audit & Logging
- Hoe worden acties gelogd?
- Is er een audit trail?
- Hoe lang worden logs bewaard?
- Wie heeft toegang tot audit informatie?

---

## 11. Ondersteuning & Onderhoud

### 11.1 Support Model
- Hoe wordt ondersteuning georganiseerd na go-live?
- Wat zijn de support levels (L1, L2, L3)?
- Wat zijn de verwachte response times?
- Wie is verantwoordelijk voor ondersteuning?

### 11.2 Onderhoud
- Hoe wordt het platform onderhouden?
- Hoe vaak worden updates uitgerold?
- Wie is verantwoordelijk voor security patches?
- Hoe wordt technische schuld beheerd?

### 11.3 Doorontwikkeling
- Wat is de roadmap na initiële oplevering?
- Welke features staan op de backlog?
- Hoe worden nieuwe requirements geprioriteerd?
- Hoeveel capaciteit is beschikbaar voor doorontwikkeling?

---

## 12. Meetbaarheid & Succesfactoren

### 12.1 Key Performance Indicators (KPIs)
- Wat zijn de KPIs om succes te meten?
  - Time-to-provision (tijd om omgeving op te zetten)
  - Aantal self-service requests vs handmatige requests
  - Gebruikerstevredenheid
  - Aantal incidents gerelateerd aan provisioning
  - Uptime van het platform
  - Adoptiegraad

### 12.2 Baseline Metingen
- Wat zijn de huidige waarden van deze KPIs?
- Hoe worden baseline metingen verzameld?

### 12.3 Targets
- Wat zijn de doelwaarden per KPI?
- Wanneer moeten deze targets bereikt zijn?
- Hoe worden deze targets gemonitord?

### 12.4 Kritieke Succesfactoren
- Wat moet absoluut goed gaan voor projectsucces?
- Welke aannames liggen ten grondslag aan de business case?
- Wat zijn de go/no-go criteria?

---

## 13. Communicatie

### 13.1 Communicatieplan
- Hoe wordt over het project gecommuniceerd?
- Wie zijn de doelgroepen?
- Welke communicatiekanalen worden gebruikt?
- Wat is de frequentie van communicatie?

### 13.2 Change Management
- Hoe worden gebruikers voorbereid op de verandering?
- Welke trainingen worden gegeven?
- Hoe wordt weerstand aangepakt?
- Wie is verantwoordelijk voor change management?

---

## 14. Exit Strategie

### 14.1 Scenarioplanning
- Wat als het project niet succesvol is?
- Wanneer wordt besloten om te stoppen?
- Wat zijn de exit criteria?

### 14.2 Transitie
- Hoe wordt teruggevallen op de oude werkwijze indien nodig?
- Wat zijn de kosten van een exit?
- Hoe worden bestaande omgevingen beheerd bij een exit?

---

## 15. Samenvattende Vragen voor Executive Summary

- Wat is het probleem in één zin?
- Wat is de oplossing in één zin?
- Wat zijn de totale kosten?
- Wat zijn de totale baten?
- Wat is de ROI?
- Wat is de terugverdientijd?
- Waarom nu?
- Wat is het advies?

---

## Bijlagen Checklist

- [ ] Gedetailleerde kostenberekening
- [ ] Gedetailleerde batenberekening
- [ ] Risicoregister met mitigatiemaatregelen
- [ ] Projectplanning (Gantt chart)
- [ ] Technische architectuur documentatie
- [ ] Stakeholder mapping
- [ ] Vergelijking met alternatieven
- [ ] Letters of support van stakeholders
- [ ] Relevante benchmarks of case studies

---

*Document gegenereerd voor Operations Manager Business Case*