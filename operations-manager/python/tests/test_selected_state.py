#!/usr/bin/env python3
"""
Test the new selected attribute in menubar
"""

import sys

sys.path.append("/Users/robbertuittenbroek/IdeaProjects/RIG-Cluster/jinja-roos-components")

from jinja2 import Environment, FileSystemLoader
from jinja_roos_components.extension import setup_components


def test_selected_state():
    """Test the selected attribute functionality"""

    # Write test template
    test_template_content = """
<c-menubar 
    size="md"
    :useIcons="true"
    :items="[
        {
            'label': 'Home',
            'link': '/',
            'selected': true
        },
        {
            'label': 'About',
            'link': '/about',
            'active': true
        },
        {
            'label': 'Services',
            'link': '/services'
        },
        {
            'label': 'Contact',
            'link': '/contact',
            'align': 'right'
        }
    ]" />
"""

    with open(
        "/Users/robbertuittenbroek/IdeaProjects/RIG-Cluster/operations-manager/python/templates/test-selected.html", "w"
    ) as f:
        f.write(test_template_content)

    template_dir = "/Users/robbertuittenbroek/IdeaProjects/RIG-Cluster/operations-manager/python/templates"
    env = Environment(loader=FileSystemLoader(template_dir))
    setup_components(env)

    template = env.get_template("test-selected.html")
    result = template.render()

    print("SELECTED STATE TEST RESULTS:")
    print("=" * 50)

    # Count active states
    active_count = result.count("rvo-link--active")
    selected_items = []

    # Look for specific items with active class
    lines = result.split("\n")
    current_item = None

    for line in lines:
        if "href=" in line and ("Home" in line or "About" in line or "Services" in line or "Contact" in line):
            if "Home" in line:
                current_item = "Home"
            elif "About" in line:
                current_item = "About"
            elif "Services" in line:
                current_item = "Services"
            elif "Contact" in line:
                current_item = "Contact"

            if "rvo-link--active" in line and current_item:
                selected_items.append(current_item)

    print(f"✅ Total items with rvo-link--active: {active_count}")
    print(f"✅ Items marked as active/selected: {', '.join(selected_items)}")

    # Verify expected behavior
    expected_selected = ["Home", "About"]  # Home has 'selected': true, About has 'active': true
    success = active_count == 2 and "Home" in selected_items and "About" in selected_items

    print(
        f"\n{'🎉 SUCCESS' if success else '❌ FAILURE'}: Selected state is {'working correctly' if success else 'not working'}"
    )

    if success:
        print("\n📝 EXPLANATION:")
        print("- Home: Has 'selected': true → Shows rvo-link--active (persistent)")
        print("- About: Has 'active': true → Shows rvo-link--active (temporary)")
        print("- Services: No state → Normal styling")
        print("- Contact: No state → Normal styling")

    # Also test formulier template
    print("\n" + "=" * 50)
    print("TESTING FORMULIER TEMPLATE:")

    formulier_template = env.get_template("formulier-template.html.j2")
    formulier_result = formulier_template.render()

    formulier_active = formulier_result.count("rvo-link--active")
    has_naam_app_selected = "Naam app/website" in formulier_result and "rvo-link--active" in formulier_result

    print(f"✅ Formulier active items: {formulier_active}")
    print(f"✅ 'Naam app/website' is selected: {has_naam_app_selected}")

    return success and has_naam_app_selected


if __name__ == "__main__":
    test_selected_state()
