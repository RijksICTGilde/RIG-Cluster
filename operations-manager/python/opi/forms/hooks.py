"""
Form hooks for lifecycle events and dynamic behavior.

Hooks allow custom logic to be executed at various points in the form lifecycle:
- Before/after field rendering
- On field value change
- Before/after form submission
- Validation hooks

Inspired by TAD's Editable pattern with enforcers, converters, and validators.
"""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


@dataclass
class FormState:
    """
    Represents the current state of a form during processing.

    This provides context for hooks and allows them to modify
    form behavior dynamically.
    """

    # Form data
    data: dict[str, Any] = field(default_factory=dict)
    errors: dict[str, list[str]] = field(default_factory=dict)

    # Metadata
    is_new: bool = True
    is_submitted: bool = False
    is_valid: bool = True

    # Original data (for change detection)
    original_data: dict[str, Any] = field(default_factory=dict)

    # Context (cluster, user, etc.)
    context: dict[str, Any] = field(default_factory=dict)

    def has_changed(self, field_name: str) -> bool:
        """Check if a field value has changed from original."""
        current = self.data.get(field_name)
        original = self.original_data.get(field_name)
        return current != original

    def get_changed_fields(self) -> list[str]:
        """Get list of field names that have changed."""
        return [key for key in set(self.data.keys()) | set(self.original_data.keys()) if self.has_changed(key)]

    def add_error(self, field_name: str, message: str) -> None:
        """Add an error message to a field."""
        if field_name not in self.errors:
            self.errors[field_name] = []
        self.errors[field_name].append(message)
        self.is_valid = False

    def clear_errors(self, field_name: str | None = None) -> None:
        """Clear errors for a field or all fields."""
        if field_name:
            self.errors.pop(field_name, None)
        else:
            self.errors.clear()
        self.is_valid = len(self.errors) == 0

    def get_field_value[T](self, field_name: str, default: T = None) -> T:  # type: ignore[assignment]
        """Get a field value with optional default."""
        return self.data.get(field_name, default)

    def set_field_value(self, field_name: str, value: object) -> None:
        """Set a field value."""
        self.data[field_name] = value


class FormHook(Protocol):
    """
    Protocol for form hooks.

    Hooks are called at specific points in the form lifecycle
    and can modify form state.
    """

    async def execute(self, state: FormState) -> FormState:
        """
        Execute the hook.

        Args:
            state: Current form state

        Returns:
            Modified form state
        """
        ...


class ValidatorHook(Protocol):
    """
    Protocol for validation hooks.

    Validators check form data and add errors to state.
    """

    async def validate(self, state: FormState) -> list[str]:
        """
        Validate form state.

        Args:
            state: Current form state

        Returns:
            List of error messages (empty if valid)
        """
        ...


class EnforcerHook(Protocol):
    """
    Protocol for enforcer hooks.

    Enforcers modify form data to ensure consistency
    (e.g., generating slugs, normalizing values).
    """

    async def enforce(self, state: FormState) -> FormState:
        """
        Enforce rules on form state.

        Args:
            state: Current form state

        Returns:
            Modified form state
        """
        ...


class SlugEnforcerHook:
    """
    Enforcer that generates a slug from another field.

    Converts a display name to a lowercase, hyphenated slug.
    """

    def __init__(
        self,
        source_field: str,
        target_field: str,
        only_if_empty: bool = True,
    ) -> None:
        """
        Initialize the slug enforcer.

        Args:
            source_field: Field to generate slug from
            target_field: Field to store the slug
            only_if_empty: Only generate if target is empty
        """
        self.source_field = source_field
        self.target_field = target_field
        self.only_if_empty = only_if_empty

    async def enforce(self, state: FormState) -> FormState:
        """Generate slug from source field."""
        source_value = state.get_field_value(self.source_field, "")
        target_value = state.get_field_value(self.target_field, "")

        # Only generate if target is empty or we're not in only_if_empty mode
        if (not self.only_if_empty or not target_value) and source_value:
            slug = self._generate_slug(source_value)
            state.set_field_value(self.target_field, slug)

        return state

    def _generate_slug(self, value: str) -> str:
        """Convert a string to a slug."""
        import re

        # Lowercase
        slug = value.lower()
        # Replace spaces and underscores with hyphens
        slug = re.sub(r"[\s_]+", "-", slug)
        # Remove non-alphanumeric characters except hyphens
        slug = re.sub(r"[^a-z0-9-]", "", slug)
        # Remove multiple hyphens
        slug = re.sub(r"-+", "-", slug)
        # Trim hyphens from ends
        slug = slug.strip("-")
        return slug


class RequiredValidatorHook:
    """
    Validator that checks required fields.

    Adds an error if a required field is empty.
    """

    def __init__(
        self,
        field_name: str,
        message: str = "Dit veld is verplicht",
    ) -> None:
        """
        Initialize the required validator.

        Args:
            field_name: Field to validate
            message: Error message if field is empty
        """
        self.field_name = field_name
        self.message = message

    async def validate(self, state: FormState) -> list[str]:
        """Check if field has a value."""
        value = state.get_field_value(self.field_name)
        if value is None or value == "" or (isinstance(value, list) and len(value) == 0):
            return [self.message]
        return []


class UniqueValidatorHook:
    """
    Validator that checks for unique values.

    Validates that a field value is unique within a collection.
    """

    def __init__(
        self,
        field_name: str,
        check_func: Callable[..., Awaitable[bool]],
        message: str = "Deze waarde bestaat al",
        exclude_current: bool = True,
    ) -> None:
        """
        Initialize the unique validator.

        Args:
            field_name: Field to validate
            check_func: Async function that returns True if value exists
            message: Error message if value is not unique
            exclude_current: Exclude current record when checking
        """
        self.field_name = field_name
        self.check_func = check_func
        self.message = message
        self.exclude_current = exclude_current

    async def validate(self, state: FormState) -> list[str]:
        """Check if field value is unique."""
        value = state.get_field_value(self.field_name)
        if not value:
            return []

        # Get current ID for exclusion
        current_id = state.context.get("id") if self.exclude_current else None

        # Check if value exists
        exists = await self.check_func(value, exclude_id=current_id)
        if exists:
            return [self.message]
        return []


class PatternValidatorHook:
    """
    Validator that checks field value matches a regex pattern.
    """

    def __init__(
        self,
        field_name: str,
        pattern: str,
        message: str = "Dit veld voldoet niet aan het vereiste formaat",
    ) -> None:
        """
        Initialize the pattern validator.

        Args:
            field_name: Field to validate
            pattern: Regex pattern to match
            message: Error message if pattern doesn't match
        """
        self.field_name = field_name
        self.pattern = pattern
        self.message = message

    async def validate(self, state: FormState) -> list[str]:
        """Check if field value matches pattern."""
        import re

        value = state.get_field_value(self.field_name)
        if not value:
            return []  # Don't validate empty fields (use RequiredValidator for that)

        if not re.match(self.pattern, str(value)):
            return [self.message]
        return []


class RangeValidatorHook:
    """
    Validator that checks numeric values are within a range.
    """

    def __init__(
        self,
        field_name: str,
        min_value: float | None = None,
        max_value: float | None = None,
        message: str | None = None,
    ) -> None:
        """
        Initialize the range validator.

        Args:
            field_name: Field to validate
            min_value: Minimum allowed value (None for no minimum)
            max_value: Maximum allowed value (None for no maximum)
            message: Custom error message
        """
        self.field_name = field_name
        self.min_value = min_value
        self.max_value = max_value
        self.message = message

    async def validate(self, state: FormState) -> list[str]:
        """Check if field value is within range."""
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
    """
    Enforcer that clears dependent field values when parent changes.

    Useful for cascading dropdowns or conditional fields.
    """

    def __init__(
        self,
        parent_field: str,
        dependent_fields: list[str],
    ) -> None:
        """
        Initialize the dependency enforcer.

        Args:
            parent_field: Parent field name
            dependent_fields: Fields to clear when parent changes
        """
        self.parent_field = parent_field
        self.dependent_fields = dependent_fields

    async def enforce(self, state: FormState) -> FormState:
        """Clear dependent fields if parent has changed."""
        if state.has_changed(self.parent_field):
            for field in self.dependent_fields:
                state.set_field_value(field, None)
        return state


class FormProcessor:
    """
    Processes forms through a pipeline of hooks.

    Orchestrates the execution of enforcers and validators.
    """

    def __init__(self) -> None:
        """Initialize the form processor."""
        self.enforcers: list[EnforcerHook] = []
        self.validators: list[tuple[str, ValidatorHook]] = []
        self.pre_submit_hooks: list[FormHook] = []
        self.post_submit_hooks: list[FormHook] = []

    def add_enforcer(self, enforcer: EnforcerHook) -> FormProcessor:
        """Add an enforcer hook."""
        self.enforcers.append(enforcer)
        return self

    def add_validator(self, field_name: str, validator: ValidatorHook) -> FormProcessor:
        """Add a validator hook for a specific field."""
        self.validators.append((field_name, validator))
        return self

    def add_pre_submit_hook(self, hook: FormHook) -> FormProcessor:
        """Add a pre-submission hook."""
        self.pre_submit_hooks.append(hook)
        return self

    def add_post_submit_hook(self, hook: FormHook) -> FormProcessor:
        """Add a post-submission hook."""
        self.post_submit_hooks.append(hook)
        return self

    async def process(self, state: FormState) -> FormState:
        """
        Process form through the hook pipeline.

        Args:
            state: Initial form state

        Returns:
            Processed form state
        """
        # Run enforcers
        for enforcer in self.enforcers:
            state = await enforcer.enforce(state)

        # Run validators
        for field_name, validator in self.validators:
            errors = await validator.validate(state)
            for error in errors:
                state.add_error(field_name, error)

        return state

    async def process_submission(self, state: FormState) -> FormState:
        """
        Process a form submission through the full pipeline.

        Args:
            state: Form state with submitted data

        Returns:
            Processed form state
        """
        state.is_submitted = True

        # Run pre-submit hooks
        for hook in self.pre_submit_hooks:
            state = await hook.execute(state)

        # Process enforcers and validators
        state = await self.process(state)

        # Run post-submit hooks only if valid
        if state.is_valid:
            for hook in self.post_submit_hooks:
                state = await hook.execute(state)

        return state
