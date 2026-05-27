# Form field-level RBAC (future refactor)

Status: **niet geïmplementeerd**. Vandaag past elke save-handler
zelf `require_project_edit_access` (rol-gate) en
`merge_preserving_protected_keys` (allowlist) toe via
`opi/web/project_edit_security.py`. Vergeet één save-pad en de
bescherming is daar afwezig.

Het `Editable`/`FormSection`-systeem
(`opi/forms/editables/editable.py`) heeft geen `requires_role`-
attribuut. Er is een `AdminEnforcer` voor business-rules ("≥1 admin
moet blijven bestaan") maar geen field-level "wie mag dit veld editen".

Wenselijke eindstaat:

- `requires_role: str | None` op `Editable` of `FormSection`
- Centrale `form_submission_guard` matcht request-user-role tegen
  field-metadata vóór de processor draait — niet-toegestane waardes
  worden gedropt of leveren een `FieldError`
- Form-renderer disablet velden voor users zonder permission (UI-feedback
  in plaats van stille drop achteraf)
- `merge_preserving_protected_keys` kan dan verdwijnen: bescherming zit
  in validate-stage, automatisch op alle save-paden
