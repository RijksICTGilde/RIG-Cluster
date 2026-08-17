# PostgreSQL Database

Een PostgreSQL-database voor je project. Standaard krijg je een eigen database op de gedeelde databaseserver van het platform, inclusief gebruiker, wachtwoord en backups. Je hoeft zelf niets te installeren of te beheren.

## Wanneer gebruik je dit?

- Je applicatie bewaart gegevens in een relationele database
- Je wilt geen database in je eigen container draaien
- Je wilt dat er backups van je gegevens gemaakt worden

## Gedeeld of een eigen cluster?

In de configuratie kies je bij **scope** tussen **shared** (een database op de gedeelde server, de standaard) en **project** (een eigen databasecluster voor je project). Kies een eigen cluster als je extensies, een eigen image of SUPERUSER-rechten nodig hebt, of gescheiden wilt staan van andere projecten. Dat is hetzelfde resultaat als de aparte service **Namespace PostgreSQL Database**. In alle andere gevallen is gedeeld eenvoudiger en zuiniger.

## Wat wordt er ingesteld?

Er worden een database, een gebruiker en een wachtwoord aangemaakt. Je component krijgt onder meer **DATABASE_SERVER_HOST**, **DATABASE_SERVER_PORT**, **DATABASE_SERVER_USER**, **DATABASE_PASSWORD** en **DATABASE_DB**. Er is ook een meelezende gebruiker (**DATABASE_SERVER_USER_RO**) voor rapportages of analyses.

Je kunt extra schema's binnen dezelfde database laten aanmaken. Bij het verwijderen van deze service wordt de database gemarkeerd voor uitgestelde verwijdering, zodat gegevens niet meteen weg zijn.
