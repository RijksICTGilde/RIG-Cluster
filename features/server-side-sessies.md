# Server-side sessies met een begrensde hot-cache

## Wat het is

De sessie van het ZAD-portaal woont in rig-db, niet in de cookie. De sessiecookie draagt
alleen een ondertekend sessie-id (itsdangerous, zoals voorheen): klein, onveranderlijk in
grootte, ongeacht wat er in de sessie staat.

Voorheen droeg de cookie de hele inhoud, en elke server-side toevoeging maakte hem
zwaarder. Op 27-10-2026 liep de OTP-registratie erop stuk: de cookie droeg de id_token
PLUS de afdruk van de lopende registratie en schoof over de ~4 KB die een browser
geruisloos laat vallen (eerst opgelost in PR 370, die alleen de registratie verhuisde).
Nu staat alles achter het id.

`request.session` gedraagt zich als bij Starlette's SessionMiddleware: een dict.
Bestaande code (login, uitloggen, wizardstate, step-up, OTP) merkt niet waar de sessie
woont.

## Hoe het werkt

```
cookie: sign(session_id)              <- alleen "welke sessie"
        |
ServerSideSessionMiddleware           <- opi/middleware/server_side_session.py
        |  request.session: dict-achtig, write-through bij elke mutatie
        v
SessionStore                          <- opi/services/session_store.py
        |  read: cache -> tabel -> cache vullen
        |  write: cache + tabel tegelijk (write-through)
        v    +--------------------------------------------+
SessionCache (LRU + TTL)              |  tabel `sessions`  |
  opi/core/session_cache.py           |  (migratie 011)    |
  max SESSION_CACHE_MAX_ENTRIES       +--------------------+
  TTL SESSION_CACHE_TTL_SECONDS
```

- **Elke request leest van de cache**; de database alleen bij een miss. De cache is een
  LRU met twee grenzen: maximaal `SESSION_CACHE_MAX_ENTRIES` (standaard 5000) items, en
  een per-item TTL (`SESSION_CACHE_TTL_SECONDS`, standaard 60 seconden). Een verlopen item
  komt nooit uit de cache; een volle cache zakt de minst-recent-aangeraakte eruit. Na de
  TTL is de database weer de bron, dus een tweede OPI-pod loopt nooit verder achter dan
  die minuut.
- **Writes zijn write-through**: een mutatie vult de cache én upsert de rij in dezelfde
  adem, zodat een volgende lezer de wijziging direct ziet, ook halverwege het request.
  De middleware wacht lopende schrijfacties af voordat het antwoord vertrekt.
- **Statische paden en probes lezen de sessie helemaal niet** (dezelfde uitsluitlijst als
  de autorisatie-middleware: `/health`, `/ready`, `/version`, `/static/`, `/metrics`):
  geen cache- of databaseverkeer voor iets dat geen gebruikerscontext heeft.
- **Uitloggen en intrekken verwijderen de rij** (naast de revocatiepost uit
  `session_revocations`): een weggegooide of ingetrokken sessie is op de volgende request
  een harde weigering, niet iets dat zijn cookie nog kan uitdienen.
- **Inloggen roteert het sessie-id** (`regenerate` in `auth_callback`): een id dat een
  aanvaller via cookie tossing in je browser plantte is dood zodra de identiteit in de
  sessie landt, en de browser krijgt een nieuwe cookie.

## Levensduur

De grenzen uit `features/portaalsessie-levensduur.md` gelden onverkort:

- **inactiviteit** (`SESSION_MAX_AGE_SECONDS`, 8 uur): de ondertekening van de cookie
  draagt een tijdstempel die bij elke mutatie ververst wordt, en de middleware weigert een
  stempel die ouder is dan het venster; `expires_at` op de rij schuift mee bij activiteit
  (`touch` bij een gelezen sessie). Wie niets doet, vervalt. De cookie kent dat venster
  zelf niet: hij is een sessiecookie en verdwijnt als de browser sluit
  (`features/portaalsessie-levensduur.md`).
- **absolute levensduur** (`SESSION_ABSOLUTE_MAX_SECONDS`, 12 uur): zit in de data zelf
  (`user.login_at`), gehandhaafd door dezelfde guard als voorheen.

De uurlijkse lus van de taakwerker ruimt verlopen rijen op (`purge_expired`), naast de
bestaande `cleanup_old_tasks` en `purge_expired_pending_registrations`.

## Meting

De store logt bij activiteit hooguit eens per minuut een INFO-regel met hits, misses,
hit-ratio, het aantal sets, verdringingen en de cachevulling. Op de database zelf is
`count()` de snelle vraag "hoeveel sessies staan er".

## Upgrade vanaf een eerdere versie

De tabel komt met migratie 011. Wie op dat moment is ingelogd heeft een cookie volgens de
oude vorm (inhoud in plaats van id): die leest als een onbekend id, wat een lege sessie
betekent. Iedereen logt dus eenmalig opnieuw in; dual-read is er bewust niet.

## Waar zit wat

| wat | waar |
|---|---|
| middleware + `request.session`-dict | `opi/middleware/server_side_session.py` |
| store (cache + tabel, metingen, purge) | `opi/services/session_store.py` |
| begrensde LRU-cache met TTL | `opi/core/session_cache.py` |
| ORM-model | `opi/services/persistence/sessions.py` |
| migratie 011 | `opi/migrations/versions/011_add_sessions.py` |
| guard op levensduur en intrekken | `opi/core/session_guard.py` (ongewijzigd) |
