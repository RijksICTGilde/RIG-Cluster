#!/usr/bin/env python3
"""Test the new components section in the self-service portal."""

import sys
from pathlib import Path

# Add jinja-roos-components to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "jinja-roos-components"))

from jinja2 import Environment, FileSystemLoader
from jinja_roos_components.extension import ComponentExtension

# Setup paths
template_dir = Path(__file__).parent / "templates"
component_templates = (
    Path(__file__).parent.parent.parent / "jinja-roos-components" / "jinja_roos_components" / "templates"
)

# Create environment
env = Environment(
    loader=FileSystemLoader([str(template_dir), str(component_templates)]),
    extensions=[ComponentExtension],
    autoescape=True,
)

# Test data for components
context = {
    "project": {
        "name": "Test Components Project",
        "description": "Testing components functionality",
        "users": [{"name": "Test User", "email": "test@example.com", "role": "Admin"}],
    },
    "available_services": [
        {"name": "Keycloak", "description": "Identity management", "selected": False},
        {"name": "PostgreSQL", "description": "Database", "selected": False},
        {"name": "MinIO", "description": "Object storage", "selected": False},
    ],
}

# Render template
template = env.get_template("self-service-portal.html.j2")
html_output = template.render(**context)

# Save output
output_file = Path(__file__).parent / "test_components_output.html"
output_file.write_text(html_output)

print("✓ Components section template rendered successfully")
print(f"✓ Output written to: {output_file}")

# Check for components section elements
components_checks = [
    # Component form fields
    ("Components Configuratie", "Components section header"),
    ("Component Type", "Component type field label"),
    ("Container Image", "Container image field label"),
    ("CPU Limiet", "CPU limit field label"),
    ("Memory Limiet", "Memory limit field label"),
    ("Poort", "Port field label"),
    # Select options for component type
    ("Single (All-in-one)", "Single component type option"),
    ("Frontend", "Frontend component type option"),
    ("Backend", "Backend component type option"),
    # CPU options
    ("1 CPU", "1 CPU option"),
    ("4 CPU", "4 CPU option"),
    # Memory options
    ("128 MB", "128MB memory option"),
    ("1 GB", "1GB memory option"),
    # Service binding section
    ("Gekoppelde Services", "Service binding section"),
    ("component-1-service-keycloak", "Keycloak service binding checkbox"),
    ("component-1-service-postgres", "PostgreSQL service binding checkbox"),
    ("component-1-service-minio", "MinIO service binding checkbox"),
    # Component management buttons
    ("Component Toevoegen", "Add component button"),
    ("removeComponentRow", "Remove component function"),
    # Rootless images explanation
    ("rootless image", "Rootless image explanation"),
    ("USER instructie", "Docker USER instruction explanation"),
    # Service binding visual elements
    ("service-binding-item", "Service binding visual cards"),
    ("Identity Management", "Keycloak service description"),
    ("Object Storage", "MinIO service description"),
]

print("\nChecking components section elements:")
all_passed = True
for expected, description in components_checks:
    if expected in html_output:
        print(f"  ✅ {description}")
    else:
        print(f"  ❌ {description} - NOT FOUND")
        all_passed = False

# Check form structure
print("\nChecking form structure:")
form_structure_checks = [
    ("components[0][type]", "Component type form field name"),
    ("components[0][image]", "Component image form field name"),
    ("components[0][port]", "Component port form field name"),
    ("components[0][cpu_limit]", "Component CPU limit form field name"),
    ("components[0][memory_limit]", "Component memory limit form field name"),
    ("components[0][services][]", "Component services array form field name"),
]

for expected, description in form_structure_checks:
    if expected in html_output:
        print(f"  ✅ {description}")
    else:
        print(f"  ❌ {description} - NOT FOUND")
        all_passed = False

# Check accessibility
print("\nChecking accessibility:")
accessibility_checks = [
    ('aria-label="Identity"', "Keycloak icon accessibility label"),
    ('aria-label="Database"', "PostgreSQL icon accessibility label"),
    ('aria-label="Storage"', "MinIO icon accessibility label"),
    ('role="img"', "Icon role attributes"),
]

for expected, description in accessibility_checks:
    if expected in html_output:
        print(f"  ✅ {description}")
    else:
        print(f"  ❌ {description} - NOT FOUND")
        all_passed = False

# Check event handlers (converted to onclick in HTML)
print("\nChecking event handlers:")
event_checks = [
    ('onclick="addComponentRow()"', "Add component event handler"),
    ('onclick="removeComponentRow(this)"', "Remove component event handler"),
]

for expected, description in event_checks:
    if expected in html_output:
        print(f"  ✅ {description}")
    else:
        print(f"  ❌ {description} - NOT FOUND")
        all_passed = False

print(f"\n📊 Total form sections found: {html_output.count('fieldset')}")
print(f"📊 Total component service checkboxes: {html_output.count('components[0][services][]')}")

if all_passed:
    print("\n✅ All components section tests passed!")
    print(f"🌐 View the full form at: {output_file}")
else:
    print("\n❌ Some components section tests failed")
    sys.exit(1)
