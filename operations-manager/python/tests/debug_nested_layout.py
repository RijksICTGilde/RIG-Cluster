#!/usr/bin/env python3
"""Debug nested layout component processing."""

from jinja2 import Environment, FileSystemLoader
from jinja_roos_components import get_templates_path, setup_components

# Setup paths

# Test nested layout components like in the form
test_template = """
<c-layout-row gap="md">
    <c-layout-column size="md-6">
        Column 1
    </c-layout-column>
    <c-layout-column size="md-6">
        Column 2
    </c-layout-column>
</c-layout-row>
"""


if __name__ == "__main__":
    # Create environment
    env = Environment(loader=FileSystemLoader(str(get_templates_path())), autoescape=True)
    setup_components(env)

    print("Testing nested layout components...")
    print("Input template:")
    print(test_template)
    print("\n" + "=" * 50)

    try:
        template = env.from_string(test_template)
        html_output = template.render()
        print("Rendered output:")
        print(html_output[:1000] + "..." if len(html_output) > 1000 else html_output)

        # Check if all components were processed
        checks = [
            ("rvo-layout-row", "Layout row processed"),
            ("rvo-layout-column--md-6", "Layout columns processed"),
            ("c-layout", "No unprocessed c-layout tags remain"),
        ]

        print("\nChecking results:")
        for pattern, description in checks:
            if pattern == "c-layout":
                # This should NOT be found (no unprocessed tags)
                if pattern not in html_output:
                    print(f"  ✅ {description}")
                else:
                    print(f"  ❌ {description} - Found unprocessed tags!")
            else:
                # These SHOULD be found (processed components)
                if pattern in html_output:
                    print(f"  ✅ {description}")
                else:
                    print(f"  ❌ {description} - NOT FOUND")

    except Exception as e:
        print(f"\n❌ ERROR: {e}")
        import traceback

        traceback.print_exc()
