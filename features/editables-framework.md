# Editables Framework

Een declaratief form framework voor Python dat data-logica volledig scheidt van UI. Editables beschrijft velden als paden naar waarden in geneste datastructuren, met pluggable validatie, conversie, business rules en computed values.

## Kernidee: pad-gebaseerde velden

Elk veld is een **pad** naar een waarde in een geneste structuur. Het pad bepaalt waar de waarde leeft — niet hoe die wordt opgeslagen of getoond:

```python
Editable(yaml_path="display-name")
Editable(yaml_path="components[*]/name")
Editable(yaml_path="deployments[0]/base-domain")
Editable(yaml_path="config/age-public-key")
```

Paden ondersteunen geneste navigatie met wildcards (`[*]`) voor iteratie over lijsten, concrete indices (`[0]`), en dict-key filters (`services{keycloak}`). Het framework is storage-agnostisch — dezelfde padlogica werkt op YAML, JSON, ORM-objecten, of elke andere geneste structuur.

In de cross-object variant (TAD/AMT) fungeert het eerste segment als object-type referentie (`algorithm/{id}/system_card/name`), waardoor een enkel formulier meerdere objecten tegelijk kan beheren.

## Architectuur: drie lagen

```
Editable          →  EditableVisualizer    →  FormField     →  Widget
(data-logica)        (UI binding)             (resolved)       (HTML)
```

Elke laag heeft een helder eigen domein:

### Editable — wat het veld IS

Puur data. Geen labels, geen widgets, geen HTML. Extractable als standalone package.

```python
@dataclass
class Editable:
    yaml_path: str
    validator: EditableValidator | None = None
    converter: EditableConverter | None = None
    enforcer: EditableEnforcer | None = None
    generator: EditableGenerator | None = None
    values_provider: str | None = None
    required: bool = False
    default: Any = None
    children: list[Editable] | None = None
    min_items: int = 0
    max_items: int | None = None
    depends_on: str | None = None
    show_when: dict[str, Any] | None = None
    transient: bool = False
    defers_to: str | None = None
    defer_when: EditableCondition | None = None
    remove_when_none: bool = False
    virtualize: tuple[str, str] | None = None
    hooks: dict[str, Any] | None = None
    rename_targets: list[str] | None = None
```

### EditableVisualizer — hoe het veld ERUITZIET

Wraps een `Editable` en voegt alle UI-metadata toe:

```python
@dataclass
class EditableVisualizer:
    editable: Editable
    widget: WidgetType
    label: str
    description: str | None = None
    placeholder: str | None = None
    help_text: str | None = None
    readonly: bool = False
    readonly_on_edit: bool = False
    locked_by_service: str | None = None
    htmx_trigger: str | None = None
    htmx_target: str | None = None
    children: list[EditableVisualizer] | None = None
```

Dit maakt hetzelfde `Editable` herbruikbaar met verschillende widgets — tekstveld in een wizard, readonly op een detailpagina, verborgen in een API-context.

### FormField — resolved en klaar voor rendering

De bridge-laag lost het pad op tegen de actuele data, past converters toe, evalueert condities, en levert een volledig resolved veld op dat de template direct kan renderen.

## Een formulier opbouwen

### Stap 1: Editables definiëren (data)

```python
from opi.forms.editables.editable import Editable
from opi.forms.editables.validators import SlugValidator, MinMaxLengthValidator
from opi.forms.editables.converters import EmptyToNoneConverter

PROJECT_NAME = Editable(
    yaml_path="display-name",
    validator=SlugValidator(),
    required=True,
)

DESCRIPTION = Editable(
    yaml_path="description",
    validator=MinMaxLengthValidator(max_length=500),
    remove_when_none=True,
)

BASE_DOMAIN = Editable(
    yaml_path="deployments[0]/base-domain",
    converter=CustomDomainSelectConverter(),
    defers_to="deployments[0]/base-domain",
    defer_when=SentinelValueCondition("__custom__"),
)
```

### Stap 2: Visualizers definiëren (UI)

```python
from opi.forms.visualizers.visualizer import EditableVisualizer
from opi.forms.editables.editable import WidgetType

PROJECT_NAME_VIS = EditableVisualizer(
    editable=PROJECT_NAME,
    widget=WidgetType.TEXT,
    label="Projectnaam",
    description="Technische naam, wordt gebruikt in URLs en namespaces",
    placeholder="mijn-project",
    readonly_on_edit=True,
)

DESCRIPTION_VIS = EditableVisualizer(
    editable=DESCRIPTION,
    widget=WidgetType.TEXTAREA,
    label="Omschrijving",
)

BASE_DOMAIN_VIS = EditableVisualizer(
    editable=BASE_DOMAIN,
    widget=WidgetType.SELECT,
    label="Basisdomein",
    values_provider="supported_domains",
)
```

### Stap 3: Groeperen in secties

```python
from opi.forms.visualizers.sections import FormSection

IDENTITY_SECTION = FormSection(
    section_id="identity",
    title="Basisgegevens",
    icon="huis",
    editables=[PROJECT_NAME_VIS, DESCRIPTION_VIS],
    enforcer=UniqueDeploymentNameEnforcer(),
    post_save_action="save_only",
)
```

### Stap 4: Combineren in een flow

```python
from opi.forms.visualizers.flows import FormFlow, FlowMode

CREATE_FLOW = FormFlow(
    flow_id="create-project",
    title="Nieuw project aanmaken",
    mode=FlowMode.WIZARD,
    sections=[IDENTITY_SECTION, SERVICES_SECTION, TEAM_SECTION, ...],
    show_review=True,
    save_per_section=True,
    generated_editables=[...],  # computed values, niet getoond in UI
)
```

## Pluggable componenten

Vijf protocollen haken in op een Editable. Allemaal gedefinieerd als Python `Protocol` classes — implementeer de juiste methode(s) en je bent klaar.

### Validator — inputregels

Retourneert een lege lijst bij succes, foutmeldingen bij falen. Puur synchroon, geen side effects.

```python
class EditableValidator(Protocol):
    def validate(self, value: Any) -> list[str]: ...
```

```python
class SlugValidator:
    def validate(self, value: Any) -> list[str]:
        if not re.match(r"^[a-z][a-z0-9-]*$", str(value)):
            return ["Moet beginnen met een kleine letter, alleen kleine letters, cijfers en streepjes"]
        return []

class MemoryRangeValidator:
    def __init__(self, min_mi: int = 32, max_mi: int | None = None): ...

    def validate(self, value: Any) -> list[str]:
        mi = parse_k8s_memory_to_mi(str(value))
        if mi < self.min_mi or mi > self.max_mi:
            return [f"Geheugen moet tussen {self.min_mi}Mi en {self.max_mi}Mi liggen"]
        return []
```

### Enforcer — business rules en cross-field validatie

Opereert op section-niveau: ontvangt de hele sectie-data plus context. Gooit `ValueError` bij falen, of `FieldError` voor per-veld foutmeldingen.

```python
class EditableEnforcer(Protocol):
    def enforce(self, value: Any, context: dict[str, Any]) -> Any: ...
```

```python
class AdminRequiredEnforcer:
    async def enforce(self, value: Any, context: dict[str, Any]) -> Any:
        has_admin = any(user.get("role") == "admin" for user in value)
        if not has_admin:
            raise ValueError("Er moet minimaal een administrator zijn")
        return value

class DomainConfigEnforcer:
    async def enforce(self, value: Any, context: dict[str, Any]) -> Any:
        # Cross-field: check subdomain + base-domain + domain-format combinatie
        # Async: controleert beschikbaarheid in database
        ...
```

Het verschil met validators: validators checken de waarde van een enkel veld, enforcers checken relaties tussen velden en externe regels (is dit subdomein beschikbaar? Mag deze gebruiker dit?).

### Converter — waarde-transformatie in 3 fasen

Converters vertalen waarden tussen opslag, formulier-invoer en weergave:

```python
class EditableConverter(Protocol):
    def read(self, value: Any) -> Any: ...   # opslag → formulier
    def write(self, value: Any) -> Any: ...  # formulier → opslag
    def view(self, value: Any) -> Any: ...   # opslag → leesweergave
```

| Fase | Richting | Voorbeeld |
|------|----------|-----------|
| `read()` | Opslag → formulier | `["staging"]` → `"staging"` (list → select value) |
| `write()` | Formulier → opslag | `"staging"` → `{"type": "deployment", "reference": "staging", "mode": "once"}` |
| `view()` | Opslag → display | `{"reference": "staging", "status": {"completed": true}}` → `"Gekloond van staging - Voltooid"` |

Voorbeelden uit de codebase:

```python
class AGEEncryptConverter:
    """Encrypt/decrypt met AGE keys."""
    def read(self, value):   # decrypt voor bewerking
    def write(self, value):  # encrypt voor opslag
    def view(self, value):   # toont "********"

class KeyValueConverter:
    """KEY=value tekst ↔ dict/string opslag, met AGE encrypt/decrypt."""

class CloneFromConverter:
    """Dropdown-waarde ↔ clone-from dict structuur."""

class EmptyToNoneConverter:
    """Lege string → None, zodat YAML-key wordt weggelaten."""
```

### Generator — computed values bij submit

Generators worden niet gerenderd in formulieren. Ze berekenen waarden uit de samengestelde data bij het opslaan.

```python
class EditableGenerator(Protocol):
    def generate(self, yaml_data: dict[str, Any]) -> Any: ...
```

```python
class ProjectNameGenerator:
    def generate(self, yaml_data):
        return generate_project_name(yaml_data["display-name"])

class AGEKeyPairGenerator:
    def generate(self, yaml_data):
        private_key, public_key = generate_sops_key_pair()
        # Stash private key voor EncryptedPrivateKeyGenerator
        yaml_data.setdefault("_generated", {})["age-private-key-plain"] = private_key
        return public_key

class EncryptedPrivateKeyGenerator:
    def generate(self, yaml_data):
        plain = yaml_data["_generated"]["age-private-key-plain"]
        return encrypt_age_content_sync(plain, settings.SOPS_AGE_PUBLIC_KEY)
```

Generators draaien in volgorde, waardoor latere generators afhankelijk kunnen zijn van eerdere resultaten.

### Condition — conditioneel veldgedrag

Bepaalt wanneer een veld zich "terugtrekt" ten gunste van een ander veld (deferred fields):

```python
class EditableCondition(Protocol):
    def check(self, value: Any) -> bool: ...
```

```python
class SentinelValueCondition:
    """True wanneer de waarde een sentinel is (bijv. '__custom__')."""
    def __init__(self, sentinel: str = "__custom__"): ...
    def check(self, value): return str(value) == self.sentinel
```

Dit maakt het "select met eigen invoer" patroon mogelijk: een select-veld met een `__custom__` optie die, indien gekozen, de waarde doorgeeft aan een transient tekstveld.

## Geavanceerde patronen

### Conditionele zichtbaarheid

Velden kunnen afhankelijk zijn van de waarde van een ander veld:

```python
Editable(
    yaml_path="config/keycloak/realm",
    depends_on="services",
    show_when={"contains": "keycloak"},
)
```

### Transient fields en deferral

Tijdelijke velden die niet worden opgeslagen maar wel in het formulier verschijnen:

```python
CUSTOM_DOMAIN_TEXT = Editable(
    yaml_path="deployments[0]/base-domain:custom",
    transient=True,
    validator=CustomDomainValidator(),
)

BASE_DOMAIN_SELECT = Editable(
    yaml_path="deployments[0]/base-domain",
    defers_to="deployments[0]/base-domain",
    defer_when=SentinelValueCondition("__custom__"),
)
```

Wanneer de select op `__custom__` staat, neemt het transient tekstveld het over als bron van de uiteindelijke waarde.

### Virtualization

Voorkomt pad-collisions wanneer meerdere velden dezelfde YAML-prefix delen:

```python
Editable(
    yaml_path="services",
    virtualize=("services", "_services-config"),
)
```

Het formulier gebruikt `_services-config` als HTML field name, de processor leest van het virtuele pad en schrijft naar het echte pad.

### Rename propagation

Wanneer een veld hernoemd wordt (bijv. een componentnaam), kunnen verwijzingen elders automatisch mee-updaten:

```python
Editable(
    yaml_path="components[*]/name",
    rename_targets=[
        "deployments[*]/components[*]/reference",
        "components[*]/uses-components",
    ],
)
```

## Processing pipeline

Bij het verwerken van een form-submit doorloopt de `EditableFormProcessor` deze stappen:

```
Form submit (JSON)
    ↓
1. Parse submitted data (respecteer virtualize mappings)
2. Validate per veld (required + custom validators)
3. Convert per veld (converter.write())
4. Schrijf naar result data (set_value)
    ↓
5. Clear verborgen velden (depends_on conditie niet vervuld)
6. Draai generators (computed values, in volgorde)
7. Resolve deferrals (transient → parent)
8. Draai section enforcers (cross-field validatie)
9. Propagate renames (als veld hernoemd is)
10. Strip transients (tijdelijke velden verwijderen)
    ↓
Result data → opslaan
```

## Pad-gebaseerde autorisatie (toekomst)

Het pad-systeem biedt een natuurlijke basis voor field-level autorisatie. Omdat elk veld een uniek pad heeft, kunnen rechten declaratief op paden worden gekoppeld:

```
permission("display-name",              role=ADMIN,   access=EDIT)
permission("components[*]/*",           role=EDITOR,  access=EDIT)
permission("config/age-*",              role=VIEWER,  access=VIEW)
permission("deployments[*]/base-domain", role=VIEWER,  access=NONE)
```

Dit maakt het mogelijk om:
- **Per veld** te bepalen wie mag zien en wie mag bewerken
- **Wildcard-matching** op paden (`components[*]/*` = alle component-velden)
- **Form rendering** automatisch aan te passen: geen leesrecht → veld niet getoond, geen schrijfrecht → readonly

In TAD/AMT bestaat dit deels al via enforcers die autorisatie per veld checken. De stap naar een declaratief model maakt dit configureerbaar in plaats van hard-coded per enforcer.

## Vergelijking met bestaande frameworks

| | Django Forms | Pydantic | Editables |
|---|---|---|---|
| **Scope** | 1 model per form | 1 model per schema | N objecten per form (cross-object) |
| **Storage** | ORM (1 tabel) | Dict/JSON | Pluggable (ORM, JSON, YAML) |
| **Nesting** | Formsets (beperkt) | Nested models | Pad-navigatie met wildcards |
| **Lifecycle** | `save()` | Geen | Processing pipeline met 10 stappen |
| **Conversie** | Widget ↔ Python | Python ↔ JSON | 3-fase: read/write/view |
| **Validatie** | Field + `clean()` | Type + `@validator` | Validator (veld) + Enforcer (cross-field/authz) |
| **Computed** | `save()` overrides | `computed_field` | Generators (geordend, afhankelijk) |
| **Autorisatie** | Nee | Nee | Enforcer per veld + pad-based rechten (toekomst) |
| **UI binding** | Verweven (Widget) | Geen | Gescheiden (Editable → Visualizer → FormField → Widget) |
