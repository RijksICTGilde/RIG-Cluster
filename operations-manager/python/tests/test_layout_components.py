#!/usr/bin/env python3
"""Test layout components rendering."""

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

# Test layout components
test_template = """
<c-layout-row gap="md">
    <c-layout-column size="md-6">
        <div>Column 1 Content</div>
    </c-layout-column>
    <c-layout-column size="md-6">
        <div>Column 2 Content</div>
    </c-layout-column>
</c-layout-row>
"""

# Create environment
env = Environment(loader=FileSystemLoader([str(component_templates)]), extensions=[ComponentExtension], autoescape=True)

# Render template
template = env.from_string(test_template)
html_output = template.render()

print("Layout components test output:")
print("=" * 50)
print(html_output)
print("=" * 50)

# Check if components are processed
if "rvo-layout-row" in html_output and "rvo-layout-column--md-6" in html_output:
    print("✅ Layout components processed correctly")
else:
    print("❌ Layout components NOT processed correctly")
    print("Missing layout-row:", "rvo-layout-row" not in html_output)
    print("Missing md-6 column:", "rvo-layout-column--md-6" not in html_output)
