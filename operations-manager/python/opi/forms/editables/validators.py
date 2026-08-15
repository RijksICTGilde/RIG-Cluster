from __future__ import annotations

import re
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import TypeAdapter, ValidationError

from opi.forms.editables.converters import command_line_has_unbalanced_quote, split_command_line
from opi.utils.naming import SCHEMA_POSTFIX_MAX_LENGTH, SCHEMA_POSTFIX_PATTERN

if TYPE_CHECKING:
    from pydantic import BaseModel


class ModelFieldValidator:
    """Validate a form field with the pydantic field that ALREADY defines the rule.

    Where a service has a config model, that model is the contract the API writes against and
    the stored project file is validated against. A hand-written validator next to it is a
    second definition of the same rule, and the two drift: the cross-domain peer fields
    restated "DNS-1123 label" as ``KubernetesNameValidator``, which also demands a leading
    LETTER -- so the form rejected a peer whose name the schema, the API and the project store
    all accept. Same shape as ``opi/api/validation.py``'s reuse of shared editables, one layer
    down: point at the definition instead of copying it.

    The pydantic message stays out of the UI on purpose -- it is English and speaks about
    types -- so the caller supplies the human explanation; only the RULE is shared.
    """

    def __init__(self, model: type[BaseModel], field_name: str, message: str) -> None:
        field = model.model_fields[field_name]
        # A field carries its constraints in ``metadata`` only when they sit directly on
        # the field. On an OPTIONAL field they sit inside the union member instead
        # (``MailLocalPart | None``), so ``metadata`` is empty and ``Annotated[(X,)]`` --
        # one argument -- is a TypeError at import time. The annotation alone already
        # carries the rule in that case, so use it as-is.
        annotation = Annotated[(field.annotation, *field.metadata)] if field.metadata else field.annotation
        self._adapter: TypeAdapter[Any] = TypeAdapter(annotation)
        self._message = message

    def validate(self, value: Any, context: dict[str, Any] | None = None) -> list[str]:
        # Emptiness is ``required``'s business, not the field rule's.
        if value is None or value == "":
            return []
        try:
            self._adapter.validate_python(value)
        except ValidationError:
            return [self._message]
        return []


class SlugValidator:
    """
    Validates slug format: starts with letter, only lowercase letters, digits, hyphens.

    Pattern: ^[a-z][a-z0-9-]*$
    """

    def validate(self, value: Any) -> list[str]:
        if not value:
            return []  # Let RequiredValidator handle emptiness
        value_str = str(value)
        if not re.match(r"^[a-z][a-z0-9-]*$", value_str):
            return ["Moet beginnen met een kleine letter en mag alleen kleine letters, cijfers en streepjes bevatten"]
        return []


class EmailValidator:
    """Validates basic email format."""

    def validate(self, value: Any) -> list[str]:
        if not value:
            return []  # Let RequiredValidator handle emptiness
        value_str = str(value)
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", value_str):
            return ["Geen geldig e-mailadres"]
        return []


class MinMaxLengthValidator:
    """Validates minimum and/or maximum string length."""

    def __init__(
        self,
        min_length: int | None = None,
        max_length: int | None = None,
    ) -> None:
        self.min_length = min_length
        self.max_length = max_length

    def validate(self, value: Any) -> list[str]:
        if not value:
            return []  # Let RequiredValidator handle emptiness
        value_str = str(value)
        errors: list[str] = []
        if self.min_length is not None and len(value_str) < self.min_length:
            errors.append(f"Moet minimaal {self.min_length} tekens bevatten")
        if self.max_length is not None and len(value_str) > self.max_length:
            errors.append(f"Mag maximaal {self.max_length} tekens bevatten")
        return errors


class RangeValidator:
    """Validates that a numeric value falls within a specified range."""

    def __init__(
        self,
        min_value: int | float | None = None,
        max_value: int | float | None = None,
    ) -> None:
        self.min_value = min_value
        self.max_value = max_value

    def validate(self, value: Any) -> list[str]:
        if value is None or value == "":
            return []
        try:
            num = float(value)
        except ValueError, TypeError:
            return ["Moet een geldig getal zijn"]
        errors: list[str] = []
        if self.min_value is not None and num < self.min_value:
            errors.append(f"Moet minimaal {self.min_value} zijn")
        if self.max_value is not None and num > self.max_value:
            errors.append(f"Mag maximaal {self.max_value} zijn")
        return errors


class ComponentNameValidator:
    """
    Validates component names, identical to deployment names (``KubernetesNameValidator``)
    and the ``component.name`` schema: start with a lowercase letter, then lowercase
    letters, digits and hyphens, end alphanumeric (no leading or trailing hyphen, no
    uppercase), max 63 chars. A leading letter is required because Kubernetes RFC 1035
    names (e.g. Services) must start with a letter and an all-digit name would parse
    as a YAML integer.

    When called with context containing ``existing_component_names``,
    also checks uniqueness.
    """

    def validate(self, value: Any, context: dict[str, Any] | None = None) -> list[str]:
        if not value:
            return []
        value_str = str(value)
        if len(value_str) > 63:
            return ["Componentnaam mag maximaal 63 tekens bevatten"]
        if not re.match(r"^[a-z]([-a-z0-9]*[a-z0-9])?$", value_str):
            return [
                "Moet beginnen met een kleine letter, mag kleine letters, cijfers en streepjes "
                "bevatten, en moet eindigen op een letter of cijfer"
            ]
        if context and value_str in context.get("existing_component_names", []):
            return [f"Er bestaat al een component met de naam '{value_str}'"]
        return []


class KubernetesNameValidator:
    """Validates a name that becomes part of Kubernetes resource names (deployment, PVC, ...).

    Must start with a lowercase letter (so it is never all-digits, nor parsed as a YAML int),
    then lowercase letters, digits and hyphens, ending alphanumeric, within ``max_length``.
    ``label`` names the field in the message; the wizard fields are free text, so the message
    spells out the common mistakes (spaces, capitals). Uniqueness, where relevant, is
    enforced elsewhere (e.g. UniqueDeploymentNameEnforcer).
    """

    def __init__(self, label: str = "Naam", max_length: int = 63) -> None:
        self._label = label
        self._max_length = max_length

    def validate(self, value: Any, context: dict[str, Any] | None = None) -> list[str]:
        if not value:
            return []
        value_str = str(value)
        if len(value_str) > self._max_length:
            return [f"{self._label} mag maximaal {self._max_length} tekens bevatten"]
        if not re.match(r"^[a-z]([-a-z0-9]*[a-z0-9])?$", value_str):
            return [
                f"{self._label} moet met een kleine letter beginnen en mag alleen kleine letters, "
                "cijfers en streepjes bevatten, geen spaties of hoofdletters"
            ]
        return []


class AttachmentIdValidator:
    """
    Validates attachment ids: lowercase letters, digits and hyphens, starting with a
    letter and ending alphanumeric, max 40 chars. The id becomes part of a Kubernetes
    volume name (``attch-{id}``), a DNS-1123 label capped at 63 chars, so 40 leaves
    margin while allowing descriptive names.

    When called with context containing ``existing_attachment_ids``, also checks uniqueness.
    """

    def validate(self, value: Any, context: dict[str, Any] | None = None) -> list[str]:
        if not value:
            return []
        value_str = str(value)
        if len(value_str) > 40:
            return ["Bijlage-id mag maximaal 40 tekens bevatten"]
        if not re.match(r"^[a-z]([a-z0-9-]*[a-z0-9])?$", value_str):
            return [
                "Moet beginnen met een kleine letter, mag kleine letters, cijfers en streepjes bevatten, "
                "en moet eindigen op een letter of cijfer"
            ]
        if context and value_str in context.get("existing_attachment_ids", []):
            return [f"Er bestaat al een bijlage met de id '{value_str}'"]
        return []


class SchemaPostfixValidator:
    """Validates an extra-schema postfix (RC-17): lowercase letters, digits and
    underscores, starting with a letter, and not longer than
    ``SCHEMA_POSTFIX_MAX_LENGTH``.

    The postfix becomes part of a PostgreSQL schema name
    (``{project}_{deployment}_{postfix}``) and, uppercased, an env-variable name
    (``DATABASE_SCHEMA_{POSTFIX}``), so both must be valid. Shape and length come from
    ``opi/utils/naming.py``, the same place the config model and the API read them, so a
    postfix cannot be accepted by one road in and refused by another.

    The length here does not replace the composed 63-character check: uniqueness, that
    limit and variable-name collisions are the section enforcer's job (they need the
    project and deployment names). It only makes an obviously-too-long postfix fail as
    what it is.

    Never normalised. The shape is strict enough that lowercasing ``Rapportage`` would
    mean storing something other than what was asked for, and the caller would find out
    from the schema name in their database rather than from the response.
    """

    def validate(self, value: Any) -> list[str]:
        if not value:
            return ["Postfix is verplicht"]
        if not re.match(SCHEMA_POSTFIX_PATTERN, str(value)):
            return [
                "Gebruik alleen kleine letters, cijfers en underscores, beginnend met een letter (bijv. 'rapportage')"
            ]
        if len(str(value)) > SCHEMA_POSTFIX_MAX_LENGTH:
            return [f"Een postfix mag hoogstens {SCHEMA_POSTFIX_MAX_LENGTH} tekens lang zijn"]
        return []


class InviteKeyValidator:
    """Validates an invite key: letters, digits, hyphens and underscores, starting with a
    letter or digit, 3 to 64 characters.

    The key becomes a URL path segment (``/invite/{key}``), so spaces, slashes and percent
    signs are rejected. Uppercase is allowed because a blank key is filled with a generated
    ``secrets.token_urlsafe`` value (mixed-case, URL-safe base64), which is later re-validated
    on edit and must pass. Emptiness is allowed here (the empty key is generated at save time);
    only a non-empty, malformed key is rejected.
    """

    def validate(self, value: Any) -> list[str]:
        if not value:
            return []  # empty is generated at save time
        value_str = str(value)
        if len(value_str) < 3 or len(value_str) > 64:
            return ["Uitnodigingssleutel moet tussen 3 en 64 tekens bevatten"]
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9_-]*$", value_str):
            return [
                "Uitnodigingssleutel mag alleen letters, cijfers, streepjes en "
                "onderstrepingstekens bevatten en moet met een letter of cijfer beginnen"
            ]
        return []


class ContainerImageValidator:
    """Validates container image references.

    Enforces lowercase, no spaces, and the same allowed-character set as
    the project schema's ``image`` pattern (alphanumerics plus ``._:/@-``,
    starting with an alphanumeric). Keeping this in sync with the schema
    means illegal characters are rejected up front with a clear message
    instead of failing late during ``validate_project_schema``.
    """

    # Mirrors the "image" pattern in opi/schemas/project_v2.json.
    _PATTERN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._:/@-]*)?\Z")
    _ALLOWED_CHAR = re.compile(r"[A-Za-z0-9._:/@-]")

    def validate(self, value: Any) -> list[str]:
        if not value:
            return []
        value_str = str(value)
        errors: list[str] = []
        if value_str != value_str.lower():
            errors.append("Container image moet volledig in kleine letters zijn")
        if " " in value_str:
            errors.append("Container image mag geen spaties bevatten")
        if not self._PATTERN.match(value_str):
            # Report the offending characters (spaces are flagged separately).
            invalid = sorted({c for c in value_str if c != " " and not self._ALLOWED_CHAR.match(c)})
            if invalid:
                errors.append("Container image bevat niet-toegestane tekens: " + " ".join(invalid))
            elif not errors:
                errors.append("Container image moet beginnen met een letter of cijfer")
        return errors


class RealmRoleValidator:
    """Validates Keycloak realm role names: alphanumeric, hyphens, underscores, max 255 chars."""

    def validate(self, value: Any) -> list[str]:
        if not value:
            return []
        value_str = str(value)
        if len(value_str) > 255:
            return ["Rolnaam mag maximaal 255 tekens bevatten"]
        if not re.match(r"^[a-zA-Z0-9_-]+$", value_str):
            return ["Rolnaam mag alleen letters, cijfers, streepjes en underscores bevatten"]
        return []


class EnvNameValidator:
    """Validates an environment-variable name: a letter or underscore, then letters,
    digits and underscores. Mirrors the ``_ENV_NAME`` regex the attachments config model
    enforces (``AttachmentUse._valid_env_name``), so an invalid name is caught at the
    field instead of only as a whole-config error at save time."""

    _PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\Z")

    def validate(self, value: Any) -> list[str]:
        if not value:
            return []
        if not self._PATTERN.match(str(value)):
            return [
                "Ongeldige omgevingsvariabelenaam: begin met een letter of underscore, daarna letters, cijfers en underscores"
            ]
        return []


class PathValidator:
    """Validates publication path format: must start with / and contain no spaces."""

    def validate(self, value: Any) -> list[str]:
        if not value:
            return []
        v = str(value)
        if not v.startswith("/"):
            return ["Pad moet beginnen met /"]
        if " " in v:
            return ["Pad mag geen spaties bevatten"]
        return []


class UrlValidator:
    """Validates that a value is a valid HTTP(S) URL."""

    def validate(self, value: Any) -> list[str]:
        if not value:
            return []
        v = str(value)
        if not v.startswith("http://") and not v.startswith("https://"):
            return ["Moet beginnen met http:// of https://"]
        return []


class RequiredValidator:
    """Validates that a field has a non-empty value."""

    def validate(self, value: Any) -> list[str]:
        if value is None:
            return ["Dit veld is verplicht"]
        if isinstance(value, str) and not value.strip():
            return ["Dit veld is verplicht"]
        if isinstance(value, list) and len(value) == 0:
            return ["Dit veld is verplicht"]
        return []


class KeyValueValidator:
    """Validates that text is parseable as ENV (KEY=value) or YAML key-value pairs.

    Delegates to ``validate_and_parse_env_vars`` which is the same parser
    used at deploy time, so validation here matches what will actually be
    accepted.
    """

    def validate(self, value: Any) -> list[str]:
        if not value or (isinstance(value, str) and not value.strip()):
            return []
        if not isinstance(value, str):
            return []
        try:
            from opi.utils.env_vars import validate_and_parse_env_vars

            validate_and_parse_env_vars(value)
        except (ValueError, TypeError) as e:
            return [str(e)]
        return []


class AllowedValuesValidator:
    """Validates that a value is one of the allowed options."""

    def __init__(self, allowed: list[str]) -> None:
        self.allowed = allowed

    def validate(self, value: Any) -> list[str]:
        if value is None or (isinstance(value, str) and not value.strip()):
            return []
        if str(value) not in self.allowed:
            return [f"Ongeldige waarde: {value}. Toegestaan: {', '.join(self.allowed)}"]
        return []


class MemoryRangeValidator:
    """Validates that a K8s memory string falls within a min/max range.

    Parses values like ``256Mi``, ``1Gi``, ``384Mi`` and checks that the
    value in MiB is between *min_mi* and *max_mi* (inclusive).

    When *max_mi* is None the cluster's ``max_memory_limit_mi`` is used.
    """

    def __init__(self, min_mi: int = 25, max_mi: int | None = None) -> None:
        self.min_mi = min_mi
        self._max_mi = max_mi

    @property
    def max_mi(self) -> int:
        if self._max_mi is not None:
            return self._max_mi
        from opi.core.cluster_config import get_max_memory_limit_mi
        from opi.core.config import settings

        return get_max_memory_limit_mi(settings.CLUSTER_MANAGER)

    def validate(self, value: Any) -> list[str]:
        if value is None or (isinstance(value, str) and not value.strip()):
            return []
        from opi.services.resource_analyzer import parse_k8s_memory_to_mi

        try:
            mi = parse_k8s_memory_to_mi(str(value))
        except ValueError:
            return [f"Ongeldige geheugenwaarde: {value}"]
        max_mi = self.max_mi
        if mi < self.min_mi or mi > max_mi:
            return [f"Geheugen moet tussen {self.min_mi}Mi en {max_mi}Mi liggen (was: {value})"]
        return []


class MemoryRequestRangeValidator(MemoryRangeValidator):
    """Validates memory requests against the lower request cap."""

    @property
    def max_mi(self) -> int:
        if self._max_mi is not None:
            return self._max_mi
        from opi.core.cluster_config import get_max_memory_request_mi
        from opi.core.config import settings

        return get_max_memory_request_mi(settings.CLUSTER_MANAGER)


class SubdomainValidator:
    """Validates subdomain format using the canonical validation from subdomain connector."""

    def validate(self, value: Any) -> list[str]:
        if not value:
            return []
        from opi.connectors.subdomain import validate_subdomain

        is_valid, error_msg = validate_subdomain(str(value))
        return [error_msg] if not is_valid and error_msg else []


class BaseDomainValidator:
    """Validates that a base domain is in the supported domains list."""

    def validate(self, value: Any) -> list[str]:
        if not value:
            return []
        if str(value) == "__custom__":
            return []  # Sentinel; actual domain validated via CustomDomainValidator
        from opi.connectors.subdomain import validate_base_domain

        is_valid, error_msg = validate_base_domain(str(value))
        return [error_msg] if not is_valid and error_msg else []


class CustomDomainValidator:
    """Validates custom domain format."""

    def validate(self, value: Any) -> list[str]:
        if not value:
            return []
        domain = str(value).strip().lower()
        if not re.match(
            r"^[a-z0-9]([a-z0-9\-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9\-]*[a-z0-9])?)+$",
            domain,
        ):
            return ["Ongeldig domeinformaat. Gebruik een geldig domeinnaam zoals 'voorbeeld.nl'"]
        return []


class DomainFormatValidator:
    """Validates that domain-format is a known template ID."""

    def validate(self, value: Any) -> list[str]:
        if not value:
            return []
        from opi.utils.naming import DOMAIN_FORMAT_TEMPLATES

        if str(value) not in DOMAIN_FORMAT_TEMPLATES:
            return [f"Onbekend URL-formaat: {value}"]
        return []


class CommandLineValidator:
    """Het startcommando van een container, als een regel tekst.

    Ruim over de inhoud en streng over de vorm. Een echt commando ziet eruit als
    ``sh -c "<script>"``, dus spaties, quotes en operatoren horen erin thuis; die weigeren
    zou juist het geval uitsluiten waarvoor het veld bestaat.

    Wat wel geweigerd wordt:

    * een quote die openstaat aan het eind. Dan splitst de regel anders dan de gebruiker
      bedoelde, en dat is precies het soort fout dat pas in een CrashLoopBackOff opvalt;
    * stuurtekens, die je niet per ongeluk typt en die ongewijzigd de container bereiken;
    * een regel die na het splitsen niets oplevert terwijl er wel iets stond.

    Geen verdediging tegen YAML-injectie: de canonieke schrijver zet een meerregelige
    waarde neer als literal block, dus een geknutseld argument komt terug als een string en
    niet als extra sleutels. ``tests/test_component_command_field.py`` bewaakt dat.
    """

    MAX_LENGTH = 4096

    def validate(self, value: Any) -> list[str]:
        if value is None or value == "":
            return []
        text = str(value)
        if not text.strip():
            return []
        if len(text) > self.MAX_LENGTH:
            return [f"Een startcommando mag hoogstens {self.MAX_LENGTH} tekens zijn."]
        verboden = [c for c in text if ord(c) < 32 and c not in "\n\t"]
        if verboden:
            return ["Dit commando bevat een stuurteken dat er niet in hoort."]
        if command_line_has_unbalanced_quote(text):
            return ['Er staat een dubbele quote open. Sluit hem, of typ "" als je er letterlijk een bedoelt.']
        if not split_command_line(text):
            return ["Dit commando levert geen argumenten op."]
        return []
