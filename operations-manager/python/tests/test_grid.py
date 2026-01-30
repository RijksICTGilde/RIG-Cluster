#!/usr/bin/env python3
"""Test the layout-grid component."""

from jinja2 import Environment, FileSystemLoader
from jinja_roos_components import get_templates_path, setup_components

# Setup paths

# Test grid component
test_template = """
<c-layout-grid columns="2" gap="md">
    <div>Item 1</div>
    <div>Item 2</div>
</c-layout-grid>
"""

if __name__ == "__main__":
    # Create environment
    env = Environment(loader=FileSystemLoader(str(get_templates_path())), autoescape=True)
    setup_components(env)

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
