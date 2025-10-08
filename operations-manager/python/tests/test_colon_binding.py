#!/usr/bin/env python3
"""
Test colon binding with content in c-button.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../jinja-roos-components"))

from jinja2 import Environment, FileSystemLoader
from jinja_roos_components.extension_dom import ComponentExtensionDOM


def test_colon_binding_with_content():
    """Test that colon binding with content works correctly."""
    # Set up Jinja2 environment with component extension
    template_dir = (
        "/Users/robbertuittenbroek/IdeaProjects/RIG-Cluster/jinja-roos-components/jinja_roos_components/templates"
    )
    env = Environment(loader=FileSystemLoader(template_dir), extensions=[ComponentExtensionDOM])

    # Test template with colon binding and content
    test_template = """
<c-button kind="primary" :showIcon="'before'" :icon="'home'">
    Test Button
</c-button>
"""

    try:
        template = env.from_string(test_template)
        result = template.render()
        print("✅ Template with colon binding and content rendered successfully")
        print("Result:")
        print(result)
        return True
    except Exception as e:
        print(f"❌ Template failed to render: {e}")
        return False


def test_problematic_template():
    """Test the specific problematic template from line 356."""
    template_dir = (
        "/Users/robbertuittenbroek/IdeaProjects/RIG-Cluster/jinja-roos-components/jinja_roos_components/templates"
    )
    env = Environment(loader=FileSystemLoader(template_dir), extensions=[ComponentExtensionDOM])

    # The exact problematic template
    test_template = """<c-button
    kind="quaternary"
    size="sm"
    :showIcon="'before'"
    :icon="'verwijderen'"
    @click="removeComponentRow(this)">
    Verwijderen
</c-button>"""

    try:
        template = env.from_string(test_template)
        result = template.render()
        print("✅ Problematic template rendered successfully")
        print("Result:")
        print(result)
        return True
    except Exception as e:
        print(f"❌ Problematic template failed: {e}")
        import traceback

        traceback.print_exc()
        return False


if __name__ == "__main__":
    print("Testing colon binding with content")
    print("=" * 50)

    print("\n1. Simple test:")
    success1 = test_colon_binding_with_content()

    print("\n2. Problematic template test:")
    success2 = test_problematic_template()

    if success1 and success2:
        print("\n✅ All tests passed! Colon binding with content works correctly")
    else:
        print("\n❌ Some tests failed!")
