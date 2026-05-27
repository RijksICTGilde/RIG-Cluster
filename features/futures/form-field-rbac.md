# Form field-level RBAC (future refactor)

Status: **niet geïmplementeerd**. Vandaag past elke save-handler
zelf `require_project_edit_access` (rol-gate) toe via
`opi/web/project_edit_security.py`. Vergeet één save-pad en de
gate is daar afwezig.

Het `Editable`/`FormSection`-systeem
(`opi/forms/editables/editable.py`) heeft geen `requires_role`-
attribuut. Er is een `AdminEnforcer` voor business-rules ("≥1 admin
moet blijven bestaan") maar geen field-level "wie mag dit veld editen".

Wenselijke eindstaat:

- `requires_role: str | None` op `Editable` of `FormSection`
- Centrale `form_submission_guard` matcht request-user-role tegen
  field-metadata vóór de processor draait — niet-toegestane waardes
  leveren een `FieldError`
- Form-renderer disablet velden voor users zonder permission (UI-feedback
  in plaats van stille rejection achteraf)
- Eén bron van waarheid voor RBAC: definitie naast de field-definitie,
  automatisch geldend op alle save-paden
