#!/usr/bin/env python3
"""Debug layout component processing."""

from jinja2 import Environment, FileSystemLoader
from jinja_roos_components import get_templates_path, setup_components

# Setup paths

# Simple test - just layout components
test_template = """
<c-layout-row gap="md">
    Simple content
</c-layout-row>
"""


if __name__ == "__main__":
    # Create environment with debug mode
    env = Environment(loader=FileSystemLoader(str(get_templates_path())), autoescape=True)
    setup_components(env)

    print("Testing simple layout-row component...")
    print("Input template:")
    print(test_template)
    print("\n" + "=" * 50)

    try:
        template = env.from_string(test_template)
        html_output = template.render()
        print("Rendered output:")
        print(html_output)

        if "rvo-layout-row" in html_output:
            print("\n✅ SUCCESS: Layout component was processed!")
        else:
            print("\n❌ FAILED: Layout component was NOT processed")
            print("Raw component tag is still present in output")

    except Exception as e:
        print(f"\n❌ ERROR: {e}")
        import traceback

        traceback.print_exc()
