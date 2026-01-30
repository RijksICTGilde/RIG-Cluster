#!/usr/bin/env python3
"""
Test the layout-row center verticalSpacing processing.
"""

from jinja2 import Environment, FileSystemLoader
from jinja_roos_components import get_templates_path, setup_components


def test_layout_center():
    """Test that verticalSpacing='center' produces correct CSS class."""
    env = Environment(loader=FileSystemLoader(str(get_templates_path())), autoescape=True)
    setup_components(env)

    # Load layout-row template
    template = env.get_template("components/layout-row.html.j2")

    # Render with center verticalSpacing
    context = {"_component_context": {"verticalSpacing": "center", "content": "Test content"}}

    result = template.render(context)
    print("Rendered output:")
    print(result)

    # Check if correct class is present
    if "rvo-layout-align-content-center" in result:
        print("✅ Correct CSS class 'rvo-layout-align-content-center' found")
        return True
    else:
        print("❌ Expected CSS class 'rvo-layout-align-content-center' not found")
        return False


if __name__ == "__main__":
    print("Testing layout-row verticalSpacing='center'")
    print("=" * 50)

    success = test_layout_center()

    if success:
        print("\n✅ Test passed! verticalSpacing='center' is correctly processed")
    else:
        print("\n❌ Test failed!")
