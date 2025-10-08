#!/usr/bin/env python3
"""
Test to verify why the first components in a section aren't being processed.
"""

from jinja2 import DictLoader, Environment
from jinja_roos_components import setup_components

# Test template that mimics the structure in architecture-overview.html.j2
test_template = """
{% extends "base.html.j2" %}

{% block content %}
<div class="rvo-layout-column rvo-layout-gap--3xl">
    
    {# Hero Section #}
    <section class="rvo-hero">
        <p>Hero content here</p>
    </section>

    {# System Context - The Big Picture #}
    <div>
        <c-layout-flow gap="lg">
            <div>
                <c-heading type="h2" textContent="The Big Picture"/>
                <p class="rvo-text--lg">
                    This shows who uses RIG Cluster.
                </p>
            </div>

            <c-card padding="xl">
                <div>Card content</div>
            </c-card>
        </c-layout-flow>
    </div>

</div>
{% endblock %}
"""

base_template = """
<!DOCTYPE html>
<html>
<body>
    {% block content %}{% endblock %}
</body>
</html>
"""

# Create environment
templates = {"test.html.j2": test_template, "base.html.j2": base_template}

env = Environment(loader=DictLoader(templates))
setup_components(env)

print("Testing template with components after comments and sections:")
print("=" * 60)

try:
    template = env.get_template("test.html.j2")
    output = template.render()

    # Check for unprocessed components
    import re

    unprocessed = re.findall(r"<c-[a-z-]+[^>]*>", output)

    if unprocessed:
        print(f"Found {len(unprocessed)} unprocessed components:")
        for comp in unprocessed:
            print(f"  - {comp}")
    else:
        print("All components were processed successfully!")

    print("\nFirst 500 chars of output:")
    print(output[:500])

except Exception as e:
    print(f"Error: {e}")
    import traceback

    traceback.print_exc()
