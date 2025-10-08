#!/usr/bin/env python3
"""
Simple test to verify selected state
"""

import sys

sys.path.append("/Users/robbertuittenbroek/IdeaProjects/RIG-Cluster/jinja-roos-components")

from jinja2 import Environment, FileSystemLoader
from jinja_roos_components.extension import setup_components


def test_selected_simple():
    """Simple test for selected state"""

    template_dir = "/Users/robbertuittenbroek/IdeaProjects/RIG-Cluster/operations-manager/python/templates"
    env = Environment(loader=FileSystemLoader(template_dir))
    setup_components(env)

    template = env.get_template("formulier-template.html.j2")
    result = template.render()

    # Look for the specific line with "Naam app/website"
    lines = result.split("\n")
    for i, line in enumerate(lines):
        if "Naam app/website" in line:
            # Look at surrounding lines for the link
            for j in range(max(0, i - 3), min(len(lines), i + 4)):
                if "rvo-link" in lines[j]:
                    print(f"Line {j}: {lines[j].strip()}")

    # Count occurrences
    active_count = result.count("rvo-link--active")
    selected_count = result.count('"selected": true')

    print("\n📊 COUNTS:")
    print(f"- rvo-link--active: {active_count}")
    print(f'- "selected": true in template: {selected_count}')

    # Extract menubar section
    start = result.find("rvo-menubar__background")
    end = result.find("</nav>", start) + 6
    if start > -1 and end > start:
        menubar_section = result[start:end]
        print(f"\n🔍 MENUBAR SECTION ACTIVE COUNT: {menubar_section.count('rvo-link--active')}")


if __name__ == "__main__":
    test_selected_simple()
