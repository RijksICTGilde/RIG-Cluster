"""Form hooks: enforcers, validators and the pipeline that runs them over a ``FormState``."""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


@dataclass
class FormState:
    """The state of a form while it is being processed, and what the hooks may change."""

    data: dict[str, Any] = field(default_factory=dict)
    errors: dict[str, list[str]] = field(default_factory=dict)

    is_new: bool = True
    is_submitted: bool = False
    is_valid: bool = True

    #: The values before the submission, which is what ``has_changed`` compares against.
    original_data: dict[str, Any] = field(default_factory=dict)

    #: Surroundings a hook may need, such as the cluster or the user.
    context: dict[str, Any] = field(default_factory=dict)

    def has_changed(self, field_name: str) -> bool:
        current = self.data.get(field_name)
        original = self.original_data.get(field_name)
        return current != original

    def add_error(self, field_name: str, message: str) -> None:
        if field_name not in self.errors:
            self.errors[field_name] = []
        self.errors[field_name].append(message)
        self.is_valid = False

    def get_field_value[T](self, field_name: str, default: T = None) -> T:  # type: ignore[assignment]
        return self.data.get(field_name, default)

    def set_field_value(self, field_name: str, value: object) -> None:
        self.data[field_name] = value


class FormHook(Protocol):
    """A hook called at a point in the form lifecycle; it returns the state to go on with."""

    async def execute(self, state: FormState) -> FormState: ...


class ValidatorHook(Protocol):
    """A validator reads the form data and returns its error messages, empty when valid."""

    async def validate(self, state: FormState) -> list[str]: ...


class EnforcerHook(Protocol):
    """An enforcer changes the form data to keep it consistent: a generated slug, a
    normalized value.
    """

    async def enforce(self, state: FormState) -> FormState: ...


class SlugEnforcerHook:
    """Generates a lowercase, hyphenated slug from another field."""

    def __init__(
        self,
        source_field: str,
        target_field: str,
        only_if_empty: bool = True,
    ) -> None:
        self.source_field = source_field
        self.target_field = target_field
        self.only_if_empty = only_if_empty

    async def enforce(self, state: FormState) -> FormState:
        source_value = state.get_field_value(self.source_field, "")
        target_value = state.get_field_value(self.target_field, "")

        if (not self.only_if_empty or not target_value) and source_value:
            slug = self._generate_slug(source_value)
            state.set_field_value(self.target_field, slug)

        return state

    def _generate_slug(self, value: str) -> str:
        import re

        slug = value.lower()
        slug = re.sub(r"[\s_]+", "-", slug)
        slug = re.sub(r"[^a-z0-9-]", "", slug)
        slug = re.sub(r"-+", "-", slug)
        slug = slug.strip("-")
        return slug


class RequiredValidatorHook:
    """Adds an error when the field is empty."""

    def __init__(
        self,
        field_name: str,
        message: str = "Dit veld is verplicht",
    ) -> None:
        self.field_name = field_name
        self.message = message

    async def validate(self, state: FormState) -> list[str]:
        value = state.get_field_value(self.field_name)
        if value is None or value == "" or (isinstance(value, list) and len(value) == 0):
            return [self.message]
        return []


class UniqueValidatorHook:
    """Checks through *check_func* that the field value does not exist yet."""

    def __init__(
        self,
        field_name: str,
        check_func: Callable[..., Awaitable[bool]],
        message: str = "Deze waarde bestaat al",
        exclude_current: bool = True,
    ) -> None:
        self.field_name = field_name
        self.check_func = check_func
        self.message = message
        self.exclude_current = exclude_current

    async def validate(self, state: FormState) -> list[str]:
        value = state.get_field_value(self.field_name)
        if not value:
            return []

        current_id = state.context.get("id") if self.exclude_current else None
        exists = await self.check_func(value, exclude_id=current_id)
        if exists:
            return [self.message]
        return []


class PatternValidatorHook:
    """Checks the field value against a regex pattern."""

    def __init__(
        self,
        field_name: str,
        pattern: str,
        message: str = "Dit veld voldoet niet aan het vereiste formaat",
    ) -> None:
        self.field_name = field_name
        self.pattern = pattern
        self.message = message

    async def validate(self, state: FormState) -> list[str]:
        import re

        value = state.get_field_value(self.field_name)
        if not value:
            return []  # emptiness is RequiredValidatorHook's business

        if not re.match(self.pattern, str(value)):
            return [self.message]
        return []


class RangeValidatorHook:
    """Checks that a numeric field value lies between *min_value* and *max_value*."""

    def __init__(
        self,
        field_name: str,
        min_value: float | None = None,
        max_value: float | None = None,
        message: str | None = None,
    ) -> None:
        self.field_name = field_name
        self.min_value = min_value
        self.max_value = max_value
        self.message = message

    async def validate(self, state: FormState) -> list[str]:
        value = state.get_field_value(self.field_name)
        if value is None or value == "":
            return []

        try:
            num_value = float(value)
        except ValueError, TypeError:
            return ["Voer een geldig getal in"]

        if self.min_value is not None and num_value < self.min_value:
            return [self.message or f"Waarde moet minimaal {self.min_value} zijn"]
        if self.max_value is not None and num_value > self.max_value:
            return [self.message or f"Waarde mag maximaal {self.max_value} zijn"]
        return []


class DependencyEnforcerHook:
    """Clears the dependent fields when the parent field changed, for a cascading dropdown."""

    def __init__(
        self,
        parent_field: str,
        dependent_fields: list[str],
    ) -> None:
        self.parent_field = parent_field
        self.dependent_fields = dependent_fields

    async def enforce(self, state: FormState) -> FormState:
        if state.has_changed(self.parent_field):
            for field in self.dependent_fields:
                state.set_field_value(field, None)
        return state


class FormProcessor:
    """Runs a form through its enforcers and then its validators."""

    def __init__(self) -> None:
        self.enforcers: list[EnforcerHook] = []
        self.validators: list[tuple[str, ValidatorHook]] = []
        self.pre_submit_hooks: list[FormHook] = []
        self.post_submit_hooks: list[FormHook] = []

    async def process(self, state: FormState) -> FormState:
        """Run the enforcers, then the validators, and return the resulting state."""
        for enforcer in self.enforcers:
            state = await enforcer.enforce(state)

        for field_name, validator in self.validators:
            errors = await validator.validate(state)
            for error in errors:
                state.add_error(field_name, error)

        return state
