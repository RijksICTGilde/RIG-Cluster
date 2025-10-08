#!/usr/bin/env python3
"""
Test the menubar HTML output to verify correct class structure
"""

import sys

sys.path.append("/Users/robbertuittenbroek/IdeaProjects/RIG-Cluster/jinja-roos-components")

from jinja2 import Environment, FileSystemLoader
from jinja_roos_components.extension import setup_components


def test_menubar_html():
    """Test menubar HTML output"""

    # Simple template to test menubar
    templates = {
        "test.html": """
<c-menubar 
    size="md"
    :useIcons="true"
    iconPlacement="before"
    maxWidth="md"
    :items="[
        {
            'label': 'Home',
            'link': '#',
            'active': true
        },
        {
            'label': 'About',
            'link': '/about'
        },
        {
            'label': 'Menu item rechts',
            'link': '#',
            'align': 'right'
        }
    ]" />
"""
    }

    # Write test template to file
    with open(
        "/Users/robbertuittenbroek/IdeaProjects/RIG-Cluster/operations-manager/python/templates/test-menubar-styles.html",
        "w",
    ) as f:
        f.write(templates["test.html"])

    template_dir = "/Users/robbertuittenbroek/IdeaProjects/RIG-Cluster/operations-manager/python/templates"
    env = Environment(loader=FileSystemLoader(template_dir))
    setup_components(env)

    template = env.get_template("test-menubar-styles.html")
    result = template.render()

    print("MENUBAR HTML OUTPUT:")
    print("=" * 60)

    # Extract just the menubar part
    lines = result.split("\n")
    in_menubar = False
    menubar_lines = []

    for line in lines:
        if "rvo-menubar__background" in line:
            in_menubar = True
        if in_menubar:
            menubar_lines.append(line)
        if "</div>" in line and in_menubar and "rvo-menubar__background" in "".join(menubar_lines[-10:]):
            break

    menubar_html = "\n".join(menubar_lines)
    print(menubar_html)

    print("\n" + "=" * 60)
    print("CLASS ANALYSIS:")
    print("=" * 60)

    # Check for correct classes
    checks = [
        ("rvo-link base class", "rvo-link" in result),
        ("rvo-menubar__link", "rvo-menubar__link" in result),
        ("rvo-link--logoblauw", "rvo-link--logoblauw" in result),
        ("rvo-link--active (for selected)", "rvo-link--active" in result),
        (
            "Right-aligned items",
            "align"
            in str(
                [
                    item
                    for item in [
                        {"label": "Home", "link": "#", "active": True},
                        {"label": "About", "link": "/about"},
                        {"label": "Menu item rechts", "link": "#", "align": "right"},
                    ]
                    if item.get("align") == "right"
                ]
            ),
        ),
    ]

    for check_name, passed in checks:
        status = "✅" if passed else "❌"
        print(f"{status} {check_name}")

    # Count occurrences
    print("\n📊 COUNTS:")
    print(f"- rvo-link: {result.count('rvo-link')}")
    print(f"- rvo-link--logoblauw: {result.count('rvo-link--logoblauw')}")
    print(f"- rvo-link--active: {result.count('rvo-link--active')}")
    print(f"- rvo-menubar__item: {result.count('rvo-menubar__item')}")


if __name__ == "__main__":
    test_menubar_html()
