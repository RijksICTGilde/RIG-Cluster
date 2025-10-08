#!/usr/bin/env python3
"""
Test the component validation system.
"""

from jinja_roos_components.components.registry import ComponentRegistry
from jinja_roos_components.validation import ComponentValidationError, validate_template_components


def test_valid_component():
    """Test validation of valid component usage."""
    registry = ComponentRegistry()

    # Valid component usage
    template = '<c-button kind="primary" label="Click me" />'

    try:
        results = validate_template_components(template, registry, strict_mode=True)
        print("✅ Valid component passed validation")
        print(f"Results: {results}")
    except ComponentValidationError as e:
        print(f"❌ Unexpected validation error: {e}")


def test_unknown_component():
    """Test validation of unknown component."""
    registry = ComponentRegistry()

    # Unknown component
    template = "<c-unknown-component />"

    try:
        results = validate_template_components(template, registry, strict_mode=True)
        print("❌ Unknown component should have failed validation")
    except ComponentValidationError as e:
        print(f"✅ Unknown component correctly failed validation: {e}")


def test_invalid_attribute():
    """Test validation of invalid attribute."""
    registry = ComponentRegistry()

    # Valid component with invalid attribute
    template = '<c-button invalid_attr="value" />'

    try:
        results = validate_template_components(template, registry, strict_mode=True)
        print("❌ Invalid attribute should have failed validation")
    except ComponentValidationError as e:
        print(f"✅ Invalid attribute correctly failed validation: {e}")


def test_invalid_enum_value():
    """Test validation of invalid enum value."""
    registry = ComponentRegistry()

    # Valid component with invalid enum value
    template = '<c-button kind="invalid_kind" label="Test" />'

    try:
        results = validate_template_components(template, registry, strict_mode=True)
        print("❌ Invalid enum value should have failed validation")
    except ComponentValidationError as e:
        print(f"✅ Invalid enum value correctly failed validation: {e}")


def test_invalid_color():
    """Test validation of invalid color."""
    registry = ComponentRegistry()

    # Valid component with invalid color
    template = '<c-icon icon="home" color="invalid_color" />'

    try:
        results = validate_template_components(template, registry, strict_mode=True)
        print("❌ Invalid color should have failed validation")
    except ComponentValidationError as e:
        print(f"✅ Invalid color correctly failed validation: {e}")


def test_valid_color():
    """Test validation of valid color."""
    registry = ComponentRegistry()

    # Valid component with valid color from colors.html.j2
    template = '<c-icon icon="home" color="groen" />'

    try:
        results = validate_template_components(template, registry, strict_mode=True)
        print("✅ Valid color passed validation")
        print(f"Results: {results}")
    except ComponentValidationError as e:
        print(f"❌ Valid color should have passed validation: {e}")


def test_lenient_mode():
    """Test validation in lenient mode."""
    registry = ComponentRegistry()

    # Multiple issues that should be warnings in lenient mode
    template = """
    <c-unknown-component />
    <c-button invalid_attr="value" kind="invalid_kind" />
    """

    try:
        results = validate_template_components(template, registry, strict_mode=False)
        print("✅ Lenient mode completed without exceptions")
        print(f"Results: {results}")

        # Check that errors were captured
        for result in results:
            if not result["valid"]:
                print(f"   Warning for {result['component']}: {result['errors']}")

    except ComponentValidationError as e:
        print(f"❌ Lenient mode should not raise exceptions: {e}")


if __name__ == "__main__":
    print("Testing Component Validation System")
    print("=" * 50)

    test_valid_component()
    print()

    test_unknown_component()
    print()

    test_invalid_attribute()
    print()

    test_invalid_enum_value()
    print()

    test_invalid_color()
    print()

    test_valid_color()
    print()

    test_lenient_mode()
    print()

    print("Testing completed!")
