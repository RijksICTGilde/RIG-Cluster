#!/usr/bin/env python3
"""Test script to verify event handling in ROOS components."""

import sys
from pathlib import Path

# Add jinja-roos-components to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "jinja-roos-components"))

from jinja2 import Environment, FileSystemLoader
from jinja_roos_components.extension import ComponentExtension

# Setup paths
component_templates = (
    Path(__file__).parent.parent.parent / "jinja-roos-components" / "jinja_roos_components" / "templates"
)

# Create a simple test template
test_template = """
<c-button @click="alert('Button clicked!')" @mouseover="console.log('Hover')">
    Test Button
</c-button>

<c-text-input-field 
    id="test-input"
    name="test"
    label="Test Input"
    @change="handleChange(event)"
    @focus="handleFocus()"
    @blur="handleBlur()"
    data-test="123"
    aria-label="Custom label">
</c-text-input-field>

<c-select-field
    id="test-select"
    name="test-select"
    label="Test Select"
    :options="['Option 1', 'Option 2']"
    @change="handleSelectChange(event)">
</c-select-field>

<c-textarea-field
    id="test-textarea"
    name="test-textarea"
    label="Test Textarea"
    @input="handleTextareaInput(event)"
    @keydown="handleKeyDown(event)">
</c-textarea-field>

<c-button 
    @click="submitForm()"
    @dblclick="handleDoubleClick()"
    @mouseenter="showTooltip()"
    @mouseleave="hideTooltip()"
    data-form-id="main-form"
    aria-describedby="submit-help">
    Submit Form
</c-button>
"""

# Create environment
env = Environment(loader=FileSystemLoader([str(component_templates)]), extensions=[ComponentExtension], autoescape=True)

# Render template
template = env.from_string(test_template)
html_output = template.render()

# Save output
output_file = Path(__file__).parent / "test_event_output.html"
output_file.write_text(html_output)

print("✓ Template rendered successfully")
print(f"✓ Output written to: {output_file}")

# Test cases for event handlers
# Note: Jinja2 autoescape will convert single quotes to &#39;
test_cases = [
    ('onclick="alert(&#39;Button clicked!&#39;)"', "Button click event"),
    ('onmouseover="console.log(&#39;Hover&#39;)"', "Mouse over event"),
    ('onchange="handleChange(event)"', "Input change event"),
    ('onfocus="handleFocus()"', "Input focus event"),
    ('onblur="handleBlur()"', "Input blur event"),
    ('onchange="handleSelectChange(event)"', "Select change event"),
    ('oninput="handleTextareaInput(event)"', "Textarea input event"),
    ('onkeydown="handleKeyDown(event)"', "Textarea keydown event"),
    ('onclick="submitForm()"', "Submit button click"),
    ('ondblclick="handleDoubleClick()"', "Double click event"),
    ('onmouseenter="showTooltip()"', "Mouse enter event"),
    ('onmouseleave="hideTooltip()"', "Mouse leave event"),
    ('data-test="123"', "Data attribute"),
    ('aria-label="Custom label"', "ARIA attribute"),
    ('data-form-id="main-form"', "Custom data attribute"),
    ('aria-describedby="submit-help"', "ARIA describedby attribute"),
]

# Verify all event handlers are present
print("\nChecking event handlers:")
all_passed = True
for expected, description in test_cases:
    if expected in html_output:
        print(f"  ✓ {description}: Found")
    else:
        print(f"  ✗ {description}: NOT FOUND")
        all_passed = False

# Check that no @click remains
if "@click" in html_output:
    print("\n✗ ERROR: Found unprocessed @click attributes")
    all_passed = False
else:
    print("\n✓ No unprocessed @click attributes found")

# Check icon classes
if "rvo-icon--" in html_output and "rvo-icon--sm" not in html_output and "rvo-icon--md" not in html_output:
    print("✗ ERROR: Found double-dash icon class")
    all_passed = False
else:
    print("✓ Icon classes correctly formatted")

if all_passed:
    print("\n✅ All event handling tests passed!")
    sys.exit(0)
else:
    print("\n❌ Some tests failed")
    sys.exit(1)
