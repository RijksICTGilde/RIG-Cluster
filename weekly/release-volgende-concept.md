#### ZAD release, nog te schrijven

Concept. Hernoem dit bestand naar `release-<datum>.md` zodra de releasedatum bekend is, en schrijf de gebruikersgerichte tekst zoals in de vorige releases.

##### Notitie: issues die bij deze release dicht mogen

- **#153** SOPS-secret lifecycle na component-prune. Gemerged als RC-202. De prune spaart de ciphertext van een secret dat dezelfde run opnieuw aanmaakt, en het oauth2-cookie-secret houdt zijn waarde, dus een deploy logt niemand meer uit.
- **#56** Generational failover creates zombie databases. Gemerged als RC-203. Een run die na een geslaagde kloon afbreekt, maakt de kloon bij de volgende run af in plaats van er een `_vN`-database naast te zetten. Let op bij het sluiten: bestaande `_vN`-databases zijn niet opgeruimd, dat is een losse actie.
