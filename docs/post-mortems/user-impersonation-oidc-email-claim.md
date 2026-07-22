# Post-mortem en root cause analyse: gebruiker-imitatie via de OIDC-integratie

**Datum:** 2026-07-20
**Status:** In behandeling (containment gedaan, herstel doorgevoerd)
**Ernst:** Hoog tot zeer hoog (impact-plafond: volledige overname)
**Melder:** een interne collega
**Betrokken systemen:** Wies, ZAD

> Dit document dient twee doelen: een interne root cause analyse en de basis voor een
> security advisory in `RijksICTGilde/wies/security/advisories/`. Het is bewust eerlijk
> over wat we gemist hebben. Nog uit te lijnen op het interne post-mortem-sjabloon.

---

## Samenvatting (TL;DR)

Wies en ZAD bepaalden de identiteit en autorisatie van een gebruiker op basis van de e-mailclaim uit het OIDC-token. Dat e-mailadres was door een ingelogde gebruiker via een omweg zelf te wijzigen via de Keycloak-account-API, en er werd door de applicaties niet gecontroleerd of het adres geverifieerd was.

Een gebruiker die kon inloggen via SSO-Rijk kon daardoor zijn e-mailadres naar dat van een ander zetten en zo als die persoon inloggen. Dezelfde ontbrekende verified-controle zat ook in de autorisatie-proxy (de authorization wall) die voor twee andere applicaties staat.

De kern van onze fout is tweeledig. We kozen e-mail als identiteitssleutel in de veronderstelling dat die voor de gebruiker onwijzigbaar was. Gebruikers kunnen zelf geen accounts aanmaken voor deze applicaties, gegevens worden overgenomen van SSO Rijk of door een beheerder ingevoerd.

Die aanname was de denkfout: het adres was wel te wijzigen, niet via de UI maar via de Keycloak-account-API. Belangrijker, we misten de `email_verified`-controle die een gewijzigd, niet-geverifieerd adres had moeten weigeren. Die controle ontbrak zowel in Wies als in ZAD. E-mail als sleutel was daarmee niet inherent fout: e-mail plus de verified-controle was een valide oplossing geweest.

Na het duidelijk worden van het echte probleem is het beveiligingslek binnen een half uur in beiden applicaties gedicht.

---

## Ernst en impact

- **Wat een aanvaller kon bereiken:** zich voordoen als elke willekeurig persoon
- **Wie het kon uitvoeren:** iedereen die kon inloggen via SSO-Rijk
- **Praktische drempel:** niet triviaal. Het wijzigen van het e-mailadres vergde meerdere API-calls, en het werkt alleen voor een reeds via SSO-Rijk geauthenticeerde gebruiker. Er is geen anoniem, extern aanvalspad.
- **Voorgestelde CVSS v3.1 (te finaliseren door het team):** `AV:N/AC:H/PR:L/UI:N/S:U/C:H/I:H/A:N`. Impact hoog (vertrouwelijkheid en integriteit), maar hoge complexiteit en vereiste (lage) privileges. De melder classificeerde de bevinding als "Zeer hoog" gezien het impact-plafond (beheerder) en de brede kring die via SSO-Rijk kan inloggen.

---

## Tijdlijn

Tijden waar exact bekend; overige met [in te vullen] voor het team.

| Tijdstip                         | Gebeurtenis                                                                                                                                                                                              |
|----------------------------------|----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| Zo 2026-07-19, 16:00             | Melding gedaan. Op dat moment was de precieze aard van het probleem nog niet met zekerheid duidelijk.                                                                                                    |
| Zo 2026-07-19, ~21:00 tot 22:00  | Bevinding besproken en als urgent gekwalificeerd ("werk aan de winkel"). De algemene aard (imitatie via de identity-provider-flow) was bekend, maar de precieze toegangsstappen waren nog niet ontvangen. |
| Ma 2026-07-20, 11:30             | Precieze toegangsstappen ontvangen; het exacte probleem is nu duidelijk.                                                                                                                                 |
| Ma 2026-07-20, ~12:00 (+~30 min) | Betrokken applicaties aangepast                                                                                                                                                            |
| Ma 2026-07-20                    | Fixes doorgevoerd in Wies (PR #487) en ZAD                                                                                                                                                               |
| [loopt]                          | Controle toegang tot Wies/ZAD/Keycloak wijzigingen                                                                                                                                                       |

---

## De kwetsbaarheid (technisch)

De OIDC-keten loopt van SSO-Rijk (BZK) via een gedeelde `rig-platform` Keycloak-realm naar per-project realms, en van daaruit naar de applicaties. Applicaties ontvangen een OIDC-token en leidden daaruit de identiteit af.

Drie dingen kwamen samen:

1. **E-mail als identiteitssleutel.** Wies en ZAD gebruikten enkel het e-mailadres uit het token
2. **E-mail was wijzigbaar door de gebruiker.** Via de Keycloak-account-API kon een ingelogde gebruiker zijn eigen e-mailadres aanpassen. Het veld was niet vergrendeld.
3. **Geen `email_verified`-controle.** Noch Wies noch ZAD controleerde of het e-mailadres geverifieerd was. Daardoor werd een gewijzigd, niet-geverifieerd adres net zo vertrouwd als een echt door SSO-Rijk geleverd adres.

Gevolg: een gebruiker zette zijn e-mailadres op dat van een andere persoon en werd door de applicatie als die persoon behandeld.

---

## Root cause analyse

### 1. We kozen e-mail als sleutel, in de veronderstelling dat die niet wijzigbaar was

De e-mailclaim voelde als een praktische identiteitssleutel: hij is stabiel als een Keycloak-account opnieuw wordt aangemaakt (de `sub` verandert dan wel), en hij werkt voor zowel federatieve als lokale accounts. De doorslaggevende overweging was echter een aanname: dat het e-mailadres voor de gebruiker onwijzigbaar was. In Wies en ZAD kan een gebruiker zichzelf niet registreren en zelf geen gegevens of accounts aanmaken; het adres wordt door ons of door SSO-Rijk gezet. Op basis daarvan gingen we ervan uit dat de gebruiker de sleutel niet zelf kon aanraken.

### 2. Die aanname was de denkfout: e-mail is wel wijzigbaar, via de API

Via de Keycloak-account-API kon een ingelogde gebruiker zijn eigen e-mailadres wel degelijk wijzigen. Dat is precies wat de exploit deed. Voor Wies en ZAD was dit ook het enige aanvalspad (zelf een lokaal account aanmaken kan daar niet). Bovendien was het e-mailveld in Keycloak niet als vergrendeld (read-only) gemarkeerd; had dat wel zo gestaan, dan was ook de wijziging via de API geweigerd. Dat is dus een aparte preventie die ontbrak.

### 3. De ontbrekende `email_verified`-controle is de pijnlijke kern

Ook als de sleutel wijzigbaar is, had de `email_verified`-controle dit moeten opvangen: een zelf-gewijzigd adres is niet geverifieerd, dus die controle had het gerealiseerde aanvalspad gewoon geweigerd. Die controle ontbrak in Wies, in ZAD, en in de autorisatie-proxy die op hetzelfde mechanisme leunt. Dit is waar het echt fout ging, en ook waar het het snelst te dichten was.

E-mail als sleutel is niet inherent fout. Zowel een `email_verified`-controle als binden aan de stabiele `sub` is een goede oplossing, en beide rusten op iets dat de aanvaller niet kan vervalsen: een `sub` is niet te raden, en de e-mailclaim kun je wel wijzigen maar de `verified`-vlag niet. De kern was dus niet email-versus-`sub`, maar dat we geen van beide controleerden.

### 4. Waarom zowel Wies als ZAD het misten

De verified-controle ontbrak in Wies, in ZAD en in de door ZAD gegenereerde autorisatie-proxy, omdat het deels hetzelfde team betreft, met dezelfde mentale aanname: "de e-mail komt van SSO-Rijk, dus die klopt". Er was geen onafhankelijke controle die deze gedeelde blinde vlek ving. Dit is de systemische oorzaak: een gedeelde aanname zonder afdwingende maatregel en zonder onafhankelijke review werkt door in alle systemen die het team bouwt.

---

## Wat ging goed (detectie en respons)

- De bevinding is gevonden door een collega, niet door misbruik.
- De fix is binnen afzienbare tijd doorgevoerd.

---

## Genomen maatregelen

Status per maatregel; deel is uitgerold, deel staat klaar en wordt gefaseerd uitgerold.

**Wies**
- Identiteit gebonden aan de stabiele `sub` in plaats van aan het e-mailadres.
- Het e-mailadres wordt alleen nog eenmalig bij de eerste login gebruikt om een vooraf aangemaakt account te koppelen, en alleen zolang dat nog ongebonden is.
- `email_verified` wordt op alle paden afgedwongen.

**ZAD**
- `email_verified`-controle bij login; niet-geverifieerde e-mailadressen krijgen geen toegang
- Identiteitsvelden (`email`, `firstName`, `lastName`) worden onwijzigbaar gemaakt voor gebruikers in het declaratieve user-profile van elke realm (op het centrale realm-aanmaakpunt, dus voor alle realms bij aanmaak of reprocess). IdP-mappers en beheerders behouden schrijfrecht. Let op: dit maakt onze oorspronkelijke aanname (e-mail is onwijzigbaar) alsnog waar en sluit het gerealiseerde pad, maar het leunt op realm-configuratie en dekt realms met lokale accounts niet. De dragende, overdraagbare controle zit in de applicatie: de `email_verified`-controle en/of `sub`-binding met deny-on-conflict (zie root cause 3).
- De autorisatie-proxy (oauth2-proxy) eist voortaan een geverifieerd e-mailadres (de onveilige `--insecure-oidc-allow-unverified-email` staat expliciet op `false`).

---

## Geleerde lessen

1. **Vertrouw een gebruiker-beinvloedbare claim nooit zonder controle.** E-mail als sleutel mag, mits je het vertrouwenssignaal (`email_verified`) controleert; binden aan de stabiele, per-account unieke `sub` met deny-on-conflict is een gelijkwaardig alternatief. De valkuil was aannemen dat de claim onwijzigbaar was zonder dat af te dwingen.
2. **Dwing invarianten af in code en configuratie, niet via aanname.** "E-mail komt van SSO-Rijk dus die klopt" was waar in de praktijk, maar nergens afgedwongen; precies daar zat het gat.
3. **Een gedeeld team deelt blinde vlekken.** Dezelfde impliciete aanname zat in Wies en ZAD. Onafhankelijke security-review en defense-in-depth over de hele keten zijn nodig, niet een controle op een enkel punt.
4. **Vertrouwen is transitief.** Elke applicatie erft het vertrouwen van de realm erboven. De keten is zo sterk als de zwakste schakel; hardening moet op elke schakel, niet alleen bovenaan.
5. **Security-audit** Security issues als deze hadden met een expliciete audit vooraf gevonden kunnen worden.

---
