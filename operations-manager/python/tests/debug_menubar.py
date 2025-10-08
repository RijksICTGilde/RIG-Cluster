#!/usr/bin/env python3
"""
Debug script to test menubar data processing
"""

import sys

sys.path.append("/Users/robbertuittenbroek/IdeaProjects/RIG-Cluster/jinja-roos-components")

from jinja2 import Environment, FileSystemLoader
from jinja_roos_components.extension import setup_components


def debug_menubar():
    """Debug menubar component data processing"""

    # Setup Jinja2 environment
    template_dir = "/Users/robbertuittenbroek/IdeaProjects/RIG-Cluster/operations-manager/python/templates"
    env = Environment(loader=FileSystemLoader(template_dir))

    # Setup components
    try:
        setup_components(env)
        print("✅ Components setup successful")
    except Exception as e:
        print(f"❌ Components setup failed: {e}")
        return

    # Test data
    simple_menu = [{"label": "Home", "link": "/", "active": True}, {"label": "About", "link": "/about"}]

    context = {"simple_menu": simple_menu}

    try:
        template = env.get_template("debug-menubar.html.j2")
        result = template.render(**context)

        print("\n" + "=" * 60)
        print("RENDERED TEMPLATE OUTPUT:")
        print("=" * 60)
        print(result)

        # Check for key elements
        print("\n" + "=" * 60)
        print("ANALYSIS:")
        print("=" * 60)

        checks = [
            ("Template rendered", "html" in result.lower()),
            ("Menubar component found", "rvo-menubar" in result),
            ("Menu items rendered", "rvo-menubar__item" in result),
            ("Links found", 'href="' in result),
            ("Debug info present", "Simple menu variable:" in result),
        ]

        for check_name, passed in checks:
            status = "✅" if passed else "❌"
            print(f"{status} {check_name}")

        # Count menu items
        item_count = result.count("rvo-menubar__item")
        print(f"\n📊 Menu items found: {item_count}")

        return result

    except Exception as e:
        print(f"❌ Template rendering failed: {e}")
        import traceback

        traceback.print_exc()
        return None


if __name__ == "__main__":
    debug_menubar()
