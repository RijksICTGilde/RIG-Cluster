#!/usr/bin/env python3
"""Test the layout-grid component."""

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

# Test grid component
test_template = """
<c-layout-grid columns="2" gap="md">
    <div>Item 1</div>
    <div>Item 2</div>
</c-layout-grid>
"""

# Create environment
env = Environment(loader=FileSystemLoader([str(component_templates)]), extensions=[ComponentExtension], autoescape=True)

# Render template
template = env.from_string(test_template)
html_output = template.render()

print("Grid component output:")
print("=" * 40)
print(html_output)
print("=" * 40)

if "rvo-layout-grid--columns-2" in html_output and "rvo-layout-grid--gap-md" in html_output:
    print("✅ Grid component working correctly")
else:
    print("❌ Grid component not working correctly")
