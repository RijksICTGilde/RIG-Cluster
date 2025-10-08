#!/usr/bin/env python3
"""Test script to render and verify the self-service portal template."""

import sys
from pathlib import Path

# Add jinja-roos-components to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "jinja-roos-components"))

from jinja2 import Environment, FileSystemLoader
from jinja_roos_components.extension import ComponentExtension

# Setup paths
template_dir = Path(__file__).parent / "templates"
output_file = Path(__file__).parent / "test_portal_output.html"

# Create Jinja environment with ROOS components
# Need to include both template dir and component templates
component_templates = (
    Path(__file__).parent.parent.parent / "jinja-roos-components" / "jinja_roos_components" / "templates"
)
env = Environment(
    loader=FileSystemLoader([str(template_dir), str(component_templates)]),
    extensions=[ComponentExtension],
    autoescape=True,
)

# Test data
context = {
    "project": {
        "name": "Test Project",
        "description": "A test project for verification",
        "users": [
            {"name": "Alice Admin", "email": "alice@example.com", "role": "Admin"},
            {"name": "Bob User", "email": "bob@example.com", "role": "User"},
        ],
    },
    "available_services": [
        {"name": "PostgreSQL", "description": "Managed database", "selected": True},
        {"name": "Keycloak", "description": "Identity management", "selected": False},
        {"name": "Vault", "description": "Secret management", "selected": True},
    ],
}

# Render template
template = env.get_template("self-service-portal.html.j2")
html_output = template.render(**context)

# Write output
output_file.write_text(html_output)

# Verify critical elements
print("✓ Template rendered successfully")
print(f"✓ Output written to: {output_file}")

# Check for icon class issues
if "rvo-icon--plus" in html_output:
    print("✗ ERROR: Found double-dash icon class 'rvo-icon--plus' (should be 'rvo-icon-plus')")
    sys.exit(1)
elif "rvo-icon-plus" in html_output:
    print("✓ Icon class 'rvo-icon-plus' found correctly")
else:
    print("⚠ Warning: No plus icon found in output")

# Check button placement (should appear after user list)
user_list_pos = html_output.find("Huidige gebruikers")
button_pos = html_output.find("Gebruiker toevoegen")
if user_list_pos > 0 and button_pos > 0:
    if button_pos > user_list_pos:
        # Further check: button should be after the table
        table_end_pos = html_output.find("</table>", user_list_pos)
        if button_pos > table_end_pos:
            print("✓ 'Gebruiker toevoegen' button is correctly placed below user list")
        else:
            print("✗ ERROR: Button appears inside the user table")
            sys.exit(1)
    else:
        print("✗ ERROR: 'Gebruiker toevoegen' button appears before user list")
        sys.exit(1)

# Check for required attributes that shouldn't be there
if "required=" in html_output or "required>" in html_output:
    print("⚠ Warning: Found 'required' attribute in output (should use label classes instead)")

# Check for size="max" handling
if "utrecht-textbox--max" in html_output:
    print("✗ ERROR: Found 'utrecht-textbox--max' class (size='max' should not add classes)")
    sys.exit(1)
else:
    print("✓ No invalid 'max' size classes found")

# Verify fieldset wrapper structure
if '<div class="utrecht-form-fieldset' in html_output:
    print("✓ Fieldset wrapper divs present")
else:
    print("⚠ Warning: Fieldset wrapper divs might be missing")

print("\n✅ All critical checks passed!")
print(f"\nView the rendered output at: {output_file}")
