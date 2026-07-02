# Attachments en koppelingen: vervolg op gebruikersfeedback

Bron: feedback van Eric Wout van der Steen (30 juni 2026), bij het vervangen van FSC-certificaten
in productie. Dit zijn de vervolgacties. Een deel is al opgelost maar nog niet gedeployed.

## Al opgelost, verifiëren na deploy

- **Verwijderen geeft een foutmelding maar lukt wel** (na reload is de bijlage weg). De delete is
  idempotent gemaakt (een al verwijderde bijlage geeft succes i.p.v. een 404) en leest nu vers uit
  Git. Commits `bca13b31` + `bf5ceb1e`. Na deploy hoort de verwarrende foutmelding weg te zijn.
- **Projectpagina ziet een ontkoppeling niet zonder reload** (denkt dat de bijlage nog in gebruik is,
  dus verwijderen kan niet, tot je herlaadt). Hoort te verdwijnen met de cache-refresh-na-mutatie uit
  `features/futures/project-file-single-path-consolidation.md`. Verifiëren na deploy; zo niet, dan een
  expliciete cache-refresh op de ontkoppel-actie.

## Nieuw: functionaliteit

- **Een gebruikt certificaat vervangen is te omslachtig.** Nu moet je het overal ontkoppelen, dan
  verwijderen, dan het nieuwe uploaden, dan overal opnieuw koppelen. Wens: een "vervang inhoud"-actie
  op een bestaande bijlage (zelfde id, koppelingen blijven staan, alleen de inhoud wisselt). Dan hoeft
  er niets ontkoppeld en herkoppeld te worden.

## Nieuw: validatie bij invoer

- **Dubbel mount-pad wordt pas bij de deployment gemeld.** Twee bijlagen op hetzelfde pad koppelen zou
  al bij het invoeren gevalideerd moeten worden, niet pas bij verwerken. Nu vangt
  `_assert_unique_attachment_targets` (`project_file_handler`) het pas bij resolve/deploy af. Voeg een
  input-tijd-validator toe op de koppeling: uniek `path` en uniek `env-name` per component (en per
  deployment-component).

## Nieuw: UX

- **Meerdere edits groeperen i.p.v. elke wijziging direct uitrollen.** Elke koppel- of ontkoppel-actie
  triggert nu een aparte wijziging naar ArgoCD, wat bij het vervangen van een certificaat een lange
  reeks deploys oplevert. Overweeg wijzigingen te bundelen tot één opslag en één verwerking (meerdere
  edits stagen, dan in één keer doorvoeren).
- **Scrollpositie behouden.** Na het verwijderen van een certificaat uit een component springt de view
  naar onderen, waardoor je je plek kwijt bent. Behoud de scrollpositie (of scroll terug naar de
  bewerkte rij) na de her-render.
- **Geen click-outside-to-close bij grote of complexe modals met wijzigingen.** Per ongeluk naast het
  dialoog klikken sluit de modal en kan ingevoerde wijzigingen weggooien. Schakel click-outside-close
  uit, of vraag bevestiging, voor modals met niet-opgeslagen wijzigingen.
