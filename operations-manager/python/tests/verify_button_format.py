#!/usr/bin/env python3
"""Verify button format matches expected Utrecht design system structure."""

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

# Test button matching your expected format
test_template = """
<c-button 
    kind="primary"
    size="md"
    :hover="true"
    :showIcon="'before'"
    :icon="'home'">
    Button
</c-button>
"""

# Create environment
env = Environment(loader=FileSystemLoader([str(component_templates)]), extensions=[ComponentExtension], autoescape=True)

# Render template
template = env.from_string(test_template)
html_output = template.render()

print("Generated button HTML:")
print("=" * 50)
print(html_output)
print("=" * 50)

# Expected patterns to verify
expected_patterns = [
    'class="utrecht-button',
    "utrecht-button--hover",
    "utrecht-button--rvo-md",
    "utrecht-button--icon-before",
    'data-utrecht-button-appearance="primary-action-button"',
    "utrecht-icon rvo-icon rvo-icon-home rvo-icon--md rvo-icon--hemelblauw",
    'role="img"',
    'aria-label="Home"',
]

print("Verification checklist:")
all_passed = True
for pattern in expected_patterns:
    if pattern in html_output:
        print(f"  ✅ {pattern}")
    else:
        print(f"  ❌ {pattern} - NOT FOUND")
        all_passed = False

print("\nYour expected format:")
print(
    '<button class="utrecht-button utrecht-button--primary-action utrecht-button--hover utrecht-button--rvo-md utrecht-button--icon-before" type="button"><span class="utrecht-icon rvo-icon rvo-icon-home rvo-icon--md rvo-icon--hemelblauw" role="img" aria-label="Home"></span>Button</button>'
)

if all_passed:
    print("\n✅ SUCCESS: Button format matches Utrecht design system requirements!")
else:
    print("\n❌ Some patterns missing")
    sys.exit(1)
