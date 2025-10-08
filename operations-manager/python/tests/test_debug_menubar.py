#!/usr/bin/env python3
"""
Test script to debug menubar component data processing
"""

import sys

sys.path.append("/Users/robbertuittenbroek/IdeaProjects/RIG-Cluster/jinja-roos-components")

from jinja2 import Environment, FileSystemLoader
from jinja_roos_components.extension import setup_components


def test_debug_menubar():
    """Test menubar debug component"""

    # Setup Jinja2 environment
    template_dir = "/Users/robbertuittenbroek/IdeaProjects/RIG-Cluster/operations-manager/python/templates"
    env = Environment(loader=FileSystemLoader(template_dir))

    # Setup components
    setup_components(env)

    # Test data
    test_menu = [{"label": "Home", "link": "/", "active": True}, {"label": "About", "link": "/about"}]

    context = {"test_menu": test_menu}

    try:
        template = env.get_template("test-debug-menubar.html.j2")
        result = template.render(**context)

        print("RENDERED OUTPUT:")
        print("=" * 60)
        print(result)

    except Exception as e:
        print(f"Error: {e}")
        import traceback

        traceback.print_exc()


if __name__ == "__main__":
    test_debug_menubar()
