#!/usr/bin/env python3
"""
Test the layout-row center verticalSpacing processing.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../jinja-roos-components"))

from jinja2 import Environment, FileSystemLoader


def test_layout_center():
    """Test that verticalSpacing='center' produces correct CSS class."""
    # Set up Jinja2 environment
    template_dir = (
        "/Users/robbertuittenbroek/IdeaProjects/RIG-Cluster/jinja-roos-components/jinja_roos_components/templates"
    )
    env = Environment(loader=FileSystemLoader(template_dir))

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
