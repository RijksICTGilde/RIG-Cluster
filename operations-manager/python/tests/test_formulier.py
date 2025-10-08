#!/usr/bin/env python3
"""
Test the fixed formulier template
"""

import sys

sys.path.append("/Users/robbertuittenbroek/IdeaProjects/RIG-Cluster/jinja-roos-components")

from jinja2 import Environment, FileSystemLoader
from jinja_roos_components.extension import setup_components


def test_formulier_template():
    """Test the formulier template with fixed menubar"""

    # Setup Jinja2 environment
    template_dir = "/Users/robbertuittenbroek/IdeaProjects/RIG-Cluster/operations-manager/python/templates"
    env = Environment(loader=FileSystemLoader(template_dir))

    # Setup components
    setup_components(env)

    try:
        template = env.get_template("formulier-template.html.j2")
        result = template.render()

        # Check for menubar items
        menubar_items = result.count("rvo-menubar__item")
        menubar_links = result.count("rvo-menubar__link")
        active_items = result.count("rvo-menubar__link--active")

        print("MENUBAR ANALYSIS:")
        print("=" * 40)
        print(f"✅ Menu items found: {menubar_items}")
        print(f"✅ Menu links found: {menubar_links}")
        print(f"✅ Active items found: {active_items}")

        # Look for specific menu labels
        expected_labels = ["Naam app/website", "Menu item", "Menu item met icoon", "Menu item rechts"]
        found_labels = []
        for label in expected_labels:
            if label in result:
                found_labels.append(label)
                print(f"✅ Found label: '{label}'")
            else:
                print(f"❌ Missing label: '{label}'")

        success = menubar_items > 0 and len(found_labels) >= 3
        print(f"\n{'🎉 SUCCESS' if success else '❌ FAILURE'}: Menubar is {'working' if success else 'not working'}")

        return success

    except Exception as e:
        print(f"❌ Template rendering failed: {e}")
        import traceback

        traceback.print_exc()
        return False


if __name__ == "__main__":
    test_formulier_template()
